from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from aiops_diagnostics.operator.device_offline import analyze, instant, window

START, END, NOW = "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z", datetime(2026, 11, 3, tzinfo=UTC)


def event(start, end=None, **kw):
    return {
        "id": "E",
        "tenant_id": "T",
        "device_id": "D",
        "device_code": "PILE",
        "site_id": "S",
        "offline_time": start,
        "recover_time": end,
        **kw,
    }


def order(number, status=2, end="2026-11-01T03:00:00Z", **kw):
    return {
        "tenant_id": "T",
        "device_id": "D",
        "site_id": "S",
        "order_no": number,
        "status": status,
        "created_time": "2026-11-01T01:00:00Z",
        "stop_time": end,
        **kw,
    }


def run(events, orders=()):
    return analyze(events, orders, start=START, end=END, as_of=NOW, timezone="UTC")


def test_window_prior_open_event_survives_zero_new_events():
    result = run([event("2026-10-30T00:00:00Z")])
    assert result["summary"] == {"new_events": 0, "devices": 1, "offline_seconds": 86400}
    assert result["open_incident_list"]["items"][0]["seconds_open"] == 4 * 86400


def test_union_unique_orders_status_groups_and_missing_end():
    result = run(
        [
            event("2026-11-01T00:00:00Z", "2026-11-01T02:00:00Z"),
            event("2026-11-01T01:00:00Z", "2026-11-01T03:00:00Z"),
        ],
        [order("A"), order("B", 3), order("C", 5), order("N", 1), order("U", 2, None)],
    )
    assert result["summary"]["offline_seconds"] == 10800
    overlap = result["order_overlap"]
    assert (overlap["abnormal_orders"], overlap["evaluable"], overlap["overlapped"], overlap["unknown"]) == (
        4,
        3,
        3,
        1,
    )
    assert overlap["share"] == 1
    assert overlap["total"] == 4
    assert all(row["overlap_seconds"] == 7200 for row in overlap["items"])


def test_touching_boundary_and_different_identity_never_overlap():
    result = run(
        [event("2026-11-01T03:00:00Z", "2026-11-01T04:00:00Z")],
        [order("A"), order("B", tenant_id="OTHER"), order("C", site_id="OTHER")],
    )
    assert result["order_overlap"]["overlapped"] == 0


def test_bad_event_does_not_prove_no_order_impact():
    result = run([event(None)], [order("A")])
    assert result["quality"]["invalid_incident_times"] == 1
    assert result["order_overlap"]["unknown"] == 1
    assert result["order_overlap"]["share"] is None


def test_offsets_and_dst_are_not_guessed():
    assert instant("2026-11-01 08:00:00", ZoneInfo("Asia/Shanghai")) == instant(START, ZoneInfo("UTC"))
    with pytest.raises(ValueError):
        instant("2026-11-01 01:30:00", ZoneInfo("America/New_York"))
    with pytest.raises(ValueError):
        window("2026-11-01", "2026-11-02", NOW, "UTC")


def test_agent_offline_followup_and_revoked_readback(tmp_path, monkeypatch):
    from aiops_diagnostics.data.sources import SourceError
    from aiops_diagnostics.operator import offline_tool, operator_cpms
    from operations.test_operator_agent import call_tool, finish
    from operations.test_operator_cpms import HEADERS, ORDER, environment, terminal

    data = run([event("2026-10-30T00:00:00Z")], [order(ORDER)])
    data["order_overlap"]["items"][0]["site_id"] = "SITE-1"
    data["top_devices"]["items"][0]["site_id"] = "SITE-1"
    data["open_incident_list"]["items"][0]["site_id"] = "SITE-1"
    data["scope"] = {"site_count": 1, "device_id": ""}
    data["quality"]["unverified_device_records"] = 0
    revoked = False

    def current(*args):
        if revoked:
            raise SourceError("revoked")

    monkeypatch.setattr(offline_tool, "require_current", current)
    monkeypatch.setattr(
        offline_tool, "query", lambda *args: (data, {"devices": [["D", "SITE-1"]], "orders": []})
    )
    with environment(tmp_path, monkeypatch) as f:
        f["candidate"]["enabled"].append(offline_tool.NAME)
        f["publish"](f["route"].route_revision)
        seen = []

        def dispatch(self, config, run_id):
            if not seen:
                evidence, headers, _ = call_tool(f, run_id, offline_tool.NAME)
                assert evidence["data"]["summary"]["offline_seconds"] == 86400
                finish(
                    f,
                    run_id,
                    headers,
                    "设备PILE的离线记录与订单" + ORDER + "重叠，不能证明原因。",
                    [offline_tool.NAME],
                )
            else:
                # 用户只说“这笔”，单号来自上轮实际工具返回且仍重新走CPMS权限门。
                _, headers, _ = call_tool(f, run_id, "cpms_order_detail", {"order_no": ORDER})
                finish(f, run_id, headers, "这笔订单需要补充原始数据。", ["cpms_order_detail"])
            seen.append(run_id)

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        first = (
            f["client"]
            .post("/v1/assistant/questions", headers=HEADERS, json={"question": "最近哪些设备离线？"})
            .json()
        )
        response = terminal(f, first["qa_id"])
        assert response.json()["status"] == "completed", response.text
        second = (
            f["client"]
            .post(
                "/v1/assistant/questions",
                headers=HEADERS,
                json={"question": "分析这笔关联订单", "conversation_id": first["conversation_id"]},
            )
            .json()
        )
        assert terminal(f, second["qa_id"]).json()["status"] == "completed"
        revoked = True
        response = f["client"].get("/v1/assistant/questions/" + first["qa_id"], headers=HEADERS)
        assert response.status_code in (403, 404, 410), response.text


