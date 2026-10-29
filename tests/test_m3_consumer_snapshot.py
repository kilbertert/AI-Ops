"""同 run 消费者快照与最终授权；外部依赖复用合成契约夹具。"""

import hashlib
import json
from contextlib import contextmanager

import pytest
from execution.test_m2_close_contract import _app, _delegate, cpms_stub
from execution.test_m3_gateway_exec_config import _identity, _write_manifest

from aiops_diagnostics.execution import agent_runs


@contextmanager
def fixture(tmp_path, monkeypatch, *, m3=False, after=None):
    with cpms_stub() as upstream:
        f = _app(tmp_path, monkeypatch=monkeypatch, upstream_base=upstream["base"])
        if m3:
            manifest = _write_manifest(tmp_path)
            f["app"].state.gateway.settings.execution_manifest_file = str(manifest)

            def claim(rid):
                h = {
                    "Authorization": "Bearer node-token-alpha-0123456789",
                    "X-AI-Ops-Exec-Config": _identity(manifest),
                }
                assert (
                    f["client"]
                    .post(f"/v1/internal/charging-status/claims/{rid}", headers=h, json={})
                    .status_code
                    == 200
                )
                assert (
                    f["client"]
                    .post(
                        f"/v1/internal/charging-status/completions/{rid}",
                        headers=h,
                        json={"status": "completed"},
                    )
                    .status_code
                    == 200
                )
        else:
            claim = f["dispatcher"].claim

        def dispatch(rid):
            claim(rid)
            if after:
                after(f, rid)

        f["dispatcher"].claim = dispatch
        f["upstream"] = upstream
        yield f
        f["client"].close()


@pytest.mark.parametrize("m3", [False, True])
def test_same_run_snapshot_and_digest(tmp_path, monkeypatch, m3):
    with fixture(tmp_path, monkeypatch, m3=m3) as f:
        response = _delegate(f)
        assert response.status_code == 200, response.text
        body = response.json()
        run = f["store"].get(body["run_id"])
        assert body["charging_status"] == run.snapshot
        assert set(body["charging_status"]) == {
            "order_no",
            "status_code",
            "status_role",
            "observed_at",
            "source",
        }
        assert (
            hashlib.sha256(json.dumps(body["charging_status"], sort_keys=True).encode()).hexdigest()
            == body["result_sha256"]
        )
        assert body["charging_status"]["status_role"] == "charging"
        assert "DO-NOT-LEAK" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert f["upstream"]["calls"] == 1
        assert f["orders"].allow_count == 4
        assert "charging_status" not in run.receipt()


@pytest.mark.parametrize("unavailable", [False, True])
def test_authorization_changes_after_claim(tmp_path, monkeypatch, unavailable):
    def changed(f, rid):
        if unavailable:
            f["orders"].unavailable = True
        else:
            f["orders"].ownership["ORDER-A"] = "USER-B"

    with fixture(tmp_path, monkeypatch, after=changed) as f:
        r = _delegate(f)
        assert r.status_code == (503 if unavailable else 404)
        assert r.json()["error"]["code"] == (
            "ORDER_AUTHORIZATION_UNAVAILABLE" if unavailable else "ORDER_NOT_FOUND"
        )
        assert "charging_status" not in r.json() and "ORDER-A" not in r.text
        assert f["upstream"]["calls"] == 1


@pytest.mark.parametrize("change", ["revoke", "expire", "node", "environment", "agent", "expected-node"])
def test_final_authorization_interleaving(tmp_path, monkeypatch, change):
    def arrange(f, rid):
        def mutation():
            if change == "revoke":
                f["store"].revoke(rid, "review_revoke")
            elif change == "expire":
                expires = f["store"].peek(rid).expires_at
                monkeypatch.setattr(agent_runs, "_now", lambda: expires)
            else:
                d = json.loads(f["config"].read_text())
                if change == "node":
                    d["nodes"][0]["active"] = False
                elif change == "environment":
                    d["environment"] = "env-b"
                elif change == "agent":
                    d["snail_agent_id"] = 99
                else:
                    d["expected_node_id"] = "node-a2"
                f["config"].write_text(json.dumps(d))

        f["orders"].mutations_at[4] = mutation

    with fixture(tmp_path, monkeypatch, after=arrange) as f:
        response = _delegate(f)
        assert f["orders"].mutations == [4]
        assert response.status_code != 200
        assert "charging_status" not in response.json() and "ORDER-A" not in response.text
        rid = f["dispatcher"].calls[0][1]
        row = f["store"].peek(rid)
        if change == "revoke":
            assert row.state == "revoked" and row.reason == "review_revoke"
        if change == "expire":
            assert row.state == "expired" and row.reason == "ttl_elapsed"
        assert f["upstream"]["calls"] == 1


