"""COMM-01：订单派生范围与拒绝；上游字段须另有实证，不能由fixture认定。"""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from aiops_diagnostics.operator import communication
from aiops_diagnostics.operator.operator_conversation import OperatorRefused

DETAIL = {
    "orderNo": "ORDER-A",
    "deviceCode": "PILE-A",
    "childDeviceCode": "PILE-A-2",
    "status": 1,
    "createdTime": "2026-11-09 12:00:00",
    "stopTime": "2026-11-09 13:00:00",
}
NOW = datetime(2026, 11, 10, 12, tzinfo=UTC)


def test_window_uses_parent_device_and_declared_timezone():
    value = communication.order_window(DETAIL, NOW, "Asia/Shanghai")
    assert value["device"] == "PILE-A"
    assert value["window_start"] == "2026-11-09T04:00:00+00:00"
    assert value["window_end"] == "2026-11-09T05:00:00+00:00"
    assert not value["window_truncated"]
    value = communication.order_window({**DETAIL, "status": 0, "stopTime": ""}, NOW, "Asia/Shanghai")
    assert value["window_end"] == "2026-11-10T04:00:00+00:00"
    assert value["order_window_end"] == "2026-11-10T12:00:00+00:00"
    assert value["window_truncated"]


@pytest.mark.parametrize(
    "changes,timezone",
    [
        ({"deviceCode": ""}, "UTC"),
        ({"deviceCode": "x';SELECT"}, "UTC"),
        ({"createdTime": "2026-11-31 12:00:00"}, "UTC"),
        ({"createdTime": "2026-11-11 12:00:00"}, "UTC"),
        ({"stopTime": "2026-11-08 12:00:00"}, "UTC"),
        ({"stopTime": "garbled", "status": 0}, "UTC"),
        ({"stopTime": ""}, "UTC"),
        ({"stopTime": "", "status": False}, "UTC"),
        ({}, ""),
        ({"createdTime": "2026-11-01 01:30:00"}, "America/New_York"),
    ],
)
def test_unbounded_or_ambiguous_window_refuses(changes, timezone):
    with pytest.raises(OperatorRefused, match="OPERATOR_EVIDENCE_MISSING"):
        communication.order_window({**DETAIL, **changes}, NOW, timezone)


def setup_query():
    detail = deepcopy(DETAIL)
    task = SimpleNamespace(
        delivered={communication.DETAIL},
        evidence={communication.DETAIL: {"data": detail}},
        order_site_id="SITE-A",
    )
    calls = []

    def authorize(*args, **kwargs):
        calls.append("authorize")

    def order(*args, **kwargs):
        calls.append("order")
        return {"data": {"chOrderInfoModel": deepcopy(DETAIL)}}

    owner = SimpleNamespace(
        authorize_execution=authorize,
        _authorized_order=order,
        context=SimpleNamespace(
            settings=SimpleNamespace(operator_tsdata_url="http://127.0.0.1:9999"),
            runtime=SimpleNamespace(
                diagnostic_settings=SimpleNamespace(mysql=SimpleNamespace(timezone="UTC"))
            ),
        ),
    )
    return owner, task, SimpleNamespace(run_id="run-a", order_no="ORDER-A"), calls


def test_detail_must_be_delivered_for_current_order():
    owner, task, run, calls = setup_query()
    task.delivered.clear()
    with pytest.raises(OperatorRefused, match="OPERATOR_DETAIL_REQUIRED"):
        communication.query(owner, task, run, None)
    assert calls == ["authorize"]
    task.delivered.add(communication.DETAIL)
    run.order_no = "ORDER-B"
    with pytest.raises(OperatorRefused, match="CPMS_SCOPE_MISMATCH"):
        communication.query(owner, task, run, None)
    assert "order" not in calls


def test_changed_order_cannot_reuse_delivered_device():
    owner, task, run, calls = setup_query()
    task.evidence[communication.DETAIL]["data"]["deviceCode"] = "OLD-PILE"
    with pytest.raises(OperatorRefused, match="OPERATOR_EVIDENCE_MISSING"):
        communication.query(owner, task, run, None)
    assert calls == ["authorize", "order"]


def test_missing_comm_table_is_distinct_from_empty_event_windows():
    owner, task, run, calls = setup_query()
    queries = []

    def read(url, payload, *, timeout):
        assert 0 < timeout <= communication.SOURCE_BUDGET_SECONDS
        assert url == "http://127.0.0.1:9999/tdadmin/data/query"
        queries.append(payload)
        if payload["table"] == "charging-pile_comm":
            raise OperatorRefused("TSDATA_UNAVAILABLE")
        return {"records": [], "count": 0}

    value = communication.query(owner, task, run, read)
    assert [(s["status"], s["total"]) for s in value["sources"]] == [
        ("unavailable", None),
        ("empty", 0),
        ("empty", 0),
        ("empty", 0),
        ("empty", 0),
    ]
    queries.sort(key=lambda p: [s[0] for s in communication.SOURCES].index(p["table"]))
    assert [p["deviceId"] for p in queries] == ["PILE-A", "PILE-A", "PILE-A-2", "PILE-A", "PILE-A-2"]
    assert queries[-1]["table"] == "charging-gun_fi"
    assert queries[-1]["fields"] == ["_ts", "device", "functionId"]
    assert value["sample_limit"] == 60
    assert all(p["pageSize"] == 12 and p["page"] == 1 for p in queries)
    assert all(p["start"] == "2026-11-09 12:00:00.000" for p in queries)
    assert all("decoded" not in p["fields"] for p in queries)
    assert calls == ["authorize", "order", "authorize", "order"]


