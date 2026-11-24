"""共享控制端执行停用客服候选；目标端会话不进入控制端。"""

import json
import threading
import time

from aiops_diagnostics.common.bounded_http import ErrorMapping, RequestSpec, request_json
from aiops_diagnostics.consumer.candidate_trial import PREFIX, descriptor
from aiops_diagnostics.consumer.consumer_publication import frozen_settings, workflow_of
from aiops_diagnostics.consumer.consumer_trial import ConsumerTrial
from aiops_diagnostics.dify import candidate_knowledge, environment_consumer
from aiops_diagnostics.dify.dify_console_admin import ConsoleApiError, ConsoleApiSettings, DifyConsoleAdmin
from aiops_diagnostics.dify.dify_dsl_pull import DifyDslClient, DifyDslSource, workflow_content
from aiops_diagnostics.execution.agent_runs import scope_from_json
from aiops_diagnostics.execution.chat_route_bindings import RouteBinding
from aiops_diagnostics.execution.run_deliverability import deliverable
from aiops_diagnostics.gateway import environment_drafts as drafts
from aiops_diagnostics.gateway import environment_plans as plans
from aiops_diagnostics.gateway import environment_tasks as tasks
from aiops_diagnostics.gateway import gateway_db


def candidate_workflow_sha(dsl):
    content = workflow_content(dsl)
    for variable in content.get("environment_variables", []):
        # Dify导出补回变量自身的selector；只忽略这项已知的表示差异。
        if variable.get("selector") == ["env", variable.get("name")]:
            variable.pop("selector")
    return workflow_of(content)


def observed(context, value):
    scope, settings = descriptor(value), context.settings
    path = settings.environment_management_registry_file or settings.environment_registry_file
    # 计划read持同一个flock，必须在外层取得，再在临界区核当前摘要。
    with gateway_db.connection(drafts._database(path), readonly=True) as conn:
        request = json.loads(tasks._row(conn, scope["task_id"])["request"])
    plan = plans.read(
        path, tuple(request["identity"]), scope["plan_id"], settings.environment_onboarding_file
    )
    with drafts._locked(path), gateway_db.connection(drafts._database(path), readonly=True) as conn:
        row = tasks._row(conn, scope["task_id"])
        request, result = json.loads(row["request"]), json.loads(row["result"])
        tasks._authorize(path, tuple(request["identity"]), scope["environment"])
        renewal = scope["plan_id"] != request["plan"]["id"]
        tasks._current(path, request, settings.environment_onboarding_file, reconciling=renewal)
        if (
            plan["state"] == "expired"
            or plan["digest"] != request["plan"]["digest"]
            or (row["state"] if renewal else tasks._public(row)["state"]) != "observed"
            or row["cancel"]
            or request.get("action") != "consumer-prepare"
            or request["plan"]["environment"] != scope["environment"]
            or any(request[k] != scope[k] for k in ("tenant_id", "workspace_id", "knowledge_base_id"))
            or request.get("resources", {}).get("expected_app_id") != scope["app_id"]
            or result.get("app_id") != scope["app_id"]
        ):
            raise ValueError("candidate observation mismatch")
        proposed = next(
            r for r in drafts._rows(drafts._database(path)) if r["environment"] == scope["environment"]
        )
        draft = json.loads(proposed["body"])
    knowledge_scope = tuple(
        scope[k] for k in ("environment", "tenant_id", "workspace_id", "knowledge_base_id")
    )
    candidate_knowledge.active(settings, knowledge_scope)
    if request["binding"] != environment_consumer.binding(settings, request["consumer_source"]):
        raise ValueError("candidate template changed")
    console = DifyConsoleAdmin(
        ConsoleApiSettings(settings.dify_console_base_url, settings.dify_console_api_key)
    )
    app = console._call(scope["workspace_id"], "/console/api/apps/" + scope["app_id"])
    if (
        app.get("id") != scope["app_id"]
        or app.get("mode") != "advanced-chat"
        or app.get("enable_api") is not False
        or app.get("enable_site") is not False
        or app.get("workflow")
        or console._call(scope["workspace_id"], "/console/api/apps/" + scope["app_id"] + "/api-keys").get(
            "data"
        )
        != []
    ):
        raise ValueError("candidate entries changed")
    environment_consumer.verify(console, settings, request, knowledge_scope, scope["app_id"])
    source = DifyDslSource(
        settings.dify_console_base_url, scope["app_id"], settings.dify_console_api_key, scope["workspace_id"]
    )
    dsl = DifyDslClient(source).export_dsl()
    if candidate_workflow_sha(dsl) != scope["workflow_sha256"]:
        raise ValueError("candidate workflow changed")
    return draft, request, source, dsl, knowledge_scope


