#!/usr/bin/env python3
"""离线核对OPS-07预览证据；不发送模型或业务请求，不输出订单/推理正文。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def json_values(text: str):
    decoder = json.JSONDecoder()
    while text.strip():
        text = text.lstrip()
        value, end = decoder.raw_decode(text)
        yield value
        text = text[end:]


def tool_results(value: Any):
    if isinstance(value, str):
        for decoded in json_values(value):
            yield from tool_results(decoded)
    elif isinstance(value, dict):
        if isinstance(value.get("result"), dict) and isinstance(value.get("evidence"), dict):
            yield value
        else:
            for child in value.values():
                yield from tool_results(child)
    elif isinstance(value, list):
        for child in value:
            yield from tool_results(child)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def verify(events, audit, *, tenant: str, app: str, workspace: str, version: int) -> dict:
    ends = [e for e in events if e.get("event") == "message_end"]
    assert len(ends) == 1 and not any(e.get("event") == "error" for e in events), "stream not successful"
    end = ends[0]
    assert end.get("task_id") and end.get("message_id") and end.get("conversation_id"), "missing run identity"
    for event in events:
        for key in ("task_id", "message_id", "conversation_id"):
            assert not event.get(key) or event[key] == end[key], "mixed preview identities"

    thoughts = {}
    for event in events:
        if event.get("event") == "agent_thought":
            thought = thoughts.setdefault(event["id"], {})
            for key in ("position", "tool", "tool_input", "observation"):
                value = event.get(key)
                if value not in (None, ""):
                    assert key not in thought or thought[key] == value, "conflicting thought updates"
                    thought[key] = value
    assert 1 <= len(thoughts) <= 3, "model decision limit exceeded"
    ordered = sorted(thoughts.values(), key=lambda t: t["position"])
    assert len({t["position"] for t in ordered}) == len(ordered), "duplicate decision positions"
    calls = [(t["tool"], json.loads(t["tool_input"])) for t in ordered if t.get("tool")]
    assert [name for name, _ in calls] == ["order_list", "charging_info"], "wrong tool sequence"
    assert calls[0][1] == {"order_list": {"page": 1, "page_size": 1}}, "list input not bounded"

    unique = {}
    duplicate_count = 0
    for thought in ordered:
        for output in tool_results(thought.get("observation")):
            evidence = output["evidence"]
            assert evidence["tool"] == thought.get("tool"), "result belongs to another decision"
            call_id = evidence["call_id"]
            if call_id in unique:
                assert unique[call_id] == output, "conflicting repeated tool result"
                duplicate_count += 1
            else:
                unique[call_id] = output
            assert evidence["outcome"] == "succeeded", "tool refused"
            assert evidence["subject_type"] == "tenant_service", "wrong subject"
            assert (
                evidence["tenant_id"],
                evidence["registered_app_id"],
                evidence["registered_workspace_id"],
            ) == (
                tenant,
                app,
                workspace,
            ), "wrong registered target"
            assert evidence["permission_version"] == version, "wrong permission version"
            assert evidence["result_sha256"] == digest(output["result"]), "wrong result digest"
    assert len(unique) == 2, "missing or extra tool results"
    by_tool = {o["evidence"]["tool"]: o for o in unique.values()}
    assert set(by_tool) == {"order_list", "charging_info"}, "wrong tool results"
    listed, status = by_tool["order_list"]["result"], by_tool["charging_info"]["result"]
    assert len(listed["orders"]) == 1 and listed["page"] == listed["page_size"] == 1, "unbounded list result"
    assert listed["source"] == "mysql.ch_order_info" and status["source"] == "cpms.chargingInfo", (
        "wrong source"
    )
    assert status["order_no"] == listed["orders"][0]["order_no"], "selected order mismatch"
    assert calls[1][1] == {"charging_info": {"order_no": status["order_no"]}}, "status input not from list"

    answer = "".join(e.get("answer", "") for e in events if e.get("event") == "agent_message")
    reasoning = bool(re.match(r"^\s*<think>.*?</think>", answer, flags=re.S))
    answer = re.sub(r"^\s*<think>.*?</think>", "", answer, count=1, flags=re.S).strip()
    final = json.loads(answer)
    expected = {k: status[k] for k in ("order_no", "status_code", "status_role", "source", "observed_at")}
    expected["call_id"] = by_tool["charging_info"]["evidence"]["call_id"]
    assert final == expected, "final answer not bound to tool result"
    assert len(audit) == 2 and {a["call_id"] for a in audit} == set(unique), "audit does not match preview"
    for row in audit:
        assert row == unique[row["call_id"]]["evidence"], "audit evidence differs"
    usage = end.get("metadata", {}).get("usage", {})
    assert usage.get("total_tokens", 0) > 0, "missing model usage"
    return {
        "passed": True,
        "model_decisions_observed": len(thoughts),
        "successful_tool_calls": len(unique),
        "duplicate_results_ignored": duplicate_count,
        "permission_version": version,
        "list_then_status": True,
        "evidence_matches_audit": True,
        "final_answer_matches_tool": True,
        "reasoning_prefix_present": reasoning,
        "status_code": status["status_code"],
        "status_role": status["status_role"],
        "usage": {k: usage[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens") if k in usage},
        "evidence_sha256": digest({"events": events, "audit": audit}),
        "new_model_or_business_requests": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True, help="该预览窗口内的审计JSON数组")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify(
            json.loads(args.events.read_text()),
            json.loads(args.audit.read_text()),
            tenant=args.tenant,
            app=args.app,
            workspace=args.workspace,
            version=args.version,
        )
    except (AssertionError, ValueError, KeyError, TypeError, OSError) as error:
        print(f"FAIL: {type(error).__name__}; 检查私有证据，不输出原文")
        return 1
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    args.out.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
