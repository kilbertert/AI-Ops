"""正式配置启用前沿原试跑执行；只消费目标已发布app级key。"""

import json
import time
from types import SimpleNamespace

from aiops_diagnostics.common.private_files import validate_private_file, write_private_text
from aiops_diagnostics.consumer.candidate_trial import consumer_identity
from aiops_diagnostics.consumer.consumer_trial import ConsumerTrial
from aiops_diagnostics.execution.chat_route_bindings import RouteBinding
from aiops_diagnostics.execution.run_deliverability import deliverable
from aiops_diagnostics.gateway import environment_business as business
from aiops_diagnostics.gateway import environment_registry
from aiops_diagnostics.publication.environment_release import read_record


def document(context, identifier):
    from aiops_diagnostics.gateway.environment_drafts import REFERENCE_PATTERN

    if not isinstance(identifier, str) or not REFERENCE_PATTERN.fullmatch(identifier):
        raise ValueError("invalid release operation")
    path = context.settings.data_home / "environment-publications" / (identifier + ".release.json")
    validate_private_file(path)
    result = json.loads(path.read_text())
    if (
        result["environment"] != context.settings.environment
        or result["id"] != identifier
        or result["expires_at"] <= time.time()
    ):
        raise ValueError("release scope changed")
    return result


def require_spec(doc, spec):
    business.valid_spec(spec)
    if any(spec[k] != doc[k] for k in ("id", "environment", "tenant_id", "case_ref", "installed")) or (
        spec["operator_app"] != doc["apps"]["operator"]["app_id"]
        or spec["consumer"]["app_id"] != doc["apps"]["consumer"]["app_id"]
        or spec["consumer"]["workspace_id"] != doc["workspace_id"]
    ):
        raise ValueError("release verification scope differs")


def trial_path(context, identifier, capability):
    return (
        context.settings.data_home
        / "environment-publications"
        / (identifier + "." + capability + ".trial.json")
    )


class LocalConsumer:
    def __init__(self, context, doc, spec, case):
        self.context, self.doc, self.spec, self.case = context, doc, spec, case
        self.caller = consumer_identity(context, case["consumer_session"])
        if (
            self.caller.delegated
            or self.caller.data_scope.type != "self"
            or not self.caller.subject.c_user_id
        ):
            raise ValueError("normal consumer identity required")
        self.scope = {
            "order_no": case["cases"]["consumer"]["order_no"],
            "app_id": doc["apps"]["consumer"]["app_id"],
        }
        self.until = time.monotonic() + 300
        self.snapshot = read_record(context.settings, doc["id"])
        self.closed = False
        self.configuration = self.current_configuration()

    def current_configuration(self):
        import hashlib
        from pathlib import Path

        files = [
            Path(self.context.settings.server_config_file),
            Path(self.context.settings.environment_registry_file),
            Path(self.context.settings.chat_dify_key_dir) / (self.scope["app_id"] + ".key"),
        ]
        if self.context.settings.knowledge_delegation_file:
            files.append(Path(self.context.settings.knowledge_delegation_file))
        return tuple(hashlib.sha256(path.read_bytes()).hexdigest() for path in files)

    def authorize(self, *, allow_closed=False):
        if self.closed and not allow_closed:
            raise ValueError("release trial data capability closed")
        return self.current()

    def current(self):
        if (
            document(self.context, self.doc["id"]) != self.doc
            or time.monotonic() >= self.until
            or read_record(self.context.settings, self.doc["id"]) != self.snapshot
            or self.current_configuration() != self.configuration
        ):
            raise ValueError("release trial changed")
        case, _ = business.load_case(self.context, self.spec, "consumer")
        if case != self.case:
            raise ValueError("release case changed")
        caller = consumer_identity(self.context, self.case["consumer_session"])
        if (
            caller.scope_fingerprint != self.caller.scope_fingerprint
            or caller.effective_tenant_id != self.doc["tenant_id"]
        ):
            raise ValueError("release consumer identity changed")
        body = environment_registry.load(self.context.settings.environment_registry_file)
        row = next(r for r in body["environments"] if r["id"] == self.doc["environment"])
        if any(t["id"] == self.doc["tenant_id"] and t["enabled"] for t in row["tenants"]):
            raise ValueError("release validation requires closed admission")
        return caller

    def require_order(self, order):
        caller = self.authorize()
        if order != self.scope["order_no"]:
            raise ValueError("release trial order mismatch")
        self.context.runtime._require_chat_order_visible(caller, order)

    def require_knowledge(self):
        from aiops_diagnostics.knowledge.delegated_knowledge_client import for_application

        self.authorize()
        for_application(
            self.context.runtime, self.doc["tenant_id"], self.scope["app_id"]
        ).list_knowledge_bases()

    def knowledge(self, query, trial_id):
        from aiops_diagnostics.consumer.consumer_documents import context_from_chunks
        from aiops_diagnostics.knowledge.delegated_knowledge_client import for_application

        self.require_knowledge()
        result = for_application(self.context.runtime, self.doc["tenant_id"], self.scope["app_id"]).search(
            tuple(self.doc["apps"]["consumer"]["settings"]["knowledge_base_ids"]),
            query,
            5,
            media_signer=self.context.runtime.media_signer,
            agent_version="consumer:release-trial",
            session_id=trial_id,
        )
        self.authorize()
        return context_from_chunks(result)

    def read(self, resource, order):
        from aiops_diagnostics.consumer.consumer_billing import read_billing_record
        from aiops_diagnostics.data.charging_status import read_charging_status

        self.require_order(order)
        body = environment_registry.load(self.context.settings.environment_registry_file)
        base = next(r for r in body["environments"] if r["id"] == self.doc["environment"])["cpms_base_url"]
        fn = read_charging_status if resource == "status" else read_billing_record
        result = fn(
            base, order, tenant_id=self.caller.effective_tenant_id, user_id=self.caller.subject.c_user_id
        )
        self.authorize()
        return {"status": "found", "snapshot": result, "order_ids": [order]}

    def close(self):
        self.closed = True


