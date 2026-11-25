"""管家节点任务编排门面：凭据留在 Gateway，按单次任务转发固定 CPMS 只读接口。

内部拆分为三层高内聚模块：
- `cpms_task_store.py`：任务状态机、锁、授权刷新、轨迹与回读闸门
- `cpms_proxy.py`：CPMS / tsdata 代理转发、站点方案与充电时序取数
- `cpms_evidence.py`：无状态工具契约校验、证据投影与错误归因
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from aiops_diagnostics.common.endpoint_policy import canonical_gateway_url
from aiops_diagnostics.data.sources import SourceError
from aiops_diagnostics.execution import chat_execution, execution_manifest
from aiops_diagnostics.execution.agent_runs import (
    AgentRunConfig,
    AgentRunDispatchError,
    SnailDispatcher,
)
from aiops_diagnostics.execution.run_deliverability import deliverable, expired
from aiops_diagnostics.identity.order_identity import is_order_identifier
from aiops_diagnostics.operator import (
    cpms_proxy,
    cpms_sites,
    cpms_task_store,
    operator_agent,
    operator_answer_review,
    operator_conversation,
)
from aiops_diagnostics.operator.cpms_evidence import (
    DERIVED_EVIDENCE_FIELDS,  # noqa: F401
    DETAIL_FIELDS,  # noqa: F401
    FEEDBACK_REFUSALS,  # noqa: F401
    NODE_FAILURE_CODES,
    OBJECT_CONTRACT,
    PREFIX,
    SAMPLE_FIELDS,  # noqa: F401
    SCENARIO,
    SITE_TOOL,
    TIMELINE_FIELDS,  # noqa: F401
    TIMELINE_LIMIT,  # noqa: F401
    TIMELINE_TOOL,  # noqa: F401
    TOOL_CONTRACT,
    TOOL_CONTRACT_HEADER,  # noqa: F401
    TOOL_NAMES,
    TOOL_REFUSED_PREFIX,
    _answer_refused,
    _declared,
    _failure_code,
    _failure_reason,
    _provenance,
    cpms_get,
    diagnoses_an_order,
    minimized_response,  # noqa: F401
    operator_scope,
    project_evidence,  # noqa: F401
    run_values,
    still_authorized,
    tool_inputs,
    tool_request,
    tsdata_query,
    validate_body,  # noqa: F401
    validate_tool_arguments,
)
from aiops_diagnostics.operator.cpms_sites import (
    SITE_MAX_PAGES,  # noqa: F401
    SITE_MAX_SHOWN,  # noqa: F401
    SITE_PAGE_SIZE,  # noqa: F401
    SITE_PATH,  # noqa: F401
    SiteKeywords,  # noqa: F401
    SiteScope,  # noqa: F401
    query_sites,  # noqa: F401
    read_site_pages,  # noqa: F401
    read_site_tool,  # noqa: F401
    site_keywords,  # noqa: F401
    site_page,  # noqa: F401
    site_scope,  # noqa: F401
    site_tool_result,  # noqa: F401
    tool_site_keywords,  # noqa: F401
)
from aiops_diagnostics.operator.cpms_task_store import ActiveTask
from aiops_diagnostics.operator.operator_conversation import OperatorRefused

# 同一 run 最多一次有界重派，复用其已经读取的证据。
MAX_RESAMPLES = 1


def cpms_site_reader(base: str, bearer: str):
    """直连 CPMS 的站点页读取函数（经模块内 `cpms_get` 转发，保留测试替身入口）。"""
    return cpms_sites.cpms_site_reader(base, bearer, getter=cpms_get)


class OperatorCpms:
    def __init__(self, context):
        self.context = context
        self.tasks = {}
        self.candidate_authorizations = {}
        self.lock = threading.RLock()

    # ------------------------------------------------------------------
    # 任务状态、追踪与授权闸门（委托 cpms_task_store）
    # ------------------------------------------------------------------

    def _trace(
        self, run_id, phase, status="running", *, tool=None, duration_ms=None, detail=None, summary=None
    ):
        cpms_task_store.trace_event(
            self,
            run_id,
            phase,
            status,
            tool=tool,
            duration_ms=duration_ms,
            detail=detail,
            summary=summary,
        )

    def _summarize_tool(self, run_id, task, tool, *, refusal=None):
        cpms_task_store.summarize_tool(self, run_id, task, tool, refusal=refusal)

    def _note_refusal(self, task, tool, exc):
        cpms_task_store.note_refusal(task, tool, exc)

    def progress(self, caller, qa_id, qa=None):
        return cpms_task_store.progress(self, caller, qa_id, qa=qa)

    def console_identity(self, session):
        return cpms_task_store.console_identity(self, session)

    def refresh_scope(self, caller, *, allow_empty=False):
        return cpms_task_store.refresh_scope(self, caller, allow_empty=allow_empty)

    def authorize_readback(self, caller, qa_id, bearer, *, candidate_authorize=None):
        return cpms_task_store.authorize_readback(
            self,
            caller,
            qa_id,
            bearer,
            cpms_get_fn=cpms_get,
            candidate_authorize=candidate_authorize,
        )

    def _authorized_order(self, task, order, *, expected_site=""):
        return cpms_task_store.authorized_order(
            task, order, cpms_get_fn=cpms_get, expected_site=expected_site
        )

    def _authorize_snapshot(self, task, run):
        cpms_task_store.authorize_snapshot(self, task, run, cpms_get_fn=cpms_get)

    def _messages(self, task):
        return cpms_task_store.build_messages(self, task)

    def _prune(self):
        cpms_task_store.prune_tasks(self)

    def _expire_candidate(self, run_id):
        cpms_task_store.expire_candidate(self, run_id)

    def _stamp_gate_refusal(self, run_id, code):
        cpms_task_store.stamp_gate_refusal(self, run_id, code)

    def _authenticate(self, run_id, *, node_token=None, manifest=None):
        return cpms_task_store.authenticate_inner(self, run_id, node_token=node_token, manifest=manifest)

    def _authorize_execution(self, run_id, *, run, config, node_token=None, manifest=None, tool=None):
        return cpms_task_store.authorize_execution_inner(
            self,
            run_id,
            run=run,
            config=config,
            node_token=node_token,
            manifest=manifest,
            tool=tool,
        )

    def authenticate(self, run_id, *, node_token=None, manifest=None):
        """协议级闸：run 存在、令牌属实为本 run 的期望节点、manifest 匹配。"""
        try:
            return self._authenticate(run_id, node_token=node_token, manifest=manifest)
        except OperatorRefused as exc:
            self._stamp_gate_refusal(run_id, str(exc))
            raise

    def authorize_execution(self, run_id, *, node_token=None, manifest=None, tool=None):
        """执行级闸：协议身份之上，「本次执行仍被许可」。"""
        try:
            run, config = self._authenticate(run_id, node_token=node_token, manifest=manifest)
            return self._authorize_execution(
                run_id, run=run, config=config, node_token=node_token, manifest=manifest, tool=tool
            )
        except OperatorRefused as exc:
            self._stamp_gate_refusal(run_id, str(exc))
            raise

    def _absorb_superseded(self, run_id, patch):
        return cpms_task_store.absorb_superseded(self, run_id, patch)

    def _previous_plan(self, task):
        return cpms_task_store.previous_plan(self, task)

    def _grant_sites(self, task, run, tool) -> list[str] | None:
        del run
        return cpms_task_store.grant_sites(task, tool)

    def retract(self, route):
        cpms_task_store.retract_tasks(self, route)

    def verify_live_stream_access(self, caller, qa_id, bearer):
        return cpms_task_store.verify_live_stream_access(self, caller, qa_id, bearer, cpms_get_fn=cpms_get)

    # ------------------------------------------------------------------
    # CPMS 代理转发与计算工具（委托 cpms_proxy）
    # ------------------------------------------------------------------

    def _station_plan(self, task, run):
        return cpms_proxy.station_plan(self, task, run, cpms_get_fn=cpms_get)

    def _charging_timeline(self, task, run):
        return cpms_proxy.charging_timeline(self, task, run, tsdata_query_fn=tsdata_query)

    def proxy(self, run_id, grant, path, query, method="GET"):
        return cpms_proxy.proxy(
            self,
            run_id,
            grant,
            path,
            query,
            method,
            cpms_get_fn=cpms_get,
            tsdata_query_fn=tsdata_query,
        )

    def _proxy(self, run_id, grant, path, query, method):
        return cpms_proxy.proxy_inner(
            self,
            run_id,
            grant,
            path,
            query,
            method,
            cpms_get_fn=cpms_get,
            tsdata_query_fn=tsdata_query,
        )

    def invoke(self, grant, tool):
        return cpms_proxy.invoke(self, grant, tool)

    # ------------------------------------------------------------------
    # 节点执行编排：start / publication / grant / record / complete / _worker / result
    # ------------------------------------------------------------------

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
        candidate=False,
        agent_id="operator_main",
        attachments=None,
        attempt_no=1,
        parent_qa_id=None,
    ):
        if candidate and trial_manifest is None:
            raise OperatorRefused("OPERATOR_TRIAL_SCOPE_MISMATCH")
        conversation_turn = registration.conversation_turn
        if order and not is_order_identifier(order):
            raise OperatorRefused("ORDER_INVALID")
        operator_scope(caller, allow_empty=not order)
        current = authorize()
        if current.scope_fingerprint != caller.scope_fingerprint:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        base, proxy = canonical_gateway_url(base), canonical_gateway_url(proxy)
        manifest_text = trial_manifest if trial_manifest is not None else binding.manifest_text
        if json.loads(manifest_text).get("schema") != operator_agent.SCHEMA:
            raise OperatorRefused("OPERATOR_PUBLICATION_UPGRADE_REQUIRED")
        manifest = execution_manifest.from_text(manifest_text)
        order_site_id = ""
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
            if candidate and len(self.candidate_authorizations) >= 20:
                raise OperatorRefused("OPERATOR_BUSY")
            config = AgentRunConfig.load(Path(binding.config_file))
            store = self.context.agent_run_store
            run = store.issue(
                scope=caller,
                order_no="",
                config=config,
                gateway_version=__import__("aiops_diagnostics").__version__,
                expected_agent_version=manifest.identity,
                chat_binding=binding.key,
                execution_manifest=manifest_text,
                route_revision=binding.route_revision,
                publication_app_id=binding.app_id,
                operation="operator_agent",
                language=language,
            )
            attachment_rows = list(attachments or [])
            qa = self.context.store.create_assistant_question(
                caller.scope_fingerprint,
                question,
                internal_run_id=PREFIX + order,
                tenant_id=caller.effective_tenant_id,
                agent_id=agent_id,
                attachment_ids=[str(item["file_id"]) for item in attachment_rows],
                attempt_no=attempt_no,
                parent_qa_id=parent_qa_id,
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
                selected_order=order,
                attachments=attachment_rows,
                question=question,
                language=language,
                trial=trial_manifest is not None,
                candidate=candidate,
                qa_id=qa["qa_id"],
                agent_id=agent_id,
                attempt_no=max(1, int(attempt_no)),
                parent_qa_id=parent_qa_id,
                claim_id=getattr(registration, "claim_id", None),
            )
            qa_events = getattr(self.context, "qa_events", None)
            if qa_events is not None:
                from aiops_diagnostics.publication.agent_content import operator_agent_brief

                qa_events.append(
                    qa["qa_id"],
                    "run_started",
                    {
                        "qa_id": qa["qa_id"],
                        "conversation_id": conversation_turn[0] if conversation_turn else None,
                        "turn_no": conversation_turn[2] if conversation_turn else None,
                        "attempt_no": max(1, int(attempt_no)),
                        "agent": operator_agent_brief(language, agent_id),
                        "started_at": qa["created_at"],
                    },
                )
                if attachment_rows:
                    names = ", ".join(str(item["file_name"]) for item in attachment_rows[:3])
                    self._trace(run.run_id, "attachment_parse", "running", summary=names)
                    self._trace(
                        run.run_id,
                        "attachment_parse",
                        "succeeded",
                        duration_ms=0,
                        summary=f"已加载 {len(attachment_rows)} 份附件上下文",
                    )
            if candidate:
                self.candidate_authorizations[run.run_id] = (authorize, bearer, time.monotonic() + 300)
                expiry = threading.Timer(300, self._expire_candidate, args=(run.run_id,))
                expiry.daemon = True
                expiry.start()
        self.context.runtime._qa_registrations[qa["qa_id"]] = registration
        try:
            future = self.context.runtime._executor.submit(self._worker, run.run_id, qa["qa_id"], config)
        except Exception:  # noqa: BLE001 - 提交失败不能留下带公司凭据的待执行任务。
            self.context.agent_run_store.fail(run.run_id, "operator_schedule_failed")
            self.context.store.update_assistant_question(qa["qa_id"], status="failed", error_code="QA_FAILED")
            self.context.runtime._qa_registrations.pop(qa["qa_id"], None)
            with self.lock:
                self.tasks.pop(run.run_id, None)
                self.candidate_authorizations.pop(run.run_id, None)
            raise
        self.context.runtime._futures[qa["qa_id"]] = future
        future.add_done_callback(lambda _: self.context.runtime._futures.pop(qa["qa_id"], None))
        future.add_done_callback(lambda _: self.context.runtime._qa_registrations.pop(qa["qa_id"], None))
        return qa

    def publication(self, run_id, token):
        task, run, _ = self.authorize_execution(run_id, node_token=token)
        if (
            not diagnoses_an_order(run)
            and run.state == "issued"
            and not self.context.agent_run_store.claim(run_id)
        ):
            raise OperatorRefused("OPERATOR_CLAIM_CONFLICT")
        return {
            "run_id": run_id,
            "manifest": run.execution_manifest,
            "sha256": run.expected_agent_version,
            "operation": run.operation,
            "messages": task.messages,
        }

    def grant(self, run_id, token, manifest, tool, contract="", arguments=None):
        task = self.tasks.get(run_id)
        try:
            return self._grant(run_id, token, manifest, tool, contract, arguments)
        except OperatorRefused as exc:
            self._note_refusal(task, tool, exc)
            raise

    def _grant(self, run_id, token, manifest, tool, contract, arguments):
        if contract != (OBJECT_CONTRACT if _declared(tool).kind == "object" else TOOL_CONTRACT):
            raise OperatorRefused("OPERATOR_TOOL_CONTRACT_OUTDATED")
        task, run, _ = self.authorize_execution(run_id, node_token=token, manifest=manifest, tool=tool)
        arguments = arguments or {}
        from aiops_diagnostics.operator import offline_tool

        if tool == offline_tool.NAME:
            arguments = offline_tool.arguments(self, task, run, arguments)
        elif tool in {"cpms_order_amount_check", "cpms_order_process_stats"}:
            if arguments or not run.order_no:
                raise OperatorRefused("OPERATOR_ANALYSIS_INVALID")
        elif "order_no" in _declared(tool).inputs:
            if set(arguments) - {"order_no"}:
                raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
            _, _, observed_orders = offline_tool.selectors(offline_tool.prior_data(self, task))
            requested_order = arguments.get("order_no")
            reference_text = task.question
            if task.attachments:
                from aiops_diagnostics.operator.attachments import attachment_reference_text

                extra_text = attachment_reference_text(task.attachments)
                if extra_text:
                    reference_text = reference_text + "\n" + extra_text
            candidate = operator_agent.order_candidate(
                arguments,
                reference_text,
                task.selected_order,
                requested_order
                if len(observed_orders) == 1 and requested_order in observed_orders
                else (run.order_no or task.active_order),
            )
            with task.lock:
                if run.order_no and run.order_no != candidate:
                    raise OperatorRefused("OPERATOR_ORDER_CONFLICT")
                detail = self._authorized_order(task, candidate, expected_site=task.order_site_id)
                if not self.context.agent_run_store.bind_operator_order(run_id, candidate):
                    raise OperatorRefused("OPERATOR_ORDER_CONFLICT")
                task.order_site_id = detail["data"]["siteId"]
                run = self.context.agent_run_store.get(run_id)
                arguments = {"order_no": candidate}
        else:
            arguments = validate_tool_arguments(tool, arguments, task.question)
        if tool == "cpms_station_plan":
            previous = self._previous_plan(task)
            changed = any(
                key in previous and arguments.get(key, previous[key]) != previous[key]
                for key in ("name", "date")
            )
            if previous and arguments and not changed:
                arguments = {**previous, **arguments}
        task.arguments.setdefault(tool, arguments)
        if task.arguments[tool] != arguments:
            raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_CHANGED")
        if run.state == "issued" and not self.context.agent_run_store.claim(run_id):
            raise OperatorRefused("OPERATOR_CLAIM_CONFLICT")
        values = run_values(tool, run.order_no, arguments)
        return {
            "run_id": run_id,
            "tool": tool,
            "manifest_id": run.expected_agent_version,
            "authorization_id": hashlib.sha256(task.grant.encode()).hexdigest(),
            "tenant_id": task.caller.effective_tenant_id,
            "site_ids": self._grant_sites(task, run, tool),
            "inputs": tool_inputs(tool, values),
            "request": tool_request(tool, values),
            "contract": contract,
            "base_url": task.proxy_url + "/v1/internal/operator-runs/" + run_id + "/cpms",
            "bearer": task.grant,
            "expires_at": run.expires_at.isoformat(),
        }

    def record(self, run_id, token, manifest, tool, payload):
        task = self.tasks.get(run_id)
        try:
            return self._record(run_id, token, manifest, tool, payload)
        except OperatorRefused as exc:
            self._note_refusal(task, tool, exc)
            raise

    def _record(self, run_id, token, manifest, tool, payload):
        if set(payload) != {"authorization_id", "evidence"}:
            raise OperatorRefused("OPERATOR_INVALID_BODY")
        run, config = self.authenticate(run_id, node_token=token, manifest=manifest)
        if run.state == "succeeded":
            return {
                "run_id": run_id,
                "tool": tool,
                "authorization_id": payload["authorization_id"],
                "accepted": True,
            }
        dead = run.state not in {"issued", "running"}
        task = self.tasks.get(run_id)
        if not dead:
            try:
                task, run, config = self._authorize_execution(
                    run_id, run=run, config=config, node_token=token, manifest=manifest, tool=tool
                )
            except OperatorRefused as exc:
                self._stamp_gate_refusal(run_id, str(exc))
                latest = self.context.agent_run_store.get(run_id)
                if latest is None or latest.state in {"issued", "running"}:
                    raise
                dead, run = True, latest
        if dead:
            evidence = payload.get("evidence")
            if not isinstance(evidence, dict) or evidence.get("tool") != tool:
                raise OperatorRefused("OPERATOR_EVIDENCE_MISMATCH")
            result = self._absorb_superseded(
                run_id,
                {"evidence": {tool: {k: v for k, v in evidence.items() if k != "response"}}},
            )
            return {
                **result,
                "tool": tool,
                "authorization_id": payload["authorization_id"],
                "accepted": True,
            }
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
                or set(evidence) != {"tool", "inputs", "source", "observed_at", "data"}
                or evidence.get("tool") != tool
                or _provenance(evidence.get("data")) != _provenance(expected["data"])
                or evidence.get("source") != expected["source"]
                or evidence.get("inputs")
                != tool_inputs(tool, run_values(tool, run.order_no, task.arguments.get(tool)))
            ):
                raise OperatorRefused("OPERATOR_EVIDENCE_MISMATCH")
            try:
                observed = datetime.fromisoformat(evidence["observed_at"])
                if observed.utcoffset() is None or not run.claimed_at <= observed <= datetime.now(UTC):
                    raise ValueError("invalid observation time")
            except (ValueError, TypeError) as exc:
                raise OperatorRefused("OPERATOR_EVIDENCE_MISMATCH") from exc
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
                result = self._absorb_superseded(run_id, partial)
                return {
                    **result,
                    "tool": tool,
                    "authorization_id": payload["authorization_id"],
                    "accepted": True,
                }
            return {
                "run_id": run_id,
                "tool": tool,
                "authorization_id": payload["authorization_id"],
                "accepted": True,
            }

    def stream_chunk(self, run_id, token, manifest, delta, *, chunk_index=None):
        """接收执行节点推送的最终答复阶段增量文本分片并写入短期事件流。"""
        task, run, _ = self.authorize_execution(run_id, node_token=token, manifest=manifest)
        if not isinstance(delta, str) or not delta:
            raise OperatorRefused("OPERATOR_INVALID_BODY")
        from aiops_diagnostics.common.redaction import contains_secret

        if contains_secret(delta, (task.bearer, task.grant)):
            raise OperatorRefused("OPERATOR_SECRET_IN_STREAM")
        elapsed_ms = max(0, int((time.monotonic() - task.started_at) * 1000))
        qa_events = getattr(self.context, "qa_events", None)
        seq = None
        if qa_events is not None and run.qa_id:
            payload: dict = {"delta": delta, "elapsed_ms": elapsed_ms}
            if chunk_index is not None:
                payload["chunk_index"] = int(chunk_index)
            event = qa_events.append(run.qa_id, "message_delta", payload)
            seq = event["seq"]
        return {"run_id": run_id, "accepted": True, "seq": seq}

    def complete(self, run_id, token, manifest, payload):
        task = self.tasks.get(run_id)
        with task.completion_lock if task is not None else threading.Lock():
            return self._complete(run_id, token, manifest, payload)

    def _complete(self, run_id, token, manifest, payload):
        if set(payload) - {"status", "answer", "reason"}:
            raise OperatorRefused("OPERATOR_INVALID_BODY")
        run, config = self.authenticate(run_id, node_token=token, manifest=manifest)
        store = self.context.agent_run_store
        if payload.get("status") not in {"completed", "failed"}:
            raise OperatorRefused("OPERATOR_INVALID_COMPLETION")
        model_failure = None
        if payload["status"] == "failed":
            reason = payload.get("reason")
            if not isinstance(reason, str) or not (
                reason in NODE_FAILURE_CODES
                or re.fullmatch(TOOL_REFUSED_PREFIX + r"[A-Za-z0-9_]{1,80}", reason)
            ):
                reason = "node_failed"
            task = self.tasks.get(run_id)
            if (
                reason in {"model_failed", "model_quota_exhausted", "model_stream_incomplete"}
                and run.state in {"issued", "running"}
                and task is not None
                and task.delivered
            ):
                model_failure = reason
            else:
                store.fail(run_id, "operator_node_failed: " + reason)
            logging.getLogger("aiops.operator").warning(
                "operator_node_reported_failure reason=%s", reason or "-"
            )
            if model_failure is None:
                return {"run_id": run_id, "state": "failed"}
        raw = payload.get("answer", "") if model_failure is None else None
        if model_failure is None and (not isinstance(raw, str) or not 0 < len(raw.strip()) <= 12000):
            raise OperatorRefused("OPERATOR_INVALID_ANSWER")
        if run.state == "succeeded":
            return {"run_id": run_id, "state": "succeeded", "delivered": True}
        dead = run.state not in {"issued", "running"}
        task = self.tasks.get(run_id)
        if not dead:
            try:
                task, run, config = self._authorize_execution(
                    run_id, run=run, config=config, node_token=token, manifest=manifest
                )
            except OperatorRefused as exc:
                self._stamp_gate_refusal(run_id, str(exc))
                latest = store.get(run_id)
                if latest is None or latest.state in {"issued", "running"}:
                    raise
                dead, run = True, latest
        if dead:
            return self._absorb_superseded(run_id, {"answer": raw})
        answer = raw
        if task is None:
            raise OperatorRefused("OPERATOR_RUN_INACTIVE")
        with task.lock:
            from aiops_diagnostics.common.redaction import contains_secret

            if answer is not None and contains_secret(answer, (task.bearer, task.grant)):
                _answer_refused("credential")
            if run.order_no:
                self._authorized_order(task, run.order_no, expected_site=task.order_site_id)
            evidence = {
                name: {k: v for k, v in item.items() if k != "response"}
                for name, item in task.evidence.items()
                if name in task.delivered
            }
            from aiops_diagnostics.operator import offline_tool

            if offline_tool.NAME in evidence:
                try:
                    offline_tool.require_current(
                        self, task, evidence[offline_tool.NAME]["offline_references"]
                    )
                except SourceError as exc:
                    raise OperatorRefused("OPERATOR_SCOPE_CHANGED") from exc
            result = (
                operator_agent.render(answer, evidence, task.language, attachments=task.attachments)
                if answer is not None
                else None
            )

        history_evidence = []
        for previous_id in task.history_runs:
            previous = store.peek(previous_id)
            if previous is None or not previous.snapshot:
                raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
            self._authorize_snapshot(task, previous)
            history_evidence.append(previous.snapshot.get("evidence", {}))
        if model_failure is not None:
            review = {"verdict": "unavailable", "reason": model_failure}
        else:
            self._trace(run_id, "review")
            review_started = time.monotonic()
            review = operator_answer_review.review(
                result["blocks"][0]["text"],
                evidence,
                task.messages[:-1],
                task.question,
                task.language,
                self.context.runtime.diagnostic_settings.agent,
                history_evidence,
                tool_refusals=list(task.refusals),
            )
            self._trace(
                run_id,
                "review",
                "succeeded" if review["verdict"] != "unavailable" else "refused",
                duration_ms=(time.monotonic() - review_started) * 1000,
                detail=review.get("reason") or ("allowed" if review["verdict"] == "allow" else None),
            )
        task.answer_review = review
        if model_failure is not None:
            from aiops_diagnostics.operator.operator_evidence import render_evidence

            result = render_evidence(
                evidence, task.language, secrets=(task.bearer, task.grant), attachments=task.attachments
            )
            if result is None:
                store.complete(
                    run_id,
                    snapshot={"answer": None, "answer_review": review, "evidence": evidence},
                    actual_node_id=run.expected_node_id,
                    node_build=None,
                )
                raise OperatorRefused("OPERATOR_ANSWER_NOT_GROUNDED")
            answer = None
        superseded = {
            "answer": answer,
            "answer_review": review,
            "evidence": evidence,
        }
        with task.lock:
            try:
                task, run, config = self._authorize_execution(
                    run_id, run=run, config=config, node_token=token, manifest=manifest
                )
                if run.order_no:
                    self._authorized_order(task, run.order_no, expected_site=task.order_site_id)
                if offline_tool.NAME in evidence:
                    try:
                        offline_tool.require_current(
                            self, task, evidence[offline_tool.NAME]["offline_references"]
                        )
                    except SourceError as exc:
                        raise OperatorRefused("OPERATOR_SCOPE_CHANGED") from exc
                for previous_id in task.history_runs:
                    previous = self.context.agent_run_store.peek(previous_id)
                    if previous is None or not previous.snapshot:
                        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
                    self._authorize_snapshot(task, previous)
            except OperatorRefused as exc:
                task.completion_refusal = str(exc)
                self._stamp_gate_refusal(run_id, str(exc))
                latest = store.get(run_id)
                if latest is None or latest.state in {"issued", "running"}:
                    raise
                return self._absorb_superseded(run_id, superseded)
            snapshot = {
                "answer": answer,
                "trial": task.trial,
                "evidence": evidence,
                "language": task.language,
                "answer_review": review,
                "delivery_mode": "diagnosis" if answer is not None else "evidence_only",
                "history_runs": task.history_runs,
                "rendered_result": result,
                "plan_arguments": task.arguments.get("cpms_station_plan", {}),
                "tool_refusals": list(task.refusals),
            }
            if not store.complete(
                run_id, snapshot=snapshot, actual_node_id=run.expected_node_id, node_build=None
            ):
                return self._absorb_superseded(run_id, superseded)
            verdict, completed = store.complete_loop(
                run_id, observed_agent_version=manifest, actual_node_id=run.expected_node_id
            )
            if verdict not in {"completed", "already_completed"}:
                latest = store.get(run_id)
                if latest is not None and latest.state in {"failed", "revoked", "expired"}:
                    return self._absorb_superseded(run_id, superseded)
                raise OperatorRefused("OPERATOR_COMPLETE_REJECTED")
            return {"run_id": run_id, "state": completed.state}

    def _untouched(self, run_id, task) -> bool:
        del task
        run = self.context.agent_run_store.get(run_id)
        return run is not None and run.state in {"issued", "running"}

    def _raise_node_failure(self, run_id):
        run = self.context.agent_run_store.get(run_id)
        if run is not None and run.state == "failed":
            reason = (run.reason or "").removeprefix("operator_node_failed: ")
            if reason in NODE_FAILURE_CODES:
                raise OperatorRefused(NODE_FAILURE_CODES[reason])
            if reason.startswith(TOOL_REFUSED_PREFIX):
                raise OperatorRefused("OPERATOR_DEPENDENCY_UNAVAILABLE")

    def _worker(self, run_id, qa_id, config):
        store = self.context.store
        try:
            if not store.update_assistant_question(qa_id, status="running"):
                raise OperatorRefused("OPERATOR_RUN_INACTIVE")
            dispatcher = SnailDispatcher()
            run = self.context.agent_run_store.get(run_id)
            dispatcher.deadline_at = run.expires_at
            try:
                manifest = json.loads(run.execution_manifest or "{}")
                manifest_tool_count = len(manifest.get("tools") or []) if isinstance(manifest, dict) else 0
            except (TypeError, ValueError):
                manifest_tool_count = 0
            task = self.tasks[run_id]
            self._trace(run_id, "authorize", "running")
            task.messages = self._messages(task)
            self._trace(run_id, "authorize", "succeeded", duration_ms=0)
            dispatcher.question = task.question
            dispatch_started_at = time.monotonic()
            while True:
                try:
                    dispatch_started_at = time.monotonic()
                    self._trace(
                        run_id,
                        "dispatch",
                        detail=f"tools={manifest_tool_count}" if manifest_tool_count else None,
                    )
                    dispatcher.dispatch(config, run_id)
                except AgentRunDispatchError:
                    self._raise_node_failure(run_id)
                    if self.context.agent_run_store.get(run_id).state == "succeeded":
                        break
                    if task.completion_refusal:
                        raise OperatorRefused(task.completion_refusal) from None
                    if not self._untouched(run_id, task):
                        raise
                    if task.resamples >= MAX_RESAMPLES:
                        raise
                    task.resamples += 1
                    self._trace(run_id, "resample", detail="dispatch_refused")
                    logging.getLogger("aiops.operator").warning(
                        "operator_resample attempt=%d reason=dispatch_refused", task.resamples
                    )
                    continue
                self._raise_node_failure(run_id)
                if not self._untouched(run_id, task):
                    break
                if task.completion_refusal:
                    raise OperatorRefused(task.completion_refusal)
                if task.resamples >= MAX_RESAMPLES:
                    raise OperatorRefused("OPERATOR_MODEL_OUTPUT_UNSTABLE")
                task.resamples += 1
                self._trace(run_id, "resample", detail="no_answer_accepted")
                logging.getLogger("aiops.operator").warning(
                    "operator_resample attempt=%d reason=no_answer_accepted", task.resamples
                )
            self._trace(
                run_id,
                "dispatch",
                "succeeded",
                duration_ms=max(0, int((time.monotonic() - dispatch_started_at) * 1000)),
            )
            task = self.tasks[run_id]
            current = task.authorize()
            result = self.authorize_readback(
                current, qa_id, task.bearer, candidate_authorize=task.authorize if task.candidate else None
            )
            if store.update_assistant_question(qa_id, status="completed", result=result):
                self._trace(run_id, "completed", "succeeded")
                completed_run = self.context.agent_run_store.get(run_id)
                snapshot = (completed_run.snapshot or {}) if completed_run else {}
                delivery_mode = str(snapshot.get("delivery_mode") or "diagnosis")
                from aiops_diagnostics.operator.qa_events import project_steps_from_trace, replace_notice

                steps = project_steps_from_trace(
                    completed_run.trace if completed_run else [], language=task.language
                )
                elapsed_ms = (
                    task.finished_elapsed_ms
                    if task.finished_elapsed_ms is not None
                    else max(0, int((time.monotonic() - task.started_at) * 1000))
                )
                citations = list((result or {}).get("citations") or [])
                qa_events = getattr(self.context, "qa_events", None)
                if qa_events is not None:
                    final_text = "\n\n".join(
                        str(b.get("text") or "")
                        for b in (result or {}).get("blocks", [])
                        if b.get("kind") == "text"
                    )
                    needs_replace = delivery_mode == "evidence_only" or (
                        qa_events.has_delta_events(qa_id)
                        and qa_events.streamed_text(qa_id).strip() != final_text.strip()
                    )
                    closing_events: list[tuple[str, dict]] = []
                    if needs_replace:
                        closing_events.append(
                            (
                                "message_replace",
                                {
                                    "delivery_mode": delivery_mode,
                                    "blocks": list((result or {}).get("blocks") or []),
                                    "notice": replace_notice(
                                        task.language, evidence_only=delivery_mode == "evidence_only"
                                    ),
                                },
                            )
                        )
                    if citations:
                        closing_events.append(("citations", {"citations": citations}))
                    closing_events.append(
                        (
                            "message_done",
                            {
                                "status": "completed",
                                "elapsed_ms": elapsed_ms,
                                "delivery_mode": delivery_mode,
                                "reasoning_duration_ms": elapsed_ms,
                                "step_count": len(steps),
                                "citation_count": len(citations),
                                "result": result,
                            },
                        )
                    )
                    qa_events.append_many(qa_id, closing_events)
                active_order_no = completed_run.order_no if completed_run and completed_run.order_no else None
                self.context.runtime._complete_conversation_turn(
                    task.conversation_turn,
                    task.question,
                    result,
                    status_qa_id=qa_id,
                    active_order_no=active_order_no,
                    attempt_no=task.attempt_no,
                    parent_qa_id=task.parent_qa_id,
                    attachment_ids=[str(item["file_id"]) for item in task.attachments]
                    if task.attachments
                    else None,
                    citations=citations,
                    steps=steps,
                    elapsed_ms=elapsed_ms,
                    delivery_mode=delivery_mode,
                    preserve_completed=task.attempt_no > 1,
                    claim_id=task.claim_id,
                )
        except Exception as exc:  # noqa: BLE001 - 后台失败必须收敛，凭据及上游正文不进日志。
            failure_code = _failure_code(exc)
            failed_task = self.tasks.get(run_id)
            if failed_task is not None and ("route", "dispatch_agent") in failed_task.open_steps:
                self._trace(run_id, "dispatch", "failed")
            self._trace(run_id, "failed", "failed", detail=failure_code)
            reason = _failure_reason(exc)
            if not self.context.agent_run_store.fail(run_id, failure_code):
                self.context.agent_run_store.refuse(run_id, failure_code)
            store.update_assistant_question(qa_id, status="failed", error_code=_failure_code(exc))
            qa_events = getattr(self.context, "qa_events", None)
            task = self.tasks.get(run_id)
            if qa_events is not None and not qa_events.has_terminal_event(qa_id):
                elapsed_ms = (
                    max(0, int((time.monotonic() - task.started_at) * 1000)) if task is not None else 0
                )
                qa_events.append(
                    qa_id,
                    "run_failed",
                    {
                        "status": "failed",
                        "elapsed_ms": elapsed_ms,
                        "error": {
                            "code": failure_code,
                            "message": failure_code,
                            "retryable": failure_code != "OPERATOR_MODEL_QUOTA_EXHAUSTED",
                        },
                    },
                )
            logging.getLogger("aiops.operator").warning(
                "operator_run_failed reason=%s code=%s", reason, _failure_code(exc)
            )
            if task is not None:
                self.context.runtime._release_conversation_turn(
                    task.conversation_turn,
                    preserve_completed=task.attempt_no > 1,
                    claim_id=task.claim_id,
                )
        finally:
            with self.lock:
                self.tasks.pop(run_id, None)

    def result(self, caller, qa_id):
        run = self.context.agent_run_store.by_qa(qa_id)
        if (
            run is None
            or run.scope_fingerprint != caller.scope_fingerprint
            or run.operation not in {SCENARIO, operator_conversation.SCENARIO, "operator_agent"}
        ):
            raise OperatorRefused("QA_NOT_FOUND")
        binding = chat_execution.binding_for_key(self.context.settings, run.chat_binding)
        if not still_authorized(binding, run, trial=bool((run.snapshot or {}).get("trial"))) or expired(run):
            raise OperatorRefused("OPERATOR_RESULT_RETRACTED")
        config = AgentRunConfig.load(Path(binding.config_file))
        if execution_manifest.from_text(run.execution_manifest).identity != run.expected_agent_version:
            raise OperatorRefused("OPERATOR_MANIFEST_MISMATCH")
        run = self.context.agent_run_store.get(run.run_id)
        if run is None or not deliverable(run) or not run.exec_config_confirmed or not run.snapshot:
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
        if operator_agent.is_agent(run):
            scope = operator_scope(caller, allow_empty=True)
            for item in run.snapshot.get("evidence", {}).values():
                data = item["data"]
                sites = [data.get("siteId"), data.get("site_id")]
                if item["tool"] == SITE_TOOL:
                    sites.extend(row["id"] for row in data.get("records", []))
                if scope.site_ids is not None and any(site and site not in scope.site_ids for site in sites):
                    raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
            return run.snapshot["rendered_result"]
        scope = operator_scope(caller, allow_empty=not run.order_no)
        evidence = run.snapshot.get("evidence", {})
        if run.order_no:
            site = evidence.get(TOOL_NAMES[0], {}).get("data", {}).get("siteId")
            if not site or (scope.site_ids is not None and site not in scope.site_ids):
                raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        if run.snapshot.get("rendered_result"):
            return run.snapshot["rendered_result"]
        if run.snapshot.get("rendered_report"):
            return {
                "blocks": [{"kind": "text", "text": run.snapshot["rendered_report"]}],
                "retrieval_status": "unavailable",
            }
        raise OperatorRefused("OPERATOR_RESULT_UNAVAILABLE")
