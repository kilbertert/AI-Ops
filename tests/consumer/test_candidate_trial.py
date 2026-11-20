"""D35：单候选权限与原回调互斥；越权不得读取业务。"""

import copy
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from aiops_diagnostics.consumer import candidate_trial as target
from aiops_diagnostics.execution.agent_runs import scope_to_json
from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

ORDER = "2117453316060192769"


def caller(tenant="same", user="customer"):
    subject = SubjectRecord(b_user_id="c:" + user, c_user_id=user, tenant_id=tenant)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=tenant,
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset(),
    )


def scope():
    return dict(
        environment="124",
        tenant_id="same",
        workspace_id="workspace",
        app_id="app",
        knowledge_base_id="library",
        task_id="task_" + "a" * 32,
        plan_id="plan_" + "b" * 32,
        workflow_sha256="c" * 64,
        order_no=ORDER,
    )


@pytest.fixture
def leases(monkeypatch):
    current = {"caller": caller(), "snapshot": ("https://cpms.example", ("config", "build")), "owned": True}
    reads = []

    def order_visible(who, order):
        if not current["owned"] or who != current["caller"] or order != ORDER:
            raise ValueError("not visible")

    context = SimpleNamespace(runtime=SimpleNamespace(_require_chat_order_visible=order_visible))
    monkeypatch.setattr(target, "consumer_identity", lambda *_: current["caller"])
    monkeypatch.setattr(target, "installed", lambda *_: current["snapshot"])
    monkeypatch.setattr(
        "aiops_diagnostics.data.charging_status.read_charging_status",
        lambda base, order, **kw: reads.append((base, order, kw)) or {"order_no": order, "status_code": 3},
    )
    manager = target.CandidateLeases(context)
    return manager, current, reads


def open_lease(manager):
    return manager.open({"candidate": scope(), "third_session": "synthetic-fresh-session"})["token"]


def call(manager, token, action, **extra):
    return manager.call(token, {"candidate": scope(), "action": action, **extra})


def test_one_claim_reads_exact_own_order_and_completion_closes_reads(leases):
    manager, _, reads = leases
    token = open_lease(manager)
    assert "session" not in str(call(manager, token, "check"))
    with pytest.raises(ValueError):
        call(manager, token, "read", resource="status", order_no=ORDER)
    call(manager, token, "claim")
    with pytest.raises(ValueError):
        call(manager, token, "claim")
    result = call(manager, token, "read", resource="status", order_no=ORDER)
    assert result["snapshot"]["order_no"] == ORDER and len(reads) == 1
    assert reads[0][2] == {"tenant_id": "same", "user_id": "customer"}
    call(manager, token, "close")
    assert call(manager, token, "check")["state"] == "closed"
    with pytest.raises(ValueError):
        call(manager, token, "read", resource="status", order_no=ORDER)
    assert len(reads) == 1


@pytest.mark.parametrize(
    "change", ["tenant", "user", "owner", "config", "expired", "app", "kb", "order", "token"]
)
def test_changed_scope_expiry_and_wrong_object_refuse_without_reading(leases, change):
    manager, current, reads = leases
    token = open_lease(manager)
    call(manager, token, "claim")
    body = {"candidate": scope(), "action": "read", "resource": "status", "order_no": ORDER}
    if change == "tenant":
        current["caller"] = caller("other")
    elif change == "user":
        current["caller"] = caller(user="other")
    elif change == "owner":
        current["owned"] = False
    elif change == "config":
        current["snapshot"] = ("https://other.example", ("new",))
    elif change == "expired":
        next(iter(manager.leases.values())).until = time.monotonic() - 1
    elif change == "app":
        body["candidate"]["app_id"] = "other"
    elif change == "kb":
        body["candidate"]["knowledge_base_id"] = "other"
    elif change == "order":
        body["order_no"] = "2117453316060192770"
    else:
        token += "wrong"
    with pytest.raises(ValueError):
        manager.call(token, body)
    assert reads == []


def test_revocation_during_upstream_read_refuses_result(leases, monkeypatch):
    manager, current, _ = leases
    token = open_lease(manager)
    call(manager, token, "claim")

    def read(*_, **kw):
        current["owned"] = False
        return {"order_no": ORDER}

    monkeypatch.setattr("aiops_diagnostics.data.charging_status.read_charging_status", read)
    with pytest.raises(ValueError):
        call(manager, token, "read", resource="status", order_no=ORDER)


def test_failed_close_cannot_resurrect_reading_after_identity_recovers(leases):
    manager, current, reads = leases
    token = open_lease(manager)
    call(manager, token, "claim")
    current["caller"] = caller(user="revoked")
    with pytest.raises(ValueError):
        call(manager, token, "close")
    current["caller"] = caller()
    with pytest.raises(ValueError):
        call(manager, token, "read", resource="status", order_no=ORDER)
    assert reads == []


def test_operator_or_wrong_tenant_cannot_open_consumer_candidate(leases):
    manager, current, reads = leases
    current["caller"] = caller("other")
    with pytest.raises(ValueError):
        open_lease(manager)
    current["caller"] = replace(caller(), data_scope=DataScope(type="all"), scope_fingerprint="")
    with pytest.raises(ValueError):
        open_lease(manager)
    assert manager.leases == {} and reads == []


def test_remote_adapter_rechecks_candidate_and_closes_without_moving_session(leases, monkeypatch):
    from aiops_diagnostics.dify import environment_consumer_trial as control

    manager, _, reads = leases
    token = open_lease(manager)
    state = {
        "observed": (dict(gateway_url="https://target.example"), {}, None, "frozen", ("e", "t", "w", "k"))
    }
    monkeypatch.setattr(control, "observed", lambda *_: copy.deepcopy(state["observed"]))
    requests = []

    def send(request, **kwargs):
        import json

        requests.append(request)
        assert "synthetic-fresh-session" not in request.body.decode()
        return manager.call(request.headers["Authorization"][7:], json.loads(request.body))

    monkeypatch.setattr(control, "request_json", send)
    adapter = control.RemoteCandidate(SimpleNamespace(), scope(), token)
    assert adapter.caller.scope_fingerprint == caller().scope_fingerprint
    assert adapter.read("status", ORDER)["status"] == "found" and len(reads) == 1
    adapter.close()
    adapter.authorize(allow_closed=True)
    with pytest.raises(ValueError):
        adapter.authorize()
    with pytest.raises(ValueError):
        control.RemoteCandidate(SimpleNamespace(), scope(), token)
    assert all(r.follow_redirects is False and r.max_read_bytes == 131072 for r in requests)


def test_candidate_evidence_uses_target_authorization_but_normal_stays_disabled(leases):
    from aiops_diagnostics.consumer import consumer_agent

    manager, _, _ = leases
    token = open_lease(manager)
    call(manager, token, "claim")
    adapter = SimpleNamespace(
        authorize=lambda: call(manager, token, "check"),
        require_order=lambda order: (
            call(manager, token, "check") if order == ORDER else (_ for _ in ()).throw(ValueError())
        ),
    )
    runtime = SimpleNamespace(gateway_settings=SimpleNamespace(consumer_chatflow_enabled=False))
    binding = SimpleNamespace(callback_version=6)
    evidence = {"conversation": {"selected_order": ORDER}}
    consumer_agent.require_evidence(
        runtime, caller(), binding, evidence, trial=SimpleNamespace(candidate=adapter)
    )
    with pytest.raises(consumer_agent.ChatRunUnavailable):
        consumer_agent.require_evidence(runtime, caller(), binding, evidence)
    assert scope_to_json(caller())
