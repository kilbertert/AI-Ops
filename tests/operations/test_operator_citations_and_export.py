"""结构化引用来源 citations 与排查报告导出至公司下载中心测试（#1711）。"""

from __future__ import annotations

import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from docx import Document

from aiops_diagnostics.operator import operator_agent, operator_cpms
from aiops_diagnostics.operator.operator_evidence import render_evidence
from operations.test_operator_cpms import HEADERS, ORDER, environment, node


def _parse_sse_frames(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for raw in body.strip().split("\n\n"):
        if not raw.strip():
            continue
        frame: dict[str, Any] = {}
        data_lines: list[str] = []
        for line in raw.splitlines():
            if line.startswith("id: "):
                frame["id"] = line[4:]
            elif line.startswith("event: "):
                frame["event"] = line[7:]
            elif line.startswith("data: "):
                data_lines.append(line[6:])
        if data_lines:
            frame["data"] = json.loads("\n".join(data_lines))
        if frame:
            frames.append(frame)
    return frames


class _ExportStubServer:
    """本地公司 UPMS + Mall 下载中心桩服务，核对 exportType=10086 与 direction=-1。"""

    def __init__(self, *, fail_upload: bool = False) -> None:
        self.fail_upload = fail_upload
        self.uploads: list[dict[str, Any]] = []
        self.saved: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.raw_bodies: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                if self.path == "/file/upload":
                    if outer.fail_upload:
                        self.send_response(503)
                        self.end_headers()
                        return
                    outer.uploads.append(
                        {
                            "headers": dict(self.headers),
                            "body": body,
                        }
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        json.dumps({"link": "https://oss.example.test/aiops/export/report-1.docx"}).encode()
                    )
                    return
                if self.path == "/importRecord/saveImportRecord":
                    raw = body.decode("utf-8")
                    outer.raw_bodies.append(raw)
                    outer.saved.append(json.loads(raw))
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"code": 0, "data": {"id": "EXP_REC_9001"}}).encode())
                    return
                self.send_response(404)
                self.end_headers()

            def do_PUT(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                outer.raw_bodies.append(raw)
                outer.updated.append(json.loads(raw))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"code": 0, "data": True}).encode())

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()


