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
from urllib.parse import urlencode

from aiops_diagnostics.common.async_job_lifecycle import QUESTION
from aiops_diagnostics.common.bounded_http import ErrorMapping, RequestSpec, request_json
from aiops_diagnostics.execution import chat_execution, execution_manifest
from aiops_diagnostics.execution.agent_runs import AgentRunConfig, SnailDispatcher
from aiops_diagnostics.identity.query_scope import resolve_query_scope
from aiops_diagnostics.identity.scope_context import is_tenant_level_account
from aiops_diagnostics.operator.operator_conversation import EXECUTED_MUTATION, OperatorRefused

SCENARIO = "operator_order_diagnosis"
PREFIX = "operator-cpms-node:"
TOOL_NAMES = chat_execution.OPERATOR_TOOL_NAMES
PATHS = {
    TOOL_NAMES[0]: "/chOrderInfo/getOrderInfoDetail/",
    TOOL_NAMES[1]: "/iotChargingDevice/getChargingMonitor/",
}


def operator_scope(caller, *, allow_empty=False):
    if not caller.subject.b_user_id or caller.delegated or caller.data_scope.type not in {"all", "organ"}:
        raise OperatorRefused("OPERATOR_SITE_SCOPE_REQUIRED")
    scope = resolve_query_scope(caller)
    if (scope.site_ids == () and not allow_empty) or (
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

        from aiops_diagnostics.gateway.gateway_config import canonical_gateway_url

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

    def refresh_scope(self, caller, *, allow_empty=False):
        from dataclasses import replace

        from aiops_diagnostics.identity.scope_context import DataScope

        if operator_scope(caller, allow_empty=allow_empty).site_ids is None:
            return caller
        directory = getattr(self.context.runtime, "_trial_operator_scope", None)
        if directory is None:
            raise OperatorRefused("OPERATOR_SITE_SCOPE_REQUIRED")
        try:
            scope = directory.site_scope_by_b_user_id(caller.subject.b_user_id, caller.effective_tenant_id)
        except Exception as exc:  # noqa: BLE001 - 授权源失败不沿用旧范围。
            raise OperatorRefused("OPERATOR_SCOPE_UNAVAILABLE") from exc
        if scope.tenant_id != caller.effective_tenant_id or (not scope.site_ids and not allow_empty):
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        return replace(
            caller, data_scope=DataScope(type="organ", site_ids=tuple(scope.site_ids)), scope_fingerprint=""
        )

    def authorize_readback(self, caller, qa_id, bearer):
        run = self.context.agent_run_store.by_qa(qa_id)
        caller = self.refresh_scope(caller, allow_empty=bool(run and not run.order_no))
        if run is None or run.scope_fingerprint != caller.scope_fingerprint:
            raise OperatorRefused("QA_NOT_FOUND")
        from aiops_diagnostics.execution.node_publication import all_targets

        binding = chat_execution.binding_for_key(self.context.settings, run.chat_binding)
        if binding is None or binding.route_revision != run.route_revision:
            raise OperatorRefused("OPERATOR_RESULT_RETRACTED")
        target = next(
            (
                t
                for t in all_targets(self.context.settings)
                if t.entry == "operator"
                and t.app_id == binding.app_id
                and t.tenant_id == caller.effective_tenant_id
                and t.environment == run.environment
            ),
            None,
        )
        if target is None:
            raise OperatorRefused("OPERATOR_TARGET_MISSING")
        if not run.order_no:
            return self.result(caller, qa_id)
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
        if order and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", order):
            raise OperatorRefused("ORDER_INVALID")
        operator_scope(caller, allow_empty=not order)
        current = authorize()
        if current.scope_fingerprint != caller.scope_fingerprint:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        from aiops_diagnostics.gateway.gateway_config import canonical_gateway_url

        base, proxy = canonical_gateway_url(base), canonical_gateway_url(proxy)
        manifest_text = trial_manifest if trial_manifest is not None else binding.manifest_text
        if not order:
            from aiops_diagnostics.operator import operator_conversation

            manifest_text = operator_conversation.manifest(manifest_text)
        manifest = execution_manifest.from_text(manifest_text)
        if order and (
            TOOL_NAMES[0] not in manifest.tool_names or not set(manifest.tool_names).issubset(TOOL_NAMES)
        ):
            raise OperatorRefused("OPERATOR_DETAIL_TOOL_DISABLED")
        # 查询只用于确认当前订单归属，不把它冒充节点本轮取得的诊断证据。
        order_site_id = ""
        if order:
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
                operation=SCENARIO if order else operator_conversation.SCENARIO,
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
        from aiops_diagnostics.operator import operator_conversation

        if (
            task is None
            or run is None
            or run.operation not in {SCENARIO, operator_conversation.SCENARIO}
            or run.state not in {"issued", "running"}
        ):
            raise OperatorRefused("OPERATOR_RUN_INACTIVE")
        qa = self.context.store.get_assistant_question(run.qa_id, run.scope_fingerprint)
        if not qa or qa["status"] not in QUESTION.active:
            raise OperatorRefused("OPERATOR_RUN_INACTIVE")
        binding = chat_execution.binding_for_key(self.context.settings, run.chat_binding)
        if binding is None or binding.entry != "operator" or binding.route_revision != run.route_revision:
            raise OperatorRefused("OPERATOR_PUBLICATION_CHANGED")
        from aiops_diagnostics.execution.node_publication import all_targets

        if not any(
            t.entry == "operator"
            and t.app_id == binding.app_id
            and t.tenant_id == task.caller.effective_tenant_id
            and t.environment == run.environment
            and t.cpms_base_url.rstrip("/") == task.base_url
            and t.proxy_base_url.rstrip("/") == task.proxy_url
            for t in all_targets(self.context.settings)
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
        if expected.identity != run.expected_agent_version:
            raise OperatorRefused("OPERATOR_MANIFEST_MISMATCH")
        if not task.trial:
            bound_manifest = (
                binding.manifest_text
                if run.order_no
                else operator_conversation.manifest(binding.manifest_text)
            )
            if bound_manifest != run.execution_manifest:
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
        if not run.order_no and run.state == "issued" and not self.context.agent_run_store.claim(run_id):
            raise OperatorRefused("OPERATOR_CLAIM_CONFLICT")
        return {
            "run_id": run_id,
            "manifest": run.execution_manifest,
            "sha256": run.expected_agent_version,
            "operation": run.operation,
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
                answer = parse_answer(raw)
            except ValueError as exc:
                logging.getLogger("aiops.operator").warning("operator_answer_rejected reason=json_parse")
                raise OperatorRefused("OPERATOR_INVALID_ANSWER") from exc
            from aiops_diagnostics.common.redaction import contains_secret

            if contains_secret(raw, (task.bearer, task.grant)):
                _answer_refused("credential")
            sites = None
            if run.order_no:
                validate_answer(answer, task.evidence)
                if not set(answer["evidence_tools"]).issubset(task.delivered):
                    raise OperatorRefused("OPERATOR_EVIDENCE_NOT_DELIVERED")
            else:
                from aiops_diagnostics.operator import operator_conversation

                operator_conversation.validate(answer, task.question)
                if answer["kind"] == "site_search":

                    def current_search_scope():
                        current_task, _, _ = self._current(run_id, node_token=token, manifest=manifest)
                        return current_task.caller

                    sites = search_sites(
                        answer, task.caller, task.base_url, task.bearer, current_search_scope
                    )
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
            if not run.order_no:
                snapshot["sites"] = sites
                snapshot["language"] = task.language
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
                (
                    "诊断本次授权订单，按已发布SOP读取证据并输出规定JSON。用户描述仅供理解问题："
                    if self.context.agent_run_store.get(run_id).order_no
                    else "本轮未指定订单，按管家对话协议回答或提出站点搜索条件。用户问题："
                )
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
        from aiops_diagnostics.operator import operator_conversation

        run = self.context.agent_run_store.by_qa(qa_id)
        if (
            run is None
            or run.scope_fingerprint != caller.scope_fingerprint
            or run.operation not in {SCENARIO, operator_conversation.SCENARIO}
        ):
            raise OperatorRefused("QA_NOT_FOUND")
        binding = chat_execution.binding_for_key(self.context.settings, run.chat_binding)
        if (
            binding is None
            or binding.route_revision != run.route_revision
            or run.expires_at <= datetime.now(UTC)
        ):
            raise OperatorRefused("OPERATOR_RESULT_RETRACTED")
        config = AgentRunConfig.load(Path(binding.config_file))
        if not (run.snapshot or {}).get("trial"):
            bound_manifest = (
                binding.manifest_text
                if run.order_no
                else operator_conversation.manifest(binding.manifest_text)
            )
            if execution_manifest.from_text(bound_manifest).identity != run.expected_agent_version:
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
        if not run.order_no:
            sites = run.snapshot.get("sites")
            scope = operator_scope(caller, allow_empty=True)
            if (
                sites
                and scope.site_ids is not None
                and any(s["id"] not in scope.site_ids for s in sites["sites"])
            ):
                raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
            return operator_conversation.render(run.snapshot["answer"], sites, run.snapshot["language"])
        scope = operator_scope(caller)
        detail = run.snapshot["evidence"][TOOL_NAMES[0]]["data"]
        if scope.site_ids is not None and detail["siteId"] not in scope.site_ids:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        answer = run.snapshot["answer"]
        validate_answer(answer, run.snapshot["evidence"])
        text = render_report(answer, run.snapshot["evidence"])
        return {"blocks": [{"kind": "text", "text": text}], "retrieval_status": "unavailable"}

    def retract(self, route):
        with self.lock:
            tasks = list(self.tasks.items())
        for run_id, task in tasks:
            run = self.context.agent_run_store.get(run_id)
            if run and run.chat_binding == route.key and run.route_revision <= route.revision:
                self.context.runtime.cancel_assistant_qa(task.caller, run.qa_id)


#: 订单详情的取证字段。**这张表就是模型能回答什么**：少一个金额，问电费的答案就只剩
#: 「接口未提供」。41 实测（2026-10-29，订单 2113549588485718017）`getOrderInfoDetail`
#: 返回 `electricityFee=13.68`、`serviceFee=13.68`、`totalAmount=27.36`、`payAmount=0.00`，
#: 而当时这里一个都没有 —— 于是两笔不同的问题得到同一份「缺证」报告。
#:
#: `time` 与 `gunPower` 一起才构成可核对的一对：只有「4.5598 kWh / 229 秒 ≈ 71.7 kW」而没有
#: 额定 23 kW，模型无法判断这算不算异常；把两者都给出来，异常才成为可陈述的事实。
#:
#: 纯电价（tip/peak/flat/valley*Fee）在 41 全为 NULL，故不在这里重复列出；计费模板的
#: 分时段明细（`periodFeeDetail`）在外层响应里，本层不取 —— 本次只补平铺金额。
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
    "time",
    "voltage",
    "electricityFee",
    "serviceFee",
    "totalAmount",
    "payAmount",
    "marketAmount",
    "refundStatus",
    "refundAmount",
    "startSoc",
    "endSoc",
    "gunPower",
    "interfaceType",
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
        # 与 `project_evidence` 同口径：响应被裁剪后仍要能分辨「一条都没有」。
        selected["samples_returned"] = len(data["records"])
        selected["absence"] = "no_samples" if not data["records"] else None
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
            # `samples_returned` 与 `truncated` 是**两件事**，以前只有后者：`total=0`
            # 让 truncated=False，于是一个从没上报过样本的枪和一个取全了的订单在
            # 证据里长得一样。41 实测（订单 2113549588485718017）该枪样本 0 条，
            # 而报告只能说「样本不是完整时间线」—— 读的人分不清是缺证还是没数据。
            # 现在这条事实直接摆进证据，模型不必也不该自行推断。
            "samples_returned": len(data["records"]),
            "absence": "no_samples" if not data["records"] else None,
        }
    )
    return {
        "tool": tool,
        "order_no": order,
        "source": "cpms.orderDetail" if tool == TOOL_NAMES[0] else "cpms.chargingMonitor",
        "observed_at": datetime.now(UTC).isoformat(),
        "data": selected,
    }


_FENCED_ANSWER = re.compile(r"\A```(?:json)?[ \t]*\r?\n(?P<body>.*?)\r?\n```\Z", re.DOTALL)


def parse_answer(raw):
    """把节点回传的正文解析成诊断报告。

    实测（41，deepseek-v4-flash，流式 + 工具历史）：即使 `response_format=json_object`，
    供应商仍会**间歇**用 ```json 围栏包住那份唯一的 JSON —— 同一提示词、同一请求，
    三次里两次裸 JSON、一次带围栏。只做 `json.loads` 于是把一次合规回答判成
    `json_parse` 失败，这正是 `OPERATOR_DIAGNOSIS_FAILED` 的确定性成因。

    这里只剥**一层**围栏且围栏内必须恰好一个 JSON 对象；散文、多份 JSON、残缺块
    仍然拒绝 —— 放宽的是供应商的装饰，不是输出的契约。
    """
    try:
        return json.loads(raw)
    except ValueError as bare:
        fenced = _FENCED_ANSWER.match(raw.strip())
        if fenced is None:
            raise
        try:
            return json.loads(fenced.group("body"))
        except ValueError:
            raise bare from None


def _answer_refused(reason):
    logging.getLogger("aiops.operator").warning("operator_answer_rejected reason=%s", reason)
    raise OperatorRefused("OPERATOR_INVALID_ANSWER")


ANSWER_FIELDS = frozenset(
    {"direct_answer", "summary", "probable_cause", "next_steps", "limitations", "evidence_tools"}
)


def validate_answer(answer, evidence):
    if not isinstance(answer, dict) or set(answer) != ANSWER_FIELDS:
        _answer_refused("shape")
    # `direct_answer` 与 summary/probable_cause 同一条文字判据：**空串即拒绝**。
    # 它是「这次回答的是用户问的那一点」的唯一落点；允许它为空，就等于允许模型交回一份
    # 与提问无关的通用报告 —— 那正是 2026-10-29 反馈里「两个问题同一个答案」的形状。
    for key in ("direct_answer", "summary", "probable_cause"):
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


def _readable_time(iso: object) -> str:
    """``2026-10-29T12:34:45.123456+00:00`` → ``2026-10-29 12:34:45 UTC``。

    取证时间带微秒是因为它是给核对用的；但直接摆在报告末尾，读者看到的是六个毫无意义的
    数字。解析不了就原样返回，不硬凑。
    """
    try:
        parsed = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return str(iso)
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") + " UTC"


#: 证据来源标识 → 人话。报告末尾给的是"我查了哪里"，不是内部接口名。
_SOURCE_LABELS = {
    "cpms.orderDetail": "订单详情",
    "cpms.chargingMonitor": "充电监测样本",
}


def render_report(answer, evidence) -> str:
    """把结构化诊断渲染成**人读的报告**。

    之前这里是 ``"\\n\\n".join([...])``：九段文字被摊平成同等重要的段落，列表失去编号、
    没有任何小标题、技术标识（``cpms.orderDetail``、带微秒的 UTC 时间）直接摆给用户。
    内容全对，但没有一处告诉读的人"哪句是结论、哪句是下一步"。

    前端按 ``blocks[]`` 分派渲染、**不解析 Markdown**（frontend-api-brief §9），所以这里
    只用纯文本能表达的层级：小标题、``-`` 列表、缩进、空行。

    顺序是刻意的：先回答问题，再给事实，最后才是排查建议与缺证 —— 运维最常要的是前两段。
    """
    sections = [answer["direct_answer"]]
    if answer["summary"]:
        sections.append("【订单事实】\n" + answer["summary"])
    if answer["probable_cause"]:
        sections.append("【可能原因】\n" + answer["probable_cause"])
    for title, key in (("【建议下一步】", "next_steps"), ("【证据缺口】", "limitations")):
        if answer[key]:
            # 编号而不是光秃秃的行：用户会说"第 2 条"，还能对上号。
            items = "\n".join(f"{i}. {item}" for i, item in enumerate(answer[key], 1))
            sections.append(title + "\n" + items)
    if evidence:
        lines = []
        for item in evidence.values():
            label = _SOURCE_LABELS.get(item.get("source"), item.get("source"))
            lines.append(f"- {label}（{_readable_time(item.get('observed_at'))}）")
        sections.append("【数据来源】\n" + "\n".join(lines))
    return "\n\n".join(sections)


def cpms_get(url, bearer, tenant, *, timeout=10):
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
            timeout=timeout,
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


def search_sites(answer, caller, base, bearer, authorize):
    scope = operator_scope(caller, allow_empty=True)
    rows, seen = [], set()
    limited = False
    total = 0
    if scope.site_ids != ():
        for page in range(1, 5):
            current = authorize()
            if current.scope_fingerprint != caller.scope_fingerprint:
                raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
            params = {"current": page, "size": 50, "type": 0}
            if answer["name"]:
                params["name"] = answer["name"]
            if answer["address_terms"]:
                params["address"] = max(answer["address_terms"], key=len)
            body = cpms_get(
                base + "/chSite/page?" + urlencode(params), bearer, caller.effective_tenant_id, timeout=3
            )
            data = body.get("data") if isinstance(body, dict) else None
            if (
                not isinstance(body, dict)
                or type(body.get("code")) is not int
                or body["code"] != 0
                or not isinstance(data, dict)
                or not isinstance(data.get("records"), list)
                or len(data["records"]) > 50
                or type(data.get("total")) is not int
                or data["total"] < 0
                or data.get("current") != page
                or type(data.get("current")) is not int
                or data.get("size") != 50
            ):
                raise OperatorRefused("CPMS_INVALID_RESPONSE")
            for item in data["records"]:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not item["id"]
                    or str(item.get("tenantId") or "") != caller.effective_tenant_id
                    or (scope.site_ids is not None and str(item["id"]) not in scope.site_ids)
                ):
                    raise OperatorRefused("CPMS_SCOPE_MISMATCH")
                selected = {key: str(item.get(key) or "") for key in ("id", "name", "address")}
                if (
                    not isinstance(item.get("name"), str)
                    or not isinstance(item.get("address"), (str, type(None)))
                    or not selected["name"]
                    or any(len(v) > 1000 for v in selected.values())
                ):
                    raise OperatorRefused("CPMS_INVALID_RESPONSE")
                haystack = (selected["name"] + " " + selected["address"]).casefold()
                if (
                    selected["id"] not in seen
                    and answer["name"].casefold() in selected["name"].casefold()
                    and all(t.casefold() in haystack for t in answer["address_terms"])
                ):
                    seen.add(selected["id"])
                    rows.append(selected)
            more = page * 50 < data["total"]
            total = max(total, data["total"])
            if len(rows) >= 10 or not more or not data["records"]:
                limited = more or len(rows) > 10
                break
            limited = more
    if authorize().scope_fingerprint != caller.scope_fingerprint:
        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
    return {
        "source": "cpms.sitePage",
        "observed_at": datetime.now(UTC).isoformat(),
        "sites": rows[:10],
        "limited": limited,
        "empty_scope": scope.site_ids == (),
        # 列举问法要回答"我有多少个"，只给前 10 条等于没回答。``total`` 是 CPMS 对
        # 这次查询自己的计数；``scope_limited`` 说明它是否还经过站点集合过滤 ——
        # 员工账号的 total 是租户口径，不能直接当成"你有多少个"。
        "total": total,
        "scope_limited": scope.site_ids is not None,
    }