@pytest.mark.parametrize(
    "change", ["missing", "extra", "order", "status", "role", "time", "source", "invalid-json"]
)
def test_corrupt_snapshot_is_not_released(tmp_path, monkeypatch, change):
    def corrupt(f, rid):
        s = f["store"].peek(rid).snapshot
        if change == "missing":
            s.pop("source")
        elif change == "extra":
            s["plateNo"] = "DO-NOT-LEAK"
        elif change == "order":
            s["order_no"] = "ORDER-B"
        elif change == "status":
            s["status_code"] = True
        elif change == "role":
            s["status_role"] = "finished"
        elif change == "time":
            s["observed_at"] = "not-a-time"
        elif change == "source":
            s["source"] = "untrusted"
        with f["store"]._connection(write=True) as c:
            c.execute(
                "UPDATE agent_runs SET snapshot_json=? WHERE run_id=?",
                ("not-json" if change == "invalid-json" else json.dumps(s), rid),
            )

    with fixture(tmp_path, monkeypatch, after=corrupt) as f:
        r = _delegate(f)
        assert r.status_code == 502
        assert "charging_status" not in r.json() and "DO-NOT-LEAK" not in r.text


def test_unknown_status_stays_unknown(tmp_path, monkeypatch):
    def unknown(f, rid):
        s = f["store"].peek(rid).snapshot
        s.update(status_code="unrecognized", status_role="unknown")
        with f["store"]._connection(write=True) as c:
            c.execute("UPDATE agent_runs SET snapshot_json=? WHERE run_id=?", (json.dumps(s), rid))

    with fixture(tmp_path, monkeypatch, after=unknown) as f:
        r = _delegate(f)
        assert r.status_code == 200
        assert r.json()["charging_status"]["status_code"] == "unrecognized"
        assert r.json()["charging_status"]["status_role"] == "unknown"


def test_two_consumers_interleave_without_sharing_snapshots(tmp_path, monkeypatch):
    from execution.test_m2_close_contract import _scope

    nested = []

    def second_request(f, rid):
        if nested:
            return
        nested.append(None)
        f["resolver"].scope = _scope("USER-B")
        try:
            nested[0] = _delegate(f, order="ORDER-B")
        finally:
            f["resolver"].scope = _scope()

    with fixture(tmp_path, monkeypatch, after=second_request) as f:
        first = _delegate(f)
        assert first.status_code == nested[0].status_code == 200
        assert first.json()["run_id"] != nested[0].json()["run_id"]
        for response, order in [(first, "ORDER-A"), (nested[0], "ORDER-B")]:
            assert response.json()["charging_status"]["order_no"] == order
            row = f["store"].peek(response.json()["run_id"])
            assert row.snapshot == response.json()["charging_status"]
        assert f["upstream"]["calls"] == 2


@pytest.mark.parametrize(
    "column,value",
    [
        ("scope_fingerprint", "wrong"),
        ("observed_agent_version", "wrong"),
        ("exec_config_state", "expected"),
        ("actual_node_id", "node-a2"),
    ],
)
def test_stored_run_identity_mismatch_refuses_snapshot(tmp_path, monkeypatch, column, value):
    def change(f, rid):
        with f["store"]._connection(write=True) as c:
            c.execute(f"UPDATE agent_runs SET {column}=? WHERE run_id=?", (value, rid))

    with fixture(tmp_path, monkeypatch, m3=True, after=change) as f:
        response = _delegate(f)
        assert response.status_code == 403
        assert "charging_status" not in response.json()


def test_unreadable_binding_after_claim_is_unavailable(tmp_path, monkeypatch):
    def change(f, rid):
        f["config"].write_text("{broken")

    with fixture(tmp_path, monkeypatch, after=change) as f:
        response = _delegate(f)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AGENT_RUN_CONFIG_UNAVAILABLE"
        assert "charging_status" not in response.json()