class RemoteCandidate:
    def __init__(self, context, scope, token):
        if not isinstance(token, str) or not token.startswith(PREFIX) or len(token) > 128:
            raise ValueError("candidate token required")
        self.context, self.scope, self.token = context, descriptor(scope), token
        self.cancelled = False
        self.until = time.monotonic() + 300
        self.snapshot = observed(context, scope)
        self.caller = scope_from_json(self.call("check")["scope_json"])
        if (
            self.caller.effective_tenant_id != scope["tenant_id"]
            or self.caller.delegated
            or self.caller.data_scope.type != "self"
            or not self.caller.subject.c_user_id
        ):
            raise ValueError("candidate consumer required")
        self.call("claim")

    def call(self, action, **kwargs):
        def refused(_):
            return ValueError("candidate target refused or unavailable")

        return request_json(
            RequestSpec(
                self.snapshot[0]["gateway_url"] + "/v1/internal/consumer-data/agent",
                method="POST",
                headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
                body=json.dumps({"action": action, "candidate": self.scope, **kwargs}).encode(),
                timeout=25,
                max_read_bytes=131072,
                follow_redirects=False,
            ),
            mapping=ErrorMapping(
                **dict.fromkeys(
                    ("auth_rejected", "http_error", "unavailable", "invalid_body", "invalid_envelope"),
                    refused,
                )
            ),
        )

    def authorize(self, *, allow_closed=False):
        from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

        if self.cancelled or time.monotonic() >= self.until:
            raise ValueError("candidate expired or cancelled")
        try:
            snapshot = observed(self.context, self.scope)
        except ConsoleApiError as exc:
            if exc.status is not None and exc.status < 500:
                raise ValueError("candidate observation refused") from exc
            raise ChatRunUnavailable(
                "CANDIDATE_OBSERVATION_UNAVAILABLE", "候选权限来源暂不可用，请稍后再试", http_status=503
            ) from exc
        if self.cancelled or time.monotonic() >= self.until or snapshot != self.snapshot:
            raise ValueError("candidate expired or changed")
        current = self.call("check")
        if (
            self.cancelled
            or time.monotonic() >= self.until
            or current.get("candidate") != self.scope
            or scope_from_json(current["scope_json"]).scope_fingerprint != self.caller.scope_fingerprint
            or current.get("state") not in ({"claimed", "closed"} if allow_closed else {"claimed"})
        ):
            raise ValueError("candidate identity changed")
        return self.caller

    def require_order(self, order):
        if not order or order != self.scope["order_no"]:
            raise ValueError("candidate order not selected")
        self.authorize()

    def require_knowledge(self):
        self.authorize()
        candidate_knowledge._owned(self.context, self.snapshot[4])

    def knowledge(self, query, trial_id):
        from aiops_diagnostics.consumer.consumer_documents import context_from_chunks
        from aiops_diagnostics.knowledge.knowledge_retrieval import KnowledgeSearchGuard

        self.require_knowledge()
        client, _ = candidate_knowledge._owned(self.context, self.snapshot[4])
        result = KnowledgeSearchGuard(
            client,
            tenant_id=candidate_knowledge.namespace(self.snapshot[4]),
            agent_version=candidate_knowledge.MEDIA_PREFIX
            + candidate_knowledge.knowledge_id(self.snapshot[4]),
            knowledge_base_ids=(self.scope["knowledge_base_id"],),
            media_signer=self.context.runtime.media_signer,
            session_id=trial_id,
            max_results=5,
            max_calls=1,
        ).search(query)
        self.authorize()
        return context_from_chunks(result)

    def read(self, resource, order):
        self.require_order(order)
        result = self.call("read", resource=resource, order_no=order)
        self.authorize()
        return result

    def close(self):
        self.call("close")


