"""#1707 行为契约测试：附件纯 Python 有界解析、下载中心零改码适配与跨主体隔离。"""

from __future__ import annotations

import io
import json
import struct
import threading
import zipfile
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import docx
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter

from aiops_diagnostics.common.scalar_rules import now_utc
from aiops_diagnostics.conversation.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway.gateway_api import create_gateway_app
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_store import GatewayStore
from aiops_diagnostics.identity.capability import CAPABILITY_ASSERTED
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord
from aiops_diagnostics.operator.attachments import MAX_IMAGE_BYTES, parse_attachment

_OP_A = {"Authorization": "Bearer user-a", "X-Business-Entry": "operator"}
_OP_B = {"Authorization": "Bearer user-b", "X-Business-Entry": "operator"}
_CONSUMER_A = {"Authorization": "Bearer user-a", "X-Business-Entry": "consumer"}


class _MultiUserCaller:
    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> ScopeContext:
        del third_session, platform_entry, source_key
        uid = "U-B" if token == "user-b" else "U-A"
        tenant = "T-B" if token == "user-b" else "T-A"
        subject = SubjectRecord(b_user_id=uid, c_user_id=uid, tenant_id=tenant)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=tenant,
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset({required_scope}),
            capability_source=CAPABILITY_ASSERTED,
        )


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord(c_user_id, c_user_id, tenant_id, "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


class _Runtime:
    def shutdown(self) -> None:
        pass


class _FakeCompanyServer:
    """复现 company cloud-upms + cloud-mall-admin 真实接口契约的本地只读替身。"""

    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []
        self.raw_record_bodies: list[str] = []
        self.upload_mode = "ok"
        self._counter = 100
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                del format, args

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length)

            def _reply(self, code: int, payload: Any) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:  # noqa: N802
                raw = self._read_body()
                if self.path == "/upms/file/upload":
                    outer.uploads.append(
                        {
                            "tenant_id": self.headers.get("tenant-id"),
                            "from": self.headers.get("from"),
                            "content_type": self.headers.get("Content-Type", ""),
                            "body": raw,
                        }
                    )
                    if outer.upload_mode == "policy_400":
                        self._reply(400, {"code": 1, "msg": "文件后缀不在 file.upload.allowed 白名单"})
                        return
                    if outer.upload_mode == "policy_413":
                        self._reply(413, {"code": 1, "msg": "Maximum upload size exceeded"})
                        return
                    if outer.upload_mode == "http_link":
                        self._reply(200, {"link": "http://insecure.example/a.png"})
                        return
                    outer._counter += 1
                    self._reply(200, {"link": f"https://oss.example/aiops/{outer._counter}.bin"})
                    return
                if self.path == "/mall/importRecord/saveImportRecord":
                    text = raw.decode("utf-8")
                    outer.raw_record_bodies.append(text)
                    payload = json.loads(text)
                    outer._counter += 1
                    rec_id = f"rec_{outer._counter}"
                    outer.records.append({"method": "POST", "id": rec_id, "payload": payload})
                    self._reply(200, {"code": 0, "data": {"id": rec_id}})
                    return
                self._reply(404, {"code": 404})

            def do_PUT(self) -> None:  # noqa: N802
                raw = self._read_body()
                if self.path == "/mall/importRecord/updateImportRecord":
                    text = raw.decode("utf-8")
                    outer.raw_record_bodies.append(text)
                    payload = json.loads(text)
                    outer.records.append({"method": "PUT", "payload": payload})
                    self._reply(200, {"code": 0, "data": True})
                    return
                self._reply(404, {"code": 404})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def upms_base(self) -> str:
        return f"http://127.0.0.1:{self.port}/upms"

    @property
    def mall_base(self) -> str:
        return f"http://127.0.0.1:{self.port}/mall"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _make_png(color: tuple[int, int, int] = (20, 120, 220)) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (64, 48), color=color)
    exif = img.getexif()
    exif[271] = "CameraSecretModel"
    img.save(buf, format="PNG", exif=exif)
    return buf.getvalue()


