"""管家节点任务：凭据留在 Gateway，按单次任务转发固定 CPMS 只读接口。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from aiops_diagnostics.async_job_lifecycle import QUESTION
from aiops_diagnostics.bounded_http import ErrorMapping, RequestSpec, request_json
from aiops_diagnostics.execution import chat_execution, execution_manifest
from aiops_diagnostics.execution.agent_runs import AgentRunConfig, SnailDispatcher
from aiops_diagnostics.identity.query_scope import resolve_query_scope
from aiops_diagnostics.identity.scope_context import is_tenant_level_account

SCENARIO = "operator_order_diagnosis"
PREFIX = "operator-cpms-node:"
TOOL_NAMES = chat_execution.OPERATOR_TOOL_NAMES
PATHS = {
    TOOL_NAMES[0]: "/chOrderInfo/getOrderInfoDetail/",
    TOOL_NAMES[1]: "/iotChargingDevice/getChargingMonitor/",
}


class OperatorRefused(RuntimeError):
    pass


def operator_scope(caller):
    if not caller.subject.b_user_id or caller.delegated or caller.data_scope.type not in {"all", "organ"}:
        raise OperatorRefused("OPERATOR_SITE_SCOPE_REQUIRED")
    scope = resolve_query_scope(caller)
    if scope.site_ids == () or (
        scope.site_ids is None and not is_tenant_level_account(caller.subject.user_type)
    ):
        raise OperatorRefused("OPERATOR_SITE_SCOPE_REQUIRED")
    return scope


@dataclass(repr=False)
class ActiveTask:
    caller: object
    binding: object
    authorize: object
    bearer: str
    base_url: str
    proxy_url: str
    order_site_id: str
    grant: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    evidence: dict = field(default_factory=dict)
    delivered: set = field(default_factory=set)
    conversation_turn: tuple | None = None
    question: str = ""
    language: str = "zh"
    trial: bool = False
    lock: object = field(default_factory=threading.RLock)


class OperatorCpms:
    def __init__(self, context):
        self.context = context
        self.tasks = {}
        self.lock = threading.RLock()

    def console_identity(self, session):
        settings = self.context.settings
        if not settings.operator_trial_login_url or not settings.operator_trial_key or not session:
            raise OperatorRefused("OPERATOR_TRIAL_LOGIN_REQUIRED")

        def unavailable(_):
            return OperatorRefused("OPERATOR_TRIAL_LOGIN_REQUIRED")

        from aiops_diagnostics.gateway_config import canonical_gateway_url

        result = request_json(
            RequestSpec(
                url=canonical_gateway_url(settings.operator_trial_login_url)
                + "/__aiops/internal/operator-token",
                method="POST",
                headers={
                    "Authorization": "Bearer " + settings.operator_trial_key,
                    "Content-Type": "application/json",
                },
                body=json.dumps({"session": session}).encode(),
                timeout=5,
                max_read_bytes=20000,
                follow_redirects=False,
            ),
            mapping=ErrorMapping(
                auth_rejected=unavailable,
                http_error=unavailable,
                unavailable=unavailable,
                invalid_body=unavailable,
                invalid_envelope=unavailable,
            ),
        )
        if not isinstance(result, dict) or any(
            not isinstance(result.get(k), str) or not result[k] for k in ("token", "user_id", "tenant_id")
        ):
            raise OperatorRefused("OPERATOR_TRIAL_LOGIN_REQUIRED")
        return result

    def refresh_scope(self, caller):
        from dataclasses import replace

        from aiops_diagnostics.identity.scope_context import DataScope

        if operator_scope(caller).site_ids is None:
            return caller
        directory = getattr(self.context.runtime, "_trial_operator_scope", None)
        if directory is None:
            raise OperatorRefused("OPERATOR_SITE_SCOPE_REQUIRED")
        try:
            scope = directory.site_scope_by_b_user_id(caller.subject.b_user_id, caller.effective_tenant_id)
        except Exception as exc:  # noqa: BLE001 - 授权源失败不沿用旧范围。
            raise OperatorRefused("OPERATOR_SCOPE_UNAVAILABLE") from exc
        if scope.tenant_id != caller.effective_tenant_id or not scope.site_ids:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        return replace(
            caller, data_scope=DataScope(type="organ", site_ids=tuple(scope.site_ids)), scope_fingerprint=""
        )

    def authorize_readback(self, caller, qa_id, bearer):
        caller = self.refresh_scope(caller)
        run = self.context.agent_run_store.by_qa(qa_id)
        if run is None or run.scope_fingerprint != caller.scope_fingerprint:
            raise OperatorRefused("QA_NOT_FOUND")
        from aiops_diagnostics.execution.node_publication import targets

        binding = chat_execution.binding_for_key(self.context.settings, run.chat_binding)
        if binding is None or binding.route_revision != run.route_revision:
            raise OperatorRefused("OPERATOR_RESULT_RETRACTED")
        target = next(
            (
                t
                for t in targets(self.context.settings.chat_node_targets_file)
                if t.entry == "operator"
                and t.app_id == binding.app_id
                and t.tenant_id == caller.effective_tenant_id
                and t.environment == run.environment
            ),
            None,
        )
        if target is None:
            raise OperatorRefused("OPERATOR_TARGET_MISSING")
        body = cpms_get(
            target.cpms_base_url + PATHS[TOOL_NAMES[0]] + run.order_no, bearer, caller.effective_tenant_id
        )
        validate_body(
            body,
            caller,
            run.order_no,
            TOOL_NAMES[0],
        )
        if body["data"]["siteId"] != run.snapshot["evidence"][TOOL_NAMES[0]]["data"]["siteId"]:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        refreshed = self.refresh_scope(caller)
        if refreshed.scope_fingerprint != caller.scope_fingerprint:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        return self.result(caller, qa_id)

    def start(
        self,
        caller,
        binding,
        order,
        question,
        *,
        bearer,
        authorize,
        base,
        proxy,
        registration,
        language="zh",
        trial_manifest=None,
    ):
        conversation_turn = registration.conversation_turn
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", order):
            raise OperatorRefused("ORDER_INVALID")
        operator_scope(caller)
        current = authorize()
        if current.scope_fingerprint != caller.scope_fingerprint:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        from aiops_diagnostics.gateway_config import canonical_gateway_url

        base, proxy = canonical_gateway_url(base), canonical_gateway_url(proxy)
        manifest_text = trial_manifest if trial_manifest is not None else binding.manifest_text
        manifest = execution_manifest.from_text(manifest_text)
        if TOOL_NAMES[0] not in manifest.tool_names or not set(manifest.tool_names).issubset(TOOL_NAMES):
            raise OperatorRefused("OPERATOR_DETAIL_TOOL_DISABLED")
        # 查询只用于确认当前订单归属，不把它冒充节点本轮取得的诊断证据。
        body = cpms_get(base + PATHS[TOOL_NAMES[0]] + order, bearer, caller.effective_tenant_id)
        validate_body(body, current, order, TOOL_NAMES[0])
        order_site_id = body["data"]["siteId"]
        current = authorize()
        if current.scope_fingerprint != caller.scope_fingerprint:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        live = chat_execution.binding_for_key(self.context.settings, binding.key)
        if live is None or live != binding:
            raise OperatorRefused("OPERATOR_PUBLICATION_CHANGED")
        with self.lock:
            self._prune()
            if len(self.tasks) >= 20:
                raise OperatorRefused("OPERATOR_BUSY")
            config = AgentRunConfig.load(Path(binding.config_file))
            store = self.context.agent_run_store
            run = store.issue(
                scope=caller,
                order_no=order,
                config=config,
                gateway_version=__import__("aiops_diagnostics").__version__,
                expected_agent_version=manifest.identity,
                chat_binding=binding.key,
                execution_manifest=manifest_text,
                route_revision=binding.route_revision,
                operation=SCENARIO,
            )
            qa = self.context.store.create_assistant_question(
                caller.scope_fingerprint,
                question,
                internal_run_id=PREFIX + order,
                tenant_id=caller.effective_tenant_id,
            )
            if not store.bind_qa(run.run_id, qa["qa_id"]):
                store.fail(run.run_id, "qa_binding_failed")
                raise OperatorRefused("OPERATOR_BIND_FAILED")
            self.tasks[run.run_id] = ActiveTask(
                caller,
                binding,
                authorize,
                bearer,
                base,
                proxy,
                order_site_id,
                conversation_turn=conversation_turn,
                question=question,
                language=language,
                trial=trial_manifest is not None,
            )
        self.context.runtime._qa_registrations[qa["qa_id"]] = registration
        try:
            future = self.context.runtime._executor.submit(self._worker, run.run_id, qa["qa_id"], config)
        except Exception:  # noqa: BLE001 - 提交失败不能留下带公司凭据的待执行任务。
            self.context.agent_run_store.fail(run.run_id, "operator_schedule_failed")
            self.context.store.update_assistant_question(qa["qa_id"], status="failed", error_code="QA_FAILED")
            self.context.runtime._qa_registrations.pop(qa["qa_id"], None)
            with self.lock:
                self.tasks.pop(run.run_id, None)
            raise
        self.context.runtime._futures[qa["qa_id"]] = future
        future.add_done_callback(lambda _: self.context.runtime._futures.pop(qa["qa_id"], None))
        future.add_done_callback(lambda _: self.context.runtime._qa_registrations.pop(qa["qa_id"], None))
        return qa

    def _prune(self):
        for key in list(self.tasks):
            run = self.context.agent_run_store.get(key)
            if (
                run is None
                or run.expires_at <= datetime.now(UTC)
                or run.state in {"failed", "revoked", "expired"}
            ):
                del self.tasks[key]

    def _current(self, run_id, *, node_token=None, manifest=None, tool=None):
        task = self.tasks.get(run_id)
        run = self.context.agent_run_store.get(run_id)
        if task is None or run is None or run.operation != SCENARIO or run.state not in {"issued", "running"}:
            raise OperatorRefused("OPERATOR_RUN_INACTIVE")
        qa = self.context.store.get_assistant_question(run.qa_id, run.scope_fingerprint)
        if not qa or qa["status"] not in QUESTION.active:
            raise OperatorRefused("OPERATOR_RUN_INACTIVE")
        binding = chat_execution.binding_for_key(self.context.settings, run.chat_binding)
        if binding is None or binding.entry != "operator" or binding.route_revision != run.route_revision:
            raise OperatorRefused("OPERATOR_PUBLICATION_CHANGED")
        from aiops_diagnostics.execution.node_publication import targets

        if not any(
            t.entry == "operator"
            and t.app_id == binding.app_id
            and t.tenant_id == task.caller.effective_tenant_id
            and t.environment == run.environment
            and t.cpms_base_url.rstrip("/") == task.base_url
            and t.proxy_base_url.rstrip("/") == task.proxy_url
            for t in targets(self.context.settings.chat_node_targets_file)
        ):
            raise OperatorRefused("OPERATOR_TARGET_CHANGED")
        config = AgentRunConfig.load(Path(binding.config_file))
        if (config.environment, config.snail_app_id, str(config.snail_agent_id), config.expected_node_id) != (
            run.environment,
            run.app_id,
            run.agent_id,
            run.expected_node_id,
        ):
            raise OperatorRefused("OPERATOR_NODE_CHANGED")
        if not any(
            n.active and n.node_id == run.expected_node_id and n.environment == run.environment
            for n in config.node_token_hashes.values()
        ):
            raise OperatorRefused("OPERATOR_NODE_REJECTED")
        if node_token is not None:
            node = config.authenticate_node(node_token)
            if node is None or node.node_id != run.expected_node_id or node.environment != run.environment:
                raise OperatorRefused("OPERATOR_NODE_REJECTED")
        caller = task.authorize()
        if caller.scope_fingerprint != run.scope_fingerprint:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        binding = chat_execution.binding_for_key(self.context.settings, run.chat_binding)
        if binding is None or binding.route_revision != run.route_revision:
            raise OperatorRefused("OPERATOR_PUBLICATION_CHANGED")
        config = AgentRunConfig.load(Path(binding.config_file))
        if not any(
            n.active and n.node_id == run.expected_node_id and n.environment == run.environment
            for n in config.node_token_hashes.values()
        ):
            raise OperatorRefused("OPERATOR_NODE_REJECTED")
        if node_token is not None:
            node = config.authenticate_node(node_token)
            if node is None or node.node_id != run.expected_node_id or node.environment != run.environment:
                raise OperatorRefused("OPERATOR_NODE_REJECTED")
        if (config.environment, config.snail_app_id, str(config.snail_agent_id), config.expected_node_id) != (
            run.environment,
            run.app_id,
            run.agent_id,
            run.expected_node_id,
        ):
            raise OperatorRefused("OPERATOR_NODE_CHANGED")
        expected = execution_manifest.from_text(run.execution_manifest)
        if expected.identity != run.expected_agent_version or (
            not task.trial and binding.manifest_text != run.execution_manifest
        ):
            raise OperatorRefused("OPERATOR_MANIFEST_MISMATCH")
        if manifest is not None and manifest != expected.identity:
            raise OperatorRefused("OPERATOR_MANIFEST_MISMATCH")
        if tool is not None and (tool not in TOOL_NAMES or tool not in expected.tool_names):
            raise OperatorRefused("OPERATOR_TOOL_DISABLED")
        latest = self.context.agent_run_store.get(run_id)
        if (
            latest is None
            or latest.state not in {"issued", "running"}
            or latest.expires_at <= datetime.now(UTC)
        ):
            raise OperatorRefused("OPERATOR_RUN_INACTIVE")
        return task, latest, config

    def publication(self, run_id, token):
        _, run, _ = self._current(run_id, node_token=token)
        return {
            "run_id": run_id,
            "manifest": run.execution_manifest,
            "sha256": run.expected_agent_version,
            "operation": SCENARIO,
        }

    def grant(self, run_id, token, manifest, tool):
        task, run, _ = self._current(run_id, node_token=token, manifest=manifest, tool=tool)
        if run.state == "issued" and not self.context.agent_run_store.claim(run_id):
            raise OperatorRefused("OPERATOR_CLAIM_CONFLICT")
        return {
            "run_id": run_id,
            "tool": tool,
            "manifest_id": run.expected_agent_version,
            "authorization_id": hashlib.sha256(task.grant.encode()).hexdigest(),
            "tenant_id": task.caller.effective_tenant_id,
            "site_ids": [task.order_site_id],
            "order_no": run.order_no,
            "base_url": task.proxy_url + "/v1/internal/operator-runs/" + run_id + "/cpms",
            "bearer": task.grant,
            "expires_at": run.expires_at.isoformat(),
        }

    def proxy(self, run_id, grant, path, query):
        task = self.tasks.get(run_id)
        if task is None or not secrets.compare_digest(task.grant, grant):
            raise OperatorRefused("OPERATOR_GRANT_REJECTED")
        run = self.context.agent_run_store.get(run_id)
        if run is None:
            raise OperatorRefused("OPERATOR_RUN_INACTIVE")
        tool = next((name for name, prefix in PATHS.items() if path == prefix + run.order_no), None)
        if tool is None or query != ("current=1&size=50" if tool == TOOL_NAMES[1] else ""):
            raise OperatorRefused("OPERATOR_PATH_REJECTED")
        with task.lock:
            self._current(run_id, tool=tool)
            if tool in task.evidence:
                return task.evidence[tool]["response"]
            if tool == TOOL_NAMES[1] and TOOL_NAMES[0] not in task.delivered:
                raise OperatorRefused("OPERATOR_DETAIL_REQUIRED")

            body = cpms_get(
                task.base_url + path + ("?" + query if query else ""),
                task.bearer,
                task.caller.effective_tenant_id,
            )
            validate_body(body, task.caller, run.order_no, tool)
            rows = [body["data"]["chOrderInfoModel"]] if tool == TOOL_NAMES[0] else body["data"]["records"]
            if any(row["siteId"] != task.order_site_id for row in rows):
                raise OperatorRefused("CPMS_SCOPE_MISMATCH")
            self._current(run_id, tool=tool)
            # 只保存裁剪后的业务证据；不记录公司令牌和原响应中的个人信息。
            task.evidence[tool] = project_evidence(body, run.order_no, tool)
            task.evidence[tool]["response"] = minimized_response(body, tool)
            return task.evidence[tool]["response"]

    def invoke(self, grant, tool):
        if tool not in TOOL_NAMES:
            raise OperatorRefused("OPERATOR_TOOL_DISABLED")
        with self.lock:
            run_id = next(
                (key for key, task in self.tasks.items() if secrets.compare_digest(task.grant, grant)), None
            )
        if run_id is None:
            raise OperatorRefused("OPERATOR_GRANT_REJECTED")
        run = self.context.agent_run_store.get(run_id)
        if run is None:
            raise OperatorRefused("OPERATOR_RUN_INACTIVE")
        return self.proxy(
            run_id, grant, PATHS[tool] + run.order_no, "current=1&size=50" if tool == TOOL_NAMES[1] else ""
        )

    def record(self, run_id, token, manifest, tool, payload):
        if set(payload) != {"authorization_id", "evidence"}:
            raise OperatorRefused("OPERATOR_INVALID_BODY")
        task, run, _ = self._current(run_id, node_token=token, manifest=manifest, tool=tool)
        with task.lock:
            expected = task.evidence.get(tool)
            evidence = payload.get("evidence")
            if (
                expected is None
                or payload.get("authorization_id") != hashlib.sha256(task.grant.encode()).hexdigest()
            ):
                raise OperatorRefused("OPERATOR_EVIDENCE_MISSING")
            if (
                not isinstance(evidence, dict)
                or set(evidence) != {"tool", "order_no", "source", "observed_at", "data"}
                or evidence.get("tool") != tool
                or evidence.get("data") != expected["data"]
                or evidence.get("source") != expected["source"]
                or evidence.get("order_no") != run.order_no
            ):
                raise OperatorRefused("OPERATOR_EVIDENCE_MISMATCH")
            try:
                observed = datetime.fromisoformat(evidence["observed_at"])
                if observed.utcoffset() is None or not run.claimed_at <= observed <= datetime.now(UTC):
                    raise ValueError("invalid observation time")
            except (ValueError, TypeError) as exc:
                raise OperatorRefused("OPERATOR_EVIDENCE_MISMATCH") from exc
            self._current(run_id, node_token=token, manifest=manifest, tool=tool)
            task.delivered.add(tool)
            partial = {
                "answer": None,
                "evidence": {
                    name: {k: v for k, v in item.items() if k != "response"}
                    for name, item in task.evidence.items()
                    if name in task.delivered
                },
            }
            if not self.context.agent_run_store.complete(
                run_id, snapshot=partial, actual_node_id=run.expected_node_id, node_build=None
            ):
                raise OperatorRefused("OPERATOR_EVIDENCE_NOT_ACCEPTED")
            return {
                "run_id": run_id,
                "tool": tool,
                "authorization_id": payload["authorization_id"],
                "accepted": True,
            }

    def complete(self, run_id, token, manifest, payload):
        if set(payload) - {"status", "answer"}:
            raise OperatorRefused("OPERATOR_INVALID_BODY")
        task, run, _ = self._current(run_id, node_token=token, manifest=manifest)
        store = self.context.agent_run_store
        if payload.get("status") not in {"completed", "failed"}:
            raise OperatorRefused("OPERATOR_INVALID_COMPLETION")
        if payload["status"] == "failed":
            store.fail(run_id, "operator_node_failed")
            return {"run_id": run_id, "state": "failed"}
        with task.lock:
            try:
                raw = payload.get("answer", "")
                if not isinstance(raw, str) or len(raw) > 24000:
                    raise ValueError("invalid answer")
                answer = json.loads(raw)
            except ValueError as exc:
                logging.getLogger("aiops.operator").warning("operator_answer_rejected reason=json_parse")
                raise OperatorRefused("OPERATOR_INVALID_ANSWER") from exc
            validate_answer(answer, task.evidence)
            from aiops_diagnostics.redaction import contains_secret

            if contains_secret(raw, (task.bearer, task.grant)):
                _answer_refused("credential")
            if not set(answer["evidence_tools"]).issubset(task.delivered):
                raise OperatorRefused("OPERATOR_EVIDENCE_NOT_DELIVERED")
            self._current(run_id, node_token=token, manifest=manifest)
            snapshot = {
                "answer": answer,
                "trial": task.trial,
                "evidence": {
                    name: {k: v for k, v in item.items() if k != "response"}
                    for name, item in task.evidence.items()
                    if name in task.delivered
                },
            }
            if not store.complete(
                run_id, snapshot=snapshot, actual_node_id=run.expected_node_id, node_build=None
            ):
                raise OperatorRefused("OPERATOR_COMPLETE_REJECTED")
            verdict, completed = store.complete_loop(
                run_id, observed_agent_version=manifest, actual_node_id=run.expected_node_id
            )
            if verdict not in {"completed", "already_completed"}:
                raise OperatorRefused("OPERATOR_COMPLETE_REJECTED")
            return {"run_id": run_id, "state": completed.state}

    def _worker(self, run_id, qa_id, config):
        store = self.context.store
        try:
            if not store.update_assistant_question(qa_id, status="running"):
                raise OperatorRefused("OPERATOR_RUN_INACTIVE")
            dispatcher = SnailDispatcher()
            task = self.tasks[run_id]
            dispatcher.question = (
                "诊断本次授权订单，按已发布SOP读取证据并输出规定JSON。用户描述仅供理解问题："
                + json.dumps(task.question, ensure_ascii=False)
                + "。回答语言："
                + task.language
            )
            dispatcher.dispatch(config, run_id)
            task = self.tasks[run_id]
            current = task.authorize()
            result = self.authorize_readback(current, qa_id, task.bearer)
            if store.update_assistant_question(qa_id, status="completed", result=result):
                self.context.runtime._complete_conversation_turn(
                    task.conversation_turn, task.question, result, status_qa_id=qa_id
                )
        except Exception:  # noqa: BLE001 - 后台失败必须收敛，凭据及上游正文不进日志。
            if not self.context.agent_run_store.fail(run_id, "operator_failed"):
                self.context.agent_run_store.refuse(run_id, "operator_failed")
            store.update_assistant_question(qa_id, status="failed", error_code="OPERATOR_DIAGNOSIS_FAILED")
            task = self.tasks.get(run_id)
            if task is not None:
                self.context.runtime._release_conversation_turn(task.conversation_turn)
        finally:
            with self.lock:
                self.tasks.pop(run_id, None)

    def result(self, caller, qa_id):
        run = self.context.agent_run_store.by_qa(qa_id)
        if run is None or run.scope_fingerprint != caller.scope_fingerprint or run.operation != SCENARIO:
            raise OperatorRefused("QA_NOT_FOUND")
        binding = chat_execution.binding_for_key(self.context.settings, run.chat_binding)
        if (
            binding is None
            or binding.route_revision != run.route_revision
            or run.expires_at <= datetime.now(UTC)
        ):
            raise OperatorRefused("OPERATOR_RESULT_RETRACTED")
        config = AgentRunConfig.load(Path(binding.config_file))
        if (
            not (run.snapshot or {}).get("trial")
            and execution_manifest.from_text(binding.manifest_text).identity != run.expected_agent_version
        ):
            raise OperatorRefused("OPERATOR_MANIFEST_MISMATCH")
        run = self.context.agent_run_store.get(run.run_id)
        if (
            run is None
            or run.state != "succeeded"
            or not run.exec_config_confirmed
            or not run.snapshot
            or run.expires_at <= datetime.now(UTC)
        ):
            raise OperatorRefused("OPERATOR_RESULT_UNAVAILABLE")
        if (
            (config.environment, config.snail_app_id, str(config.snail_agent_id), config.expected_node_id)
            != (run.environment, run.app_id, run.agent_id, run.expected_node_id)
            or run.actual_node_id != run.expected_node_id
            or not any(
                node.active and node.node_id == run.actual_node_id and node.environment == run.environment
                for node in config.node_token_hashes.values()
            )
        ):
            raise OperatorRefused("OPERATOR_NODE_CHANGED")
        scope = operator_scope(caller)
        detail = run.snapshot["evidence"][TOOL_NAMES[0]]["data"]
        if scope.site_ids is not None and detail["siteId"] not in scope.site_ids:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        answer = run.snapshot["answer"]
        validate_answer(answer, run.snapshot["evidence"])
        citations = [
            f"{item['source']} · {item['observed_at']}" for item in run.snapshot["evidence"].values()
        ]
        text = "\n\n".join(
            [
                answer["summary"],
                answer["probable_cause"],
                *answer["next_steps"],
                *answer["limitations"],
                *citations,
            ]
        )
        return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "unavailable"}

    def retract(self, route):
        with self.lock:
            tasks = list(self.tasks.items())
        for run_id, task in tasks:
            run = self.context.agent_run_store.get(run_id)
            if run and run.chat_binding == route.key and run.route_revision <= route.revision:
                self.context.runtime.cancel_assistant_qa(task.caller, run.qa_id)


DETAIL_FIELDS = [
    "orderNo",
    "siteId",
    "deviceId",
    "childDeviceId",
    "deviceCode",
    "childDeviceCode",
    "deviceProtocol",
    "status",
    "createdTime",
    "stopTime",
    "stoppedReasonCode",
    "stoppedReasonContent",
    "electricity",
]
SAMPLE_FIELDS = [
    "orderNo",
    "siteId",
    "deviceId",
    "childDeviceId",
    "createdTime",
    "power",
    "voltage",
    "current",
    "chargingElectricityQuantity",
    "soc",
    "temperature",
    "batteryMaxTemperature",
    "batteryMinTemperature",
]


def minimized_response(body, tool):
    data = body["data"]
    fields = {"tenantId", *(DETAIL_FIELDS if tool == TOOL_NAMES[0] else SAMPLE_FIELDS)}
    if tool == TOOL_NAMES[0]:
        selected = {
            "orderNo": data["orderNo"],
            "siteId": data["siteId"],
            "chOrderInfoModel": {k: v for k, v in data["chOrderInfoModel"].items() if k in fields},
        }
    else:
        selected = {k: data[k] for k in ("total", "current", "size")}
        selected["records"] = [{k: v for k, v in row.items() if k in fields} for row in data["records"]]
    return {"code": 0, "data": selected}


def validate_body(body, caller, order, tool):
    if (
        not isinstance(body, dict)
        or type(body.get("code")) is not int
        or body["code"] != 0
        or not isinstance(body.get("data"), dict)
    ):
        raise OperatorRefused("CPMS_INVALID_RESPONSE")
    data = body["data"]
    scope = operator_scope(caller)
    rows = [data.get("chOrderInfoModel")] if tool == TOOL_NAMES[0] else data.get("records")
    if not isinstance(rows, list) or len(rows) > 50:
        raise OperatorRefused("CPMS_INVALID_RESPONSE")
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("orderNo") != order
            or row.get("tenantId") != caller.effective_tenant_id
            or not isinstance(row.get("siteId"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", row["siteId"])
            or (scope.site_ids is not None and row["siteId"] not in scope.site_ids)
        ):
            raise OperatorRefused("CPMS_SCOPE_MISMATCH")
    if tool == TOOL_NAMES[0]:
        if data.get("orderNo") != order or data.get("siteId") != rows[0].get("siteId"):
            raise OperatorRefused("CPMS_SCOPE_MISMATCH")
    elif (
        type(data.get("total")) is not int
        or data["total"] < len(rows)
        or type(data.get("current")) is not int
        or data["current"] != 1
        or type(data.get("size")) is not int
        or data["size"] != 50
    ):
        raise OperatorRefused("CPMS_INVALID_PAGE")


def project_evidence(body, order, tool):
    data = body["data"]

    def pick(row, fields):
        result = {key: row[key] for key in fields if key in row}
        if any(
            isinstance(v, (dict, list)) or (isinstance(v, str) and len(v) > 1000) for v in result.values()
        ):
            raise OperatorRefused("CPMS_INVALID_FIELD")
        return result

    selected = (
        pick(data["chOrderInfoModel"], DETAIL_FIELDS)
        if tool == TOOL_NAMES[0]
        else {
            "records": [pick(row, SAMPLE_FIELDS) for row in data["records"]],
            "total": data["total"],
            "sample_limit": 50,
            "truncated": data["total"] > len(data["records"]),
            "ordering_guaranteed": False,
            "complete_timeline": False,
        }
    )
    return {
        "tool": tool,
        "order_no": order,
        "source": "cpms.orderDetail" if tool == TOOL_NAMES[0] else "cpms.chargingMonitor",
        "observed_at": datetime.now(UTC).isoformat(),
        "data": selected,
    }


EXECUTED_MUTATION = re.compile(
    r"(?:"
    r"(?:已|已经|成功|自动|完成|执行了?|进行了?)\s*"
    r"(?:订单)?(?:重算|退款|补发|重放|重试下发|重启|修改|更新|删除|写入)"
    r"|(?:重算|退款|补发|重放|重试下发|重启|修改订单|更新订单|删除订单|写入数据库)"
    r"\s*(?:已完成|成功|完成|完毕)"
    r"|\b(?:refunded|recalculated|replayed|resent|restarted|modified|updated|deleted)\b"
    r")",
    re.IGNORECASE,
)


def _answer_refused(reason):
    logging.getLogger("aiops.operator").warning("operator_answer_rejected reason=%s", reason)
    raise OperatorRefused("OPERATOR_INVALID_ANSWER")


def validate_answer(answer, evidence):
    if not isinstance(answer, dict) or set(answer) != {
        "summary",
        "probable_cause",
        "next_steps",
        "limitations",
        "evidence_tools",
    }:
        _answer_refused("shape")
    for key in ("summary", "probable_cause"):
        if not isinstance(answer[key], str) or not 1 <= len(answer[key]) <= 2000:
            _answer_refused("text_field")
    for key in ("next_steps", "limitations", "evidence_tools"):
        if (
            not isinstance(answer[key], list)
            or len(answer[key]) > 12
            or any(not isinstance(v, str) or not 1 <= len(v) <= 1000 for v in answer[key])
        ):
            _answer_refused("list_field")
    if (
        TOOL_NAMES[0] not in evidence
        or TOOL_NAMES[0] not in answer["evidence_tools"]
        or not set(answer["evidence_tools"]).issubset(evidence)
    ):
        raise OperatorRefused("OPERATOR_EVIDENCE_MISSING")
    text = json.dumps(answer, ensure_ascii=False)
    if EXECUTED_MUTATION.search(text):
        _answer_refused("mutation_claim")


def cpms_get(url, bearer, tenant):
    def unavailable(_):
        return OperatorRefused("CPMS_UNAVAILABLE")

    return request_json(
        RequestSpec(
            url=url,
            headers={
                "Authorization": "Bearer " + bearer,
                "tenant-id": tenant,
                "client-type": "admin",
                "Accept": "application/json",
            },
            timeout=10,
            max_read_bytes=262144,
            follow_redirects=False,
        ),
        mapping=ErrorMapping(
            auth_rejected=unavailable,
            http_error=unavailable,
            unavailable=unavailable,
            invalid_body=unavailable,
            invalid_envelope=unavailable,
        ),
    )