class CandidateTrials:
    def __init__(self, context):
        self.context, self.leases, self.lock = context, {}, threading.RLock()

    def describe(self, payload):
        if set(payload) != {"task_id", "order_no"}:
            raise ValueError("candidate task required")
        settings = self.context.settings
        path = settings.environment_management_registry_file or settings.environment_registry_file
        with drafts._locked(path), gateway_db.connection(drafts._database(path), readonly=True) as conn:
            row = tasks._row(conn, payload["task_id"])
            request, result = json.loads(row["request"]), json.loads(row["result"])
        scope = {k: request[k] for k in ("tenant_id", "workspace_id", "knowledge_base_id")}
        workflow = environment_consumer.document(request["consumer_source"], scope["knowledge_base_id"])
        scope.update(
            environment=request["plan"]["environment"],
            app_id=result["app_id"],
            task_id=payload["task_id"],
            plan_id=request["plan"]["id"],
            order_no=payload["order_no"],
            workflow_sha256=candidate_workflow_sha(__import__("yaml").safe_dump(workflow)),
        )
        observed(self.context, scope)
        return {"candidate": descriptor(scope), "scope": "candidate-only", "activated": False}

    def start(self, payload):
        if (
            set(payload) != {"candidate", "token", "question"}
            or not isinstance(payload["question"], str)
            or not 1 <= len(payload["question"]) <= 4000
        ):
            raise ValueError("invalid candidate trial")
        with self.lock:
            self.leases = {k: v for k, v in self.leases.items() if v.until > time.monotonic()}
            if len(self.leases) >= 20:
                raise ValueError("candidate trials busy")
        adapter = RemoteCandidate(self.context, payload["candidate"], payload["token"])
        try:
            _, _, source, dsl, _ = adapter.snapshot
            scope = adapter.scope
            target = {
                **{k: scope[k] for k in ("environment", "tenant_id", "app_id")},
                "knowledge_base_ids": [scope["knowledge_base_id"]],
                "source": "managed",
                "live_order_status": True,
                "live_billing_record": True,
            }
            from aiops_diagnostics.consumer.consumer_agent_flow import publication_settings

            content = publication_settings(
                workflow_content(dsl),
                frozen_settings(target),
                [{"knowledge_base_id": scope["knowledge_base_id"], "audience": "customer"}],
            )
            route = RouteBinding(
                scope["environment"],
                scope["tenant_id"],
                "consumer",
                "candidate_consumer_service",
                "dify",
                "",
                0,
                app_id=scope["app_id"],
                state="draft",
            )
            draft = ConsumerTrial(
                dsl, source, route, content, payload["question"], draft=True, candidate=adapter
            )
            trial = self.context.runtime.start_trial(
                adapter.caller.subject.b_user_id, scope["order_no"], draft, "zh", run_context=adapter.caller
            )
            if trial is None:
                raise ValueError("candidate trial busy")
        except Exception:
            adapter.close()
            raise
        with self.lock:
            self.leases[trial["diagnosis_id"]] = adapter
        expiry = threading.Timer(300, self.forget, args=(trial["diagnosis_id"],))
        expiry.daemon = True
        expiry.start()
        return {
            "trial_id": trial["diagnosis_id"],
            "status": trial["status"],
            "scope": "candidate-only",
            "activated": False,
        }

    def forget(self, identifier):
        with self.lock:
            self.leases.pop(identifier, None)

    def authorize_media(self, grant, knowledge_scope):
        with self.lock:
            adapter = self.leases.get(grant.session_id)
        if adapter is None or adapter.cancelled or knowledge_scope != adapter.snapshot[4]:
            raise ValueError("candidate media revoked")
        caller = adapter.authorize(allow_closed=True)
        trial = self.context.runtime.get_trial(caller, grant.session_id)
        if trial is None or trial["status"] != "completed":
            raise ValueError("candidate media not deliverable")
        with self.lock:
            if (
                self.leases.get(grant.session_id) is not adapter
                or adapter.cancelled
                or time.monotonic() >= adapter.until
            ):
                raise ValueError("candidate media revoked")

    def read(self, payload, *, cancel=False):
        if set(payload) != {"trial_id"} or payload["trial_id"] not in self.leases:
            raise ValueError("candidate trial not found")
        adapter = self.leases[payload["trial_id"]]
        if cancel:
            adapter.cancelled = True
            try:
                self.context.runtime.cancel_trial(adapter.caller, payload["trial_id"])
            finally:
                adapter.close()
        if adapter.cancelled:
            raise ValueError("candidate trial cancelled")
        caller = adapter.authorize(allow_closed=True)
        trial = self.context.runtime.get_trial(caller, payload["trial_id"])
        if trial is None:
            raise ValueError("candidate trial missing")
        if trial["status"] == "completed":
            run = self.context.dify_run_store.by_qa(payload["trial_id"])
            snapshot = self.context.dify_run_store.snapshot(run.run_id) if run else None
            report = (trial.get("result") or {}).get("report", {})
            if (
                run is None
                or not deliverable(run)
                or not snapshot
                or run.scope_fingerprint != caller.scope_fingerprint
                or report.get("source_app_id") != adapter.scope["app_id"]
                or report.get("summary")
                != "\n\n".join(b["text"] for b in snapshot["blocks"] if b.get("kind") == "text")
            ):
                raise ValueError("candidate result unavailable")
        adapter.authorize(allow_closed=True)
        return {
            "trial_id": payload["trial_id"],
            "status": trial["status"],
            "result": trial.get("result"),
            "error_code": trial.get("error_code"),
            "scope": "candidate-only",
            "activated": False,
        }