def _wait_completed(client, qa_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        res = client.get(f"/v1/assistant/questions/{qa_id}", headers=HEADERS)
        assert res.status_code == 200
        body = res.json()
        if body["status"] in {"completed", "failed", "cancelled"}:
            return body
        time.sleep(0.02)
    raise AssertionError("qa did not complete in time")


def test_build_citations_covers_cpms_tools_and_attachments():
    evidence = {
        "cpms_order_detail": {
            "observed_at": "2026-11-25T08:00:00+00:00",
            "data": {"orderNo": "ORD-001", "status": 4, "totalAmount": "15.20"},
        },
        "cpms_order_amount_check": {
            "observed_at": "2026-11-25T08:00:02+00:00",
            "data": {"conclusion": "整单计费仍待验证"},
        },
    }
    attachments = [
        {
            "file_id": "file_abc123",
            "file_name": "桩端日志.pdf",
            "category": "document",
            "page_count": 3,
            "extracted_text": "充电桩返回错误码 E104，连接器锁止异常。",
            "created_at": "2026-11-25T07:59:00+00:00",
        }
    ]
    rendered = operator_agent.render("排查完成。", evidence, "zh", attachments=attachments)
    assert rendered["blocks"][0]["kind"] == "text"
    citations = rendered["citations"]
    assert len(citations) == 3
    assert citations[0]["source_type"] == "cpms_tool"
    assert citations[0]["downloadable"] is False
    assert citations[0]["file_id"] is None
    assert "ORD-001" in citations[0]["snippet"]
    assert citations[1]["source_type"] == "cpms_tool"
    assert "整单计费仍待验证" in citations[1]["snippet"]
    assert citations[2]["source_type"] == "attachment"
    assert citations[2]["file_id"] == "file_abc123"
    assert citations[2]["title"] == "桩端日志.pdf"
    assert citations[2]["page"] == 3
    assert citations[2]["downloadable"] is True

    ev_only = render_evidence(evidence, "zh", attachments=attachments)
    assert ev_only is not None
    assert len(ev_only["citations"]) == 3


def test_operator_citations_sse_readback_and_report_export(tmp_path, monkeypatch):
    stub = _ExportStubServer()
    try:
        with environment(tmp_path, monkeypatch) as env:
            env["settings"].download_center_upms_base_url = stub.base_url
            env["settings"].download_center_mall_base_url = stub.base_url
            env["settings"].download_center_upload_export_type = 3101
            env["settings"].download_center_download_export_type = 10086
            env["app"].state.gateway.download_center = env["app"].state.gateway.download_center.from_settings(
                env["settings"]
            )

            # Seed an attachment and bind it to the question so citations include both cpms_tool & attachment.
            doc = Document()
            doc.add_paragraph("现场排查补充说明：充电枪头温度偏高。")
            buf = io.BytesIO()
            doc.save(buf)
            up = env["client"].post(
                "/v1/assistant/files",
                headers=HEADERS,
                files={
                    "file": (
                        "现场工单.docx",
                        buf.getvalue(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
            assert up.status_code == 201, up.text
            file_id = up.json()["file_id"]

            monkeypatch.setattr(
                operator_cpms.SnailDispatcher,
                "dispatch",
                lambda self, _cfg, run_id: node(env, run_id, tools=("cpms_order_detail",)),
            )
            ask = env["client"].post(
                "/v1/assistant/questions",
                headers=HEADERS,
                json={
                    "question": f"帮我排查订单 {ORDER} 为什么提前停充",
                    "order_no": ORDER,
                    "attachment_ids": [file_id],
                },
            )
            assert ask.status_code == 202, ask.text
            qa_id = ask.json()["qa_id"]
            conv_id = ask.json()["conversation_id"]

            done = _wait_completed(env["client"], qa_id)
            assert done["status"] == "completed"
            citations = done["result"]["citations"]
            assert [c["source_type"] for c in citations] == ["cpms_tool", "attachment"]
            assert citations[1]["file_id"] == file_id
            assert citations[1]["downloadable"] is True

            # Verify SSE event stream includes structured citations event before message_done.
            sse = env["client"].get(f"/v1/assistant/questions/{qa_id}/events", headers=HEADERS)
            assert sse.status_code == 200
            frames = _parse_sse_frames(sse.text)
            event_names = [f["event"] for f in frames]
            assert "citations" in event_names
            assert event_names.index("citations") < event_names.index("message_done")
            cit_frame = next(f for f in frames if f["event"] == "citations")
            assert len(cit_frame["data"]["citations"]) == 2

            # Verify conversation readback preserves structured citations.
            conv = env["client"].get(f"/v1/conversations/{conv_id}", headers=HEADERS)
            assert conv.status_code == 200
            assert len(conv.json()["turns"][0]["citations"]) == 2

            # Export .docx report to company download center (direction=-1, exportType=10086).
            stub.uploads.clear()
            stub.saved.clear()
            stub.updated.clear()
            stub.raw_bodies.clear()
            exp_docx = env["client"].post(
                f"/v1/assistant/questions/{qa_id}/export",
                headers=HEADERS,
                json={"format": "docx"},
            )
            assert exp_docx.status_code == 200, exp_docx.text
            body = exp_docx.json()
            assert body["download_center_record_id"] == "EXP_REC_9001"
            assert body["direction"] == -1
            assert body["status"] == "ready"
            assert body["format"] == "docx"
            assert body["notify_file_center"] is True
            assert body["file_name"].endswith(".docx")
            assert "storage_url" not in body
            assert "oss.example.test" not in json.dumps(body)

            # Verify company zero-code-change protocol details.
            assert len(stub.uploads) == 1
            assert b'name="fileType"\r\n\r\ndoc' in stub.uploads[0]["body"]
            assert stub.saved[0]["exportType"] == 10086
            assert stub.saved[0]["direction"] == -1
            assert stub.raw_bodies[0].index('"exportType"') < stub.raw_bodies[0].index('"direction"')
            assert stub.updated[0]["state"] == 1
            assert stub.updated[0]["sussessCount"] == 1
            assert stub.updated[0]["resultDetail"].startswith("https://")

            # Export .md report as well and verify content includes citations & disclaimer without URLs.
            stub.uploads.clear()
            exp_md = env["client"].post(
                f"/v1/assistant/questions/{qa_id}/export",
                headers=HEADERS,
                json={"format": "md"},
            )
            assert exp_md.status_code == 200, exp_md.text
            assert exp_md.json()["file_name"].endswith(".md")
            uploaded_md_body = stub.uploads[0]["body"].decode("utf-8", errors="replace")
            assert "充电桩后台排查报告" in uploaded_md_body
            assert "现场工单.docx" in uploaded_md_body
            assert "未对订单、计费、设备或业务工单执行任何变更操作" in uploaded_md_body

            # Non-completed QA cannot be exported (409 REPORT_NOT_READY).
            store = env["app"].state.gateway.store
            pending = store.create_assistant_question(
                scope_fingerprint=env["resolver"].current.scope_fingerprint,
                question="排队中问题",
            )
            not_ready = env["client"].post(
                f"/v1/assistant/questions/{pending['qa_id']}/export",
                headers=HEADERS,
                json={"format": "docx"},
            )
            assert not_ready.status_code == 409
            assert not_ready.json()["error"]["code"] == "REPORT_NOT_READY"

            # Consumer entry forbidden (403).
            consumer_res = env["client"].post(
                f"/v1/assistant/questions/{qa_id}/export",
                headers={**HEADERS, "X-Business-Entry": "consumer"},
                json={"format": "docx"},
            )
            assert consumer_res.status_code == 403

            # Upstream failure fails closed with 502 DOWNLOAD_CENTER_UNAVAILABLE.
            stub.fail_upload = True
            failed_exp = env["client"].post(
                f"/v1/assistant/questions/{qa_id}/export",
                headers=HEADERS,
                json={"format": "docx"},
            )
            assert failed_exp.status_code == 502
            assert failed_exp.json()["error"]["code"] == "DOWNLOAD_CENTER_UNAVAILABLE"
    finally:
        stub.close()