def start(context, payload):
    with context.consumer_candidate_trials.lock:
        return _start(context, payload)


def _start(context, payload):
    import yaml

    doc = document(context, payload["operation"])
    state = read_record(context.settings, doc["id"])
    if state["phase"] != "prepared":
        raise ValueError("release not prepared")
    spec = payload["spec"]
    require_spec(doc, spec)
    case, case_sha = business.load_case(context, spec, "consumer")
    adapter = LocalConsumer(context, doc, spec, case)
    adapter.authorize()
    app = doc["apps"]["consumer"]
    route = RouteBinding(
        doc["environment"],
        doc["tenant_id"],
        "consumer",
        "candidate_consumer_service",
        "dify",
        state["versions"]["consumer"]["agent_id"],
        state["versions"]["consumer"]["version_no"],
        app_id=app["app_id"],
        state="draft",
    )
    trial = ConsumerTrial(
        yaml.safe_dump({"workflow": app["workflow"]}),
        SimpleNamespace(app_id=app["app_id"], workspace_id=doc["workspace_id"]),
        route,
        app["settings"],
        case["cases"]["consumer"]["question"],
        draft=True,
        candidate=adapter,
        published=lambda: (app["app_id"], app["workflow_id"], app["key"]),
    )
    file = trial_path(context, doc["id"], "consumer")
    if file.exists():
        validate_private_file(file)
        old = json.loads(file.read_text())
        if old["spec"] != spec or old["case_sha256"] != case_sha or not old.get("result"):
            raise ValueError("formal consumer start unconfirmed or changed")
        return old["result"]
    intent = {"spec": spec, "case_sha256": case_sha}
    write_private_text(file, json.dumps(intent))
    result = context.runtime.start_trial(
        adapter.caller.subject.c_user_id, adapter.scope["order_no"], trial, "zh", run_context=adapter.caller
    )
    if result is None:
        raise ValueError("release trial busy")
    context.consumer_candidate_trials.leases[result["diagnosis_id"]] = adapter
    outcome = {
        "trial_id": result["diagnosis_id"],
        "case_sha256": case_sha,
        "expected_reference": case["cases"]["consumer"]["reference_id"],
        "scope_fingerprint": adapter.caller.scope_fingerprint,
    }
    write_private_text(file, json.dumps({**intent, "result": outcome}))
    return outcome