def test_candidate_projection_drops_all_bodies_and_reports_truncation():
    bounds = communication.order_window(DETAIL, NOW, "UTC")

    def read(url, payload, *, timeout):
        return {
            "count": 18,
            "records": [
                {
                    "_ts": "2026-11-09T12:30:00Z",
                    "device": payload["deviceId"],
                    "event": "tx_data",
                    "functionId": "startCharging",
                    "decoded": {"orderNo": "OTHER-ORDER"},
                    "hex": "OTHER-BODY",
                    "output": "PRIVATE",
                    "direction": 1,
                    "code": "SECRET",
                }
            ],
        }

    value = communication.read_sources(DETAIL, bounds, "https://source.example", read)
    for source in value["sources"]:
        row = source["records"][0]
        assert set(row) == {"ts", "event", "association"}
        assert row["association"] == "device_window_candidate"
        assert source["truncated"] and source["total"] == 18
    assert "OTHER" not in str(value) and "SECRET" not in str(value)


@pytest.mark.parametrize(
    "bad",
    [
        {"records": [], "count": 1},
        {"records": [], "count": False},
        {"records": [None], "count": 1},
        {"records": [{"_ts": "2026-11-09T12:30:00Z", "device": "OTHER"}], "count": 1},
        {"records": [{"_ts": "2026-11-09T13:30:00Z", "device": "PILE-A"}], "count": 1},
    ],
)
def test_malformed_or_out_of_scope_rows_cannot_become_empty_success(bad):
    value = communication.read_sources(
        DETAIL,
        communication.order_window(DETAIL, NOW, "UTC"),
        "https://source.example",
        lambda *_, timeout: bad,
    )
    assert all(s["status"] == "unavailable" and s["total"] is None for s in value["sources"])


def test_event_name_is_not_an_arbitrary_payload():
    bounds = communication.order_window(DETAIL, NOW, "UTC")
    with pytest.raises(ValueError, match="invalid event name"):
        communication._records(
            {
                "records": [{"_ts": "2026-11-09T12:30:00Z", "device": "PILE-A", "event": {"raw": 1}}],
                "count": 1,
            },
            "PILE-A",
            bounds,
            "event",
        )


def test_scope_revoked_during_source_query_prevents_return():
    owner, task, run, _ = setup_query()
    calls = []

    def read(*args, timeout):
        def revoked(*args, **kwargs):
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")

        calls.append(True)
        if len(calls) == 5:
            owner.authorize_execution = revoked
        return {"records": [], "count": 0}

    with pytest.raises(OperatorRefused, match="OPERATOR_SCOPE_CHANGED"):
        communication.query(owner, task, run, read)


def test_sources_share_budget_and_busy_workers_cannot_accumulate(monkeypatch):
    from threading import Barrier, Event
    from time import monotonic

    entered, release, finished = Barrier(6), Event(), Barrier(6)
    calls = []
    monkeypatch.setattr(communication, "SOURCE_BUDGET_SECONDS", 0.2)

    def stalled(url, payload, *, timeout):
        calls.append((payload["table"], timeout))
        entered.wait(timeout=2)
        release.wait(timeout=2)
        finished.wait(timeout=2)
        return {"records": [], "count": 0}

    from concurrent.futures import ThreadPoolExecutor

    bounds = communication.order_window(DETAIL, NOW, "UTC")
    with ThreadPoolExecutor(max_workers=1) as caller:
        try:
            started = monotonic()
            future = caller.submit(communication.read_sources, DETAIL, bounds, "http://source", stalled)
            entered.wait(timeout=2)
            result = future.result(timeout=1)
            assert monotonic() - started < 1
            assert [s["table"] for s in result["sources"]] == [s[0] for s in communication.SOURCES]
            assert all(s["reason"] == "source_timeout" and s["total"] is None for s in result["sources"])
            assert len(calls) == 5 and all(0 < timeout <= 0.2 for _, timeout in calls)
            busy = communication.read_sources(DETAIL, bounds, "http://source", stalled)
            assert all(s["reason"] == "source_busy" and s["total"] is None for s in busy["sources"])
            assert len(calls) == 5
        finally:
            release.set()
            finished.wait(timeout=2)
    # 迟到结果不得修改已返回的缺口，更不能写成0条。
    assert all(s["records"] == [] and s["total"] is None for s in result["sources"])