def test_offline_tool_parameters_cannot_invent_objects():
    from types import SimpleNamespace

    from aiops_diagnostics.operator import offline_tool
    from aiops_diagnostics.operator.operator_conversation import OperatorRefused

    task = SimpleNamespace(history_runs=[], evidence={}, delivered=set(), question="昨天离线的第一台")
    with pytest.raises(OperatorRefused):
        offline_tool.arguments(None, task, None, {"device": "invented"})
    with pytest.raises(OperatorRefused):
        offline_tool.arguments(None, task, None, {"tenant_id": "other"})


def test_card_with_zero_new_incidents_keeps_details_and_private_refs_off_wire(monkeypatch):
    from types import SimpleNamespace

    from aiops_diagnostics.common.config import Settings
    from aiops_diagnostics.data import offline_source
    from aiops_diagnostics.gateway.gateway_runtime import GatewayRuntime

    config = Settings()
    config.mysql.timezone = "UTC"
    detail = run([event("2026-10-30T00:00:00Z")])
    refs = {"devices": [["D", "S"]], "orders": []}
    monkeypatch.setattr(
        offline_source,
        "read",
        lambda *a, **k: (
            {"incidents": 0, "offline_devices": 0, "open_incidents": 0, "offline_minutes": 0},
            {**detail, "references": refs},
        ),
    )
    monkeypatch.setattr(offline_source, "require_references", lambda *a: None)
    runtime = SimpleNamespace(diagnostic_settings=config)
    source = SimpleNamespace(mysql=SimpleNamespace(count_devices=lambda *_: 1))
    result = GatewayRuntime._compute_card(runtime, "offline", source, ("S",), START, END, "zh", ("S",))
    assert result["device_offline"]["open_incident_list"]["total"] == 1
    assert result["incidents"] is None  # 原空摘要合同未改
    job = {"result": result}
    runtime.store = SimpleNamespace(get_standard_diagnosis=lambda *_: job)
    from contextlib import contextmanager

    from aiops_diagnostics.data import sources
    from operations.test_operator_cpms import scope

    @contextmanager
    def mysql(*a):
        yield source.mysql

    monkeypatch.setattr(sources, "scoped_mysql_source", mysql)
    observed = GatewayRuntime.get_operations_analysis(runtime, scope(), "an_test")
    assert "_offline_references" not in observed["result"]


def test_cached_offline_tool_rechecks_device_scope(tmp_path, monkeypatch):
    from aiops_diagnostics.data.sources import SourceError
    from aiops_diagnostics.operator import offline_tool, operator_cpms
    from operations.test_operator_agent import call_tool, finish
    from operations.test_operator_cpms import HEADERS, environment, terminal

    data = run([event("2026-10-30T00:00:00Z")])
    changed = False

    def require(*args):
        if changed:
            raise SourceError("device moved")

    monkeypatch.setattr(offline_tool, "require_current", require)
    monkeypatch.setattr(offline_tool, "query", lambda *a: (data, {"devices": [["D", "S"]], "orders": []}))
    with environment(tmp_path, monkeypatch) as f:
        f["candidate"]["enabled"].append(offline_tool.NAME)
        f["publish"](f["route"].route_revision)

        def dispatch(_, config, run_id):
            nonlocal changed
            _, headers, _ = call_tool(f, run_id, offline_tool.NAME)
            task = f["ctx"].operator_cpms.tasks[run_id]
            changed = True
            response = f["client"].get(
                task.proxy_url + "/v1/internal/operator-runs/" + run_id + "/cpms/aiops/device-offline",
                headers={"Authorization": "Bearer " + task.grant},
            )
            assert response.status_code == 403, response.text
            assert "PILE" not in response.text
            changed = False
            finish(f, run_id, headers, "记录只提供离线线索。", [offline_tool.NAME])

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        first = (
            f["client"]
            .post("/v1/assistant/questions", headers=HEADERS, json={"question": "最近离线情况"})
            .json()
        )
        assert terminal(f, first["qa_id"]).json()["status"] == "completed"