def read(context, payload):
    doc = document(context, payload["operation"])
    trial_id = payload["trial_id"]
    adapter = context.consumer_candidate_trials.leases.get(trial_id)
    if adapter is None or not isinstance(adapter, LocalConsumer) or adapter.doc["id"] != doc["id"]:
        raise ValueError("release trial unavailable")
    # 原ConsumerTrial会在结束关闭数据能力；完成读回核原scope/现行知识/原run。
    caller = adapter.current()
    context.runtime._require_chat_order_visible(caller, adapter.scope["order_no"])
    expected_reference = adapter.case["cases"]["consumer"]["reference_id"]
    if payload["expected_reference"] != expected_reference:
        raise ValueError("release reference changed")
    trial = context.runtime.get_trial(caller, trial_id)
    if trial is None:
        raise ValueError("release trial missing")
    if trial["status"] != "completed":
        return {"state": trial["status"]}
    run = context.dify_run_store.by_qa(trial_id)
    if (
        run is None
        or run.app_id != doc["apps"]["consumer"]["app_id"]
        or run.workflow_id != doc["apps"]["consumer"]["workflow_id"]
        or run.scope_fingerprint != caller.scope_fingerprint
        or not deliverable(run)
    ):
        raise ValueError("release run identity mismatch")
    calls = (context.dify_run_store.context(run.run_id) or {}).get("calls", [])
    sources = {
        s.get("retrieved_reference_id")
        for c in calls
        if c["tool"] == "knowledge" and (c.get("result") or {}).get("status") == "found"
        for s in c["result"].get("sources", [])
    }
    order = adapter.scope["order_no"]
    snapshot = context.dify_run_store.snapshot(run.run_id) or {}
    displayed = {b.get("reference_id") for b in snapshot.get("blocks", []) if b.get("kind") == "reference"}
    if expected_reference not in sources & displayed or not any(
        c["tool"] == "status" and order in (c.get("result") or {}).get("order_ids", []) for c in calls
    ):
        raise ValueError("formal business proof missing")
    from aiops_diagnostics.knowledge.delegated_knowledge_client import for_application

    for_application(
        context.runtime, doc["tenant_id"], doc["apps"]["consumer"]["app_id"]
    ).list_knowledge_bases()
    result = {
        "operation": doc["id"],
        "trial_id": trial_id,
        "state": "passed",
        "run_id": run.run_id,
        "workflow_id": run.workflow_id,
        "app_id": run.app_id,
        "observed_at": time.time(),
        "scope_fingerprint": run.scope_fingerprint,
    }
    write_private_text(
        context.settings.data_home / "environment-publications" / (doc["id"] + ".consumer-verification.json"),
        json.dumps(result),
    )
    return result


def complete(context, payload):
    from aiops_diagnostics.gateway.environment_trial import read as read_operator

    doc = document(context, payload["operation"])
    require_spec(doc, payload["spec"])
    path = (
        context.settings.data_home / "environment-publications" / (doc["id"] + ".consumer-verification.json")
    )
    validate_private_file(path)
    result = json.loads(path.read_text())
    if time.time() - result["observed_at"] > 300:
        raise ValueError("formal consumer evidence expired")
    original_time = result["observed_at"]
    case, _ = business.load_case(context, payload["spec"], "consumer")
    result = read(
        context,
        {
            "operation": doc["id"],
            "trial_id": result["trial_id"],
            "expected_reference": case["cases"]["consumer"]["reference_id"],
        },
    )
    if result["state"] != "passed":
        raise ValueError("formal consumer evidence unavailable")
    proofs = []
    for capability in ("order", "offline"):
        spec = payload["spec"]
        case, _ = business.load_case(context, spec, capability)
        trial_id = payload["trials"][capability]
        file = trial_path(context, doc["id"], capability)
        validate_private_file(file)
        intent = json.loads(file.read_text())
        if intent["spec"] != spec or intent["result"]["trial_id"] != trial_id:
            raise ValueError("operator run belongs to another operation")
        if (
            read_operator(context, {"app_id": doc["apps"]["operator"]["app_id"], "trial_id": trial_id})[
                "status"
            ]
            != "completed"
        ):
            raise ValueError("formal operator evidence incomplete")
        proofs.append(
            business.operator_evidence(context, capability, trial_id, case["cases"][capability], spec)
        )
    result.update(
        operator_runs=[p["run_id"] for p in proofs],
        observed_at=original_time,
        spec=payload["spec"],
        trials=payload["trials"],
    )
    write_private_text(
        context.settings.data_home / "environment-publications" / (doc["id"] + ".verification.json"),
        json.dumps(result),
    )
    return result


def start_operator(context, payload):
    with context.consumer_candidate_trials.lock:
        return _start_operator(context, payload)


def _start_operator(context, payload):
    from aiops_diagnostics.gateway import environment_trial

    doc = document(context, payload["operation"])
    require_spec(doc, payload["spec"])
    capability = payload["capability"]
    if capability not in {"order", "offline"}:
        raise ValueError("invalid formal capability")
    case, digest = business.load_case(context, payload["spec"], capability)
    file = trial_path(context, doc["id"], capability)
    if file.exists():
        validate_private_file(file)
        previous = json.loads(file.read_text())
        if (
            previous["spec"] != payload["spec"]
            or previous["case_sha256"] != digest
            or not previous.get("result")
        ):
            raise ValueError("formal operator start unconfirmed or changed")
        return previous["result"]
    intent = {"spec": payload["spec"], "case_sha256": digest}
    write_private_text(file, json.dumps(intent))
    result = environment_trial.start(
        context,
        {
            "app_id": doc["apps"]["operator"]["app_id"],
            "runtime_sha256": doc["installed"]["runtime_sha256"],
            "company_token": case["operator_token"],
            "question": case["cases"][capability]["question"],
        },
    )
    write_private_text(file, json.dumps({**intent, "result": result}))
    return result