def run_started(context, payload):
    """窗口起点探针：被测 run 行是否已落库。只读，不做过期收敛，不触发任何授权。"""
    if set(payload) != {"trial_id"} or not isinstance(payload["trial_id"], str):
        raise ValueError("candidate run probe invalid")
    return {"run_started": context.dify_run_store.by_qa(payload["trial_id"]) is not None}


def validation_evidence(context, payload):
    """只有原候选交付门通过后的真实工具/引用可成为业务技术验证证据。"""
    if set(payload) != {"trial_id", "expected_reference", "expected_scope", "case_sha256"}:
        raise ValueError("validation evidence request invalid")
    result = context.consumer_candidate_trials.read({"trial_id": payload["trial_id"]})
    if result["status"] != "completed":
        return {"status": result["status"], "error_code": result.get("error_code")}
    adapter = context.consumer_candidate_trials.leases[payload["trial_id"]]
    if adapter.caller.scope_fingerprint != payload["expected_scope"]:
        raise ValueError("validation consumer identity changed")
    run = context.dify_run_store.by_qa(payload["trial_id"])
    calls = (context.dify_run_store.context(run.run_id) or {}).get("calls", [])
    order = adapter.scope["order_no"]
    status = [
        c
        for c in calls
        if c.get("tool") == "status"
        and (c.get("result") or {}).get("status") == "found"
        and order in c["result"].get("order_ids", [])
    ]
    sources = [
        s
        for c in calls
        if c.get("tool") == "knowledge" and (c.get("result") or {}).get("status") == "found"
        for s in c["result"].get("sources", [])
    ]
    references = {s.get("retrieved_reference_id") for s in sources}
    snapshot = context.dify_run_store.snapshot(run.run_id) or {}
    displayed = {b.get("reference_id") for b in snapshot.get("blocks", []) if b.get("kind") == "reference"}
    if not order or not status or payload["expected_reference"] not in references & displayed:
        from aiops_diagnostics.gateway.environment_business import ValidationRefused

        raise ValidationRefused("ENVIRONMENT_VALIDATION_CONSUMER_EVIDENCE_REQUIRED")
    return {
        "status": "completed",
        "capability": "consumer",
        "state": "passed",
        "business_accuracy": "unconfirmed",
        "case_sha256": payload["case_sha256"],
        "observed_at": time.time(),
        "evidence": {
            "trial_id": payload["trial_id"],
            "run_id": run.run_id,
            "tools": ["knowledge", "status"],
            "objects": [order, payload["expected_reference"]],
            "scope_fingerprint": run.scope_fingerprint,
            "workflow_id": run.workflow_id,
            "app_id": adapter.scope["app_id"],
        },
    }