def _make_pdf(text: str = "Order 202611250001 fault E-402", *, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    # Append a tiny content stream with ASCII text so pypdf extracts it reliably
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode("latin1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("secret")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_docx(paragraph: str = "工单排查说明：充电枪报错 E-402") -> bytes:
    document = docx.Document()
    document.add_paragraph(paragraph)
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "订单号"
    table.rows[0].cells[1].text = "ORD-20261125"
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _make_zip_bomb_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "A" * (600 * 1024))
    return buf.getvalue()


def _make_minimal_ole_doc(text: str, *, encrypted: bool = False) -> bytes:
    """构造最小合法 OLE2 Compound File（Word 97-2003 FIB + 0Table Clx Piece Table）。"""
    encoded_text = text.encode("utf-16le")
    text_offset = 0x200
    word_stream = bytearray(text_offset + len(encoded_text))
    word_stream[text_offset : text_offset + len(encoded_text)] = encoded_text
    struct.pack_into("<H", word_stream, 0x0000, 0xA5EC)
    flags = 0x0100 if encrypted else 0x0000
    struct.pack_into("<H", word_stream, 0x000A, flags)

    # Clx: 0x02 + lcb (16 = 4*(1+1) + 8*1) + CPs [0, len(text)] + PCD (2B + 4B fc + 2B)
    lcb = 4 * 2 + 8
    clx = bytearray()
    clx.append(0x02)
    clx.extend(struct.pack("<I", lcb))
    clx.extend(struct.pack("<II", 0, len(text)))
    clx.extend(struct.pack("<HIH", 0, text_offset, 0))
    struct.pack_into("<II", word_stream, 0x01A2, 0, len(clx))
    table_stream = bytes(clx)

    # Build a valid OLE2 v3 (512-byte sectors) container containing WordDocument & 0Table
    sec = 512

    def pad(b: bytes) -> bytes:
        rem = len(b) % sec
        return b if rem == 0 else b + b"\x00" * (sec - rem)

    # Ensure streams >= 4096 bytes so they live in regular FAT sectors (not MiniFAT)
    ws_full = pad(bytes(word_stream).ljust(4096, b"\x00"))
    ts_full = pad(table_stream.ljust(4096, b"\x00"))
    ws_sectors = len(ws_full) // sec
    ts_sectors = len(ts_full) // sec

    # Sector layout: 0=FAT, 1=Directory, 2..=WordDocument, next..=0Table
    ws_start = 2
    ts_start = ws_start + ws_sectors
    fat = [0xFFFFFFFF] * (sec // 4)
    fat[0] = 0xFFFFFFFD  # FATSECT
    fat[1] = 0xFFFFFFFE  # ENDOFCHAIN (Directory)
    for i in range(ws_sectors):
        fat[ws_start + i] = ws_start + i + 1 if i + 1 < ws_sectors else 0xFFFFFFFE
    for i in range(ts_sectors):
        fat[ts_start + i] = ts_start + i + 1 if i + 1 < ts_sectors else 0xFFFFFFFE
    fat_bytes = struct.pack(f"<{sec // 4}I", *fat)

    def dir_entry(
        name: str, obj_type: int, start_sec: int, size: int, left: int = -1, right: int = -1, child: int = -1
    ) -> bytes:
        entry = bytearray(128)
        enc = (name + "\x00").encode("utf-16le")
        entry[: len(enc)] = enc
        struct.pack_into("<H", entry, 64, len(enc))
        entry[66] = obj_type
        entry[67] = 1  # black
        struct.pack_into("<iii", entry, 68, left, right, child)
        struct.pack_into("<II", entry, 116, start_sec, size)
        return bytes(entry)

    directory = (
        dir_entry("Root Entry", 5, 0xFFFFFFFE, 0, child=1)
        + dir_entry("WordDocument", 2, ws_start, len(ws_full), left=-1, right=2)
        + dir_entry("0Table", 2, ts_start, len(ts_full))
        + bytes(128)
    )

    header = bytearray(sec)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<HH", header, 24, 0x003E, 0x0003)  # minor/major v3
    struct.pack_into("<HH", header, 28, 0xFFFE, 9)  # little endian, 512B sector
    struct.pack_into("<H", header, 32, 6)  # mini sector 64B
    struct.pack_into("<I", header, 44, 1)  # 1 FAT sector
    struct.pack_into("<I", header, 48, 1)  # Directory at sector 1
    struct.pack_into("<I", header, 56, 4096)  # mini cutoff
    struct.pack_into("<I", header, 60, 0xFFFFFFFE)  # no MiniFAT
    struct.pack_into("<I", header, 68, 0xFFFFFFFE)  # no DIFAT
    difat = [0xFFFFFFFF] * 109
    difat[0] = 0
    struct.pack_into("<109I", header, 76, *difat)

    return bytes(header) + fat_bytes + directory + ws_full + ts_full


def _make_client(tmp_path: Path, *, upms_base: str = "", mall_base: str = "") -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        download_center_upms_base_url=upms_base,
        download_center_mall_base_url=mall_base,
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=_Runtime(),  # type: ignore[arg-type]
        caller_resolver=_MultiUserCaller(),
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app)


def test_upload_parse_preview_download_center_and_isolation(tmp_path: Path) -> None:
    company = _FakeCompanyServer()
    try:
        with _make_client(tmp_path, upms_base=company.upms_base, mall_base=company.mall_base) as client:
            # 1) PNG upload -> fileType=doc (bypass watermark), exportType=3101 before direction=1
            png_resp = client.post(
                "/v1/assistant/files",
                headers=_OP_A,
                files={"file": ("site_screen.png", _make_png(), "image/png")},
            )
            assert png_resp.status_code == 201
            png_body = png_resp.json()
            assert png_body["category"] == "image"
            assert png_body["parse_status"] == "ready"
            assert png_body["preview_url"] == f"/v1/assistant/files/{png_body['file_id']}/preview"
            assert "storage_url" not in png_body
            assert png_body["download_center_record_id"] is not None

            # Verify company UPMS received fileType=doc & from:Y
            assert b'name="fileType"\r\n\r\ndoc\r\n' in company.uploads[-1]["body"]
            assert company.uploads[-1]["from"] == "Y"
            assert company.uploads[-1]["tenant_id"] == "T-A"

            # Verify JSON key order: exportType strictly before direction, exportType=3101 & direction=1
            raw_save = company.raw_record_bodies[0]
            assert raw_save.index('"exportType"') < raw_save.index('"direction"')
            assert company.records[0]["payload"]["exportType"] == 3101
            assert company.records[0]["payload"]["direction"] == 1
            assert company.records[1]["payload"]["state"] == 1
            assert company.records[1]["payload"]["sussessCount"] == 1
            assert company.records[1]["payload"]["resultDetail"].startswith("https://")

            # Preview returns JPEG thumbnail without EXIF metadata
            preview = client.get(png_body["preview_url"], headers=_OP_A)
            assert preview.status_code == 200
            assert preview.headers["content-type"] == "image/jpeg"
            assert b"CameraSecretModel" not in preview.content

            # 2) PDF upload
            pdf_resp = client.post(
                "/v1/assistant/files",
                headers=_OP_A,
                files={"file": ("ticket.pdf", _make_pdf(), "application/pdf")},
            )
            assert pdf_resp.status_code == 201
            pdf_body = pdf_resp.json()
            assert pdf_body["category"] == "pdf"
            assert pdf_body["page_count"] == 1
            assert pdf_body["preview_url"] is None

            # 3) DOCX upload
            docx_resp = client.post(
                "/v1/assistant/files",
                headers=_OP_A,
                files={
                    "file": (
                        "report.docx",
                        _make_docx(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
            assert docx_resp.status_code == 201
            assert docx_resp.json()["category"] == "word"

            # 4) Legacy Word .doc upload (pure Python OLE2 piece table)
            doc_resp = client.post(
                "/v1/assistant/files",
                headers=_OP_A,
                files={
                    "file": (
                        "legacy.doc",
                        _make_minimal_ole_doc("Legacy fault E-402"),
                        "application/msword",
                    )
                },
            )
            assert doc_resp.status_code == 201
            doc_id = doc_resp.json()["file_id"]
            stored_doc = client.app.state.gateway.assistant_files.get(
                doc_id,
                _MultiUserCaller()
                .resolve("user-a", required_scope="aiops:diagnoses:write")
                .scope_fingerprint,
            )
            assert stored_doc is not None
            assert "Legacy fault E-402" in (stored_doc["extracted_text"] or "")

            # 5) Download task -> registers exportType=10086 before direction=-1, returns notify_file_center
            before_count = len(company.raw_record_bodies)
            dl_resp = client.post(
                f"/v1/assistant/files/{pdf_body['file_id']}/download-task",
                headers=_OP_A,
            )
            assert dl_resp.status_code == 200
            dl_body = dl_resp.json()
            assert dl_body["direction"] == -1
            assert dl_body["status"] == "ready"
            assert dl_body["notify_file_center"] is True
            raw_export = company.raw_record_bodies[before_count]
            assert raw_export.index('"exportType"') < raw_export.index('"direction"')
            export_rec = json.loads(raw_export)
            assert export_rec["exportType"] == 10086
            assert export_rec["direction"] == -1

            # 6) Cross-tenant isolation (404) & consumer forbidden (403)
            fid = png_body["file_id"]
            assert client.get(f"/v1/assistant/files/{fid}", headers=_OP_B).status_code == 404
            assert client.get(f"/v1/assistant/files/{fid}/preview", headers=_OP_B).status_code == 404
            assert client.post(f"/v1/assistant/files/{fid}/download-task", headers=_OP_B).status_code == 404
            assert client.get(f"/v1/assistant/files/{fid}", headers=_CONSUMER_A).status_code == 403
            assert (
                client.get(f"/v1/assistant/files/{pdf_body['file_id']}/preview", headers=_OP_A).status_code
                == 404
            )
    finally:
        company.close()


def test_attachment_validation_and_upstream_error_mapping(tmp_path: Path) -> None:
    # Pure unit validation checks for magic spoof, empty, encrypted PDF/DOC, zip-bomb DOCX, oversized image
    for bad_args, expected_code, expected_status in [
        ((b"not-a-pdf", "fake.pdf", "application/pdf"), "ATTACHMENT_TYPE_UNSUPPORTED", 400),
        ((b"", "empty.pdf", "application/pdf"), "ATTACHMENT_INVALID", 400),
        ((_make_pdf(encrypted=True), "locked.pdf", "application/pdf"), "ATTACHMENT_INVALID", 400),
        (
            (_make_minimal_ole_doc("secret", encrypted=True), "locked.doc", "application/msword"),
            "ATTACHMENT_INVALID",
            400,
        ),
        (
            (
                _make_zip_bomb_docx(),
                "bomb.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            "ATTACHMENT_INVALID",
            400,
        ),
        ((b"\xff\xd8\xff" + b"0" * MAX_IMAGE_BYTES, "huge.jpg", "image/jpeg"), "ATTACHMENT_TOO_LARGE", 413),
    ]:
        try:
            parse_attachment(*bad_args)
            raise AssertionError(f"Expected {expected_code}")
        except Exception as exc:  # noqa: BLE001
            assert getattr(exc, "code", None) == expected_code
            assert getattr(exc, "http_status", None) == expected_status

    # Unconfigured download center -> fail-closed 502 DOWNLOAD_CENTER_UNAVAILABLE
    with _make_client(tmp_path) as unconfigured:
        resp = unconfigured.post(
            "/v1/assistant/files",
            headers=_OP_A,
            files={"file": ("a.png", _make_png(), "image/png")},
        )
        assert resp.status_code == 502
        assert resp.json()["error"]["code"] == "DOWNLOAD_CENTER_UNAVAILABLE"
        assert resp.json()["error"]["retryable"] is True

    # Upstream policy rejection & non-HTTPS link mapping + rate limit + TTL expiry
    company = _FakeCompanyServer()
    try:
        with _make_client(tmp_path, upms_base=company.upms_base, mall_base=company.mall_base) as client:
            company.upload_mode = "policy_400"
            r400 = client.post(
                "/v1/assistant/files",
                headers=_OP_A,
                files={"file": ("a.png", _make_png(), "image/png")},
            )
            assert r400.status_code == 400
            assert r400.json()["error"]["code"] == "UPSTREAM_FILE_POLICY_REJECTED"

            company.upload_mode = "policy_413"
            r413 = client.post(
                "/v1/assistant/files",
                headers=_OP_A,
                files={"file": ("a.png", _make_png(), "image/png")},
            )
            assert r413.status_code == 413
            assert r413.json()["error"]["code"] == "UPSTREAM_FILE_POLICY_REJECTED"

            company.upload_mode = "http_link"
            r502 = client.post(
                "/v1/assistant/files",
                headers=_OP_A,
                files={"file": ("a.png", _make_png(), "image/png")},
            )
            assert r502.status_code == 502
            assert r502.json()["error"]["code"] == "DOWNLOAD_CENTER_UNAVAILABLE"

            # Rate limiting returns 429 UPLOAD_RATE_LIMITED
            company.upload_mode = "ok"
            client.app.state.gateway.assistant_files.rate_limiter._max_per_window = 1
            client.app.state.gateway.assistant_files.rate_limiter._recent.clear()
            first = client.post(
                "/v1/assistant/files",
                headers=_OP_A,
                files={"file": ("ok.png", _make_png(), "image/png")},
            )
            assert first.status_code == 201
            second = client.post(
                "/v1/assistant/files",
                headers=_OP_A,
                files={"file": ("ok2.png", _make_png(), "image/png")},
            )
            assert second.status_code == 429
            assert second.json()["error"]["code"] == "UPLOAD_RATE_LIMITED"

            # TTL expiry purges expired files
            expired_count = client.app.state.gateway.assistant_files.expire_now(
                at=now_utc() + timedelta(days=31)
            )
            assert expired_count >= 1
            assert (
                client.get(f"/v1/assistant/files/{first.json()['file_id']}", headers=_OP_A).status_code == 404
            )
    finally:
        company.close()
