"""管家节点任务：凭据留在 Gateway，按单次任务转发固定 CPMS 只读接口。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from aiops_diagnostics.common.async_job_lifecycle import QUESTION
from aiops_diagnostics.common.bounded_http import (
    ErrorMapping,
    RequestSpec,
    request_json,
)
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
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
    communication,
    cpms_sites,
    operator_agent,
    operator_answer_review,
    operator_conversation,
    operator_tools,
    order_pricing,
)
from aiops_diagnostics.operator.cpms_evidence import (
    _TIMELINE_DEVICE,
    DERIVED_EVIDENCE_FIELDS,  # noqa: F401
    DETAIL_FIELDS,  # noqa: F401
    FEEDBACK_REFUSALS,
    NODE_FAILURE_CODES,
    OBJECT_CONTRACT,
    PREFIX,
    SAMPLE_FIELDS,  # noqa: F401
    SCENARIO,
    SITE_TOOL,
    TIMELINE_FIELDS,
    TIMELINE_LIMIT,
    TIMELINE_TOOL,
    TOOL_CONTRACT,
    TOOL_CONTRACT_HEADER,  # noqa: F401
    TOOL_NAMES,
    TOOL_REFUSED_PREFIX,
    TOOL_SUMMARY_FIELDS,
    _answer_refused,
    _arg_summary,
    _declared,
    _failure_code,
    _failure_reason,
    _provenance,
    _proxy_tool,
    _timeline_time,
    allowed_in,
    cpms_get,
    diagnoses_an_order,
    minimized_response,
    operator_scope,
    project_evidence,
    run_values,
    still_authorized,
    tool_inputs,
    tool_request,
    tsdata_query,
    validate_body,
    validate_tool_arguments,
)
from aiops_diagnostics.operator.cpms_sites import (
    SITE_MAX_PAGES,  # noqa: F401
    SITE_MAX_SHOWN,  # noqa: F401
    SITE_PAGE_SIZE,  # noqa: F401
    SITE_PATH,  # noqa: F401
    SiteKeywords,
    SiteScope,  # noqa: F401
    query_sites,
    read_site_pages,  # noqa: F401
    read_site_tool,
    site_keywords,  # noqa: F401
    site_page,  # noqa: F401
    site_scope,
    site_tool_result,  # noqa: F401
    tool_site_keywords,  # noqa: F401
)
from aiops_diagnostics.operator.operator_conversation import OperatorRefused

# 同一run最多一次有界重派，复用其已经读取的证据。
MAX_RESAMPLES = 1


def cpms_site_reader(base: str, bearer: str):
    """直连 CPMS 的站点页读取函数（经模块内 `cpms_get` 转发，保留测试替身入口）。"""
    return cpms_sites.cpms_site_reader(base, bearer, getter=cpms_get)


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
    #: 每个工具这一次的取值（订单号或模型提议的关键词）。放行一次就固定，后续步骤只读它。
    arguments: dict = field(default_factory=dict)
    conversation_turn: tuple | None = None
    selected_order: str = ""
    active_order: str = ""
    messages: list = field(default_factory=list)
    history_runs: list = field(default_factory=list)
    question: str = ""
    language: str = "zh"
    trial: bool = False
    candidate: bool = False
    #: 本次运行可接受答复未完成时最多重派一次。
    #: 放在任务上而不是模块上：它属于这一次 run，跨 run 复用就没有"有界"可言了。
    resamples: int = 0
    #: 本轮已回喂模型的业务拒绝（`FEEDBACK_REFUSALS` 里的码）。答复复核据此区分
    #: 「查无此单」与「编造」——被拒的调用不产生证据，模型只能凭这条记录解释结局。
    refusals: list = field(default_factory=list)
    lock: object = field(default_factory=threading.RLock)
    completion_lock: object = field(default_factory=threading.Lock)
    trace_lock: object = field(default_factory=threading.Lock)
    started_at: float = field(default_factory=time.monotonic)
    trace_failed: bool = False
    finished_elapsed_ms: int | None = None
    answer_review: dict = field(default_factory=dict)
    #: `_complete` 里的终局闸拒原码。派发成功但 run 未推进时 worker 据此归码——
    #: 授权/范围死了的同一 run 重派必撞同一道闸，不能再走「没交出答复」重采样。
    completion_refusal: str | None = None


class OperatorCpms:
    def __init__(self, context):
        self.context = context
        self.tasks = {}
        self.candidate_authorizations = {}
        self.lock = threading.RLock()

    def _trace(self, run_id, phase, status="running", *, tool=None, duration_ms=None, detail=None):
        task = self.tasks.get(run_id)
        if task is None or task.trace_failed:
            return
        with task.trace_lock:
            if task.finished_elapsed_ms is not None:
                return
            event = {
                "phase": phase,
                "status": status,
                "at": datetime.now(UTC).isoformat(),
                "elapsed_ms": max(0, int((time.monotonic() - task.started_at) * 1000)),
            }
            if phase in {"completed", "failed", "cancelled", "expired"}:
                task.finished_elapsed_ms = event["elapsed_ms"]
                duration_ms = event["elapsed_ms"]
            if tool:
                event["tool"] = tool
            if duration_ms is not None:
                event["duration_ms"] = max(0, int(duration_ms))
            if detail:
                # 只写机器码（拒绝码/复核原因/终止码），截断兜底防长文本进账本。
                event["detail"] = str(detail)[:64]
            try:
                self.context.agent_run_store.record_trace(run_id, event)
            except sqlite3.Error:
                # 观测失败不能打断业务收尾，尤其不能留下永久 running 的问题。
                task.trace_failed = True
                logging.getLogger("aiops.operator").warning("operator_trace_unavailable")

    def _summarize_tool(self, run_id, task, tool, *, refusal=None):
        """把该次工具调用的行级展示摘要写进运行记录（ADR-0251）。

        观测失败不阻断业务——与 ``_trace`` 同一约定；摘要字段的白名单
        （``TOOL_SUMMARY_FIELDS``）与有界写入（``record_tool_summary``）是两层闸。
        """
        if task is None or not tool:
            return
        try:
            fields = {}
            args = _arg_summary(task, tool)
            if args:
                fields["入参"] = args
            if refusal:
                # 拒绝码原文交给 store 的 32 字上界兜底，这里不再二次截断。
                fields["拒绝"] = str(refusal)
            data = (task.evidence.get(tool) or {}).get("data")
            if isinstance(data, dict):
                for source_key, label in TOOL_SUMMARY_FIELDS.get(tool, ()):
                    if source_key in data:
                        fields[label] = data[source_key]
            elif isinstance(data, list):
                fields["结果"] = f"{len(data)}项"
            if fields:
                self.context.agent_run_store.record_tool_summary(run_id, tool, fields)
        except (sqlite3.Error, ValueError, TypeError):
            logging.getLogger("aiops.operator").warning("operator_tool_summary_unavailable")

    def progress(self, caller, qa_id, qa=None):
        """只在调用方完成当前入口授权后读取；不包含业务对象或答案。"""
        run = self.context.agent_run_store.by_qa(qa_id)
        if run is None or not operator_agent.is_agent(run):
            return None
        caller = self.refresh_scope(caller, allow_empty=True)
        if run.scope_fingerprint != caller.scope_fingerprint:
            return None
        task = self.tasks.get(run.run_id)
        if run.state in {"issued", "running"} and expired(run):
            run = self.context.agent_run_store.get(run.run_id)
        if run.state == "expired" and qa is not None and qa["status"] in QUESTION.active:
            self.context.store.update_assistant_question(qa_id, status="expired")
            qa.update(status="expired", result=None)
        terminal = run.state not in {"issued", "running", "succeeded"} or (
            qa is not None and qa["status"] == "completed"
        )
        phase = {"succeeded": "completed", "revoked": "cancelled"}.get(run.state, run.state)
        if terminal and task is not None:
            self._trace(run.run_id, phase, phase)
            run = self.context.agent_run_store.by_qa(qa_id)
        last = run.trace[-1] if run.trace else {}
        if not terminal:
            phase = last.get("phase", "queued")
            if phase == "tool" and last.get("status") != "running":
                phase = "generating"
        if task is not None and task.trace_failed:
            elapsed_ms = None
        elif terminal and task is not None and task.finished_elapsed_ms is not None:
            elapsed_ms = task.finished_elapsed_ms
        elif terminal and last.get("phase") in {"completed", "failed", "cancelled", "expired"}:
            elapsed_ms = last["elapsed_ms"]
        elif task is not None:
            elapsed_ms = int((time.monotonic() - task.started_at) * 1000)
        else:
            elapsed_ms = None  # 缺少持久终点时，不用另一种时钟猜出精确耗时。
        return {
            "phase": phase,
            "elapsed_ms": max(0, elapsed_ms) if elapsed_ms is not None else None,
            "tool_count": len({e["tool"] for e in run.trace if e.get("tool") and e["status"] == "succeeded"}),
            "resamples": sum(e["phase"] == "resample" for e in run.trace),
        }

    def console_identity(self, session):
        settings = self.context.settings
        if not settings.operator_trial_login_url or not settings.operator_trial_key or not session:
            raise OperatorRefused("OPERATOR_TRIAL_LOGIN_REQUIRED")

        def unavailable(_):
            return OperatorRefused("OPERATOR_TRIAL_LOGIN_REQUIRED")

        from aiops_diagnostics.common.endpoint_policy import canonical_gateway_url

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

        from aiops_diagnostics.gateway.environment_registry import RegistryError, require_registered
        from aiops_diagnostics.identity.scope_context import DataScope

        try:
            require_registered(self.context.runtime, caller.effective_tenant_id)
        except RegistryError as exc:
            raise OperatorRefused("TENANT_ENVIRONMENT_NOT_ENABLED") from exc
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

    def authorize_readback(self, caller, qa_id, bearer, *, candidate_authorize=None):
        run = self.context.agent_run_store.by_qa(qa_id)
        if candidate_authorize is not None:
            with self.lock:
                self._prune()
                lease = self.candidate_authorizations.get(run.run_id) if run else None
            if lease is None or candidate_authorize is not lease[0]:
                raise OperatorRefused("OPERATOR_TRIAL_SCOPE_MISMATCH")
            caller = candidate_authorize()
        else:
            caller = self.refresh_scope(caller, allow_empty=bool(run and not diagnoses_an_order(run)))
        if run is None or run.scope_fingerprint != caller.scope_fingerprint:
            raise OperatorRefused("QA_NOT_FOUND")
        if run.state != "succeeded" or not run.snapshot:
            raise OperatorRefused("OPERATOR_RESULT_UNAVAILABLE")
        from aiops_diagnostics.execution.node_publication import all_targets

        binding = chat_execution.binding_for_key(self.context.settings, run.chat_binding)
        # 窗口判据与 `result()` 同一条（`expired`）：过期与「授权已撤回」同码，
        # 不新增错误码；改的只是"仅凭行状态就放行"这件事。
        if not still_authorized(binding, run, trial=bool((run.snapshot or {}).get("trial"))) or expired(run):
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
        if operator_agent.is_agent(run):
            task = ActiveTask(
                caller,
                binding,
                candidate_authorize or (lambda: self.refresh_scope(caller, allow_empty=True)),
                bearer,
                target.cpms_base_url,
                target.proxy_base_url,
                "",
            )
            self._authorize_snapshot(task, run)
            for previous_id in run.snapshot.get("history_runs", []):
                previous = self.context.agent_run_store.peek(previous_id)
                if (
                    previous is None
                    or previous.scope_fingerprint != caller.scope_fingerprint
                    or not previous.snapshot
                ):
                    raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
                self._authorize_snapshot(task, previous)
            return self.result(caller, qa_id)
        if not diagnoses_an_order(run):
            plan = (run.snapshot or {}).get("evidence", {}).get("cpms_station_plan", {}).get("data", {})
            if plan.get("site_id"):
                body = cpms_get(
                    target.cpms_base_url + "/chSite/" + plan["site_id"], bearer, caller.effective_tenant_id
                )
                site = body.get("data") if isinstance(body, dict) and body.get("code") == 0 else None
                if (
                    not isinstance(site, dict)
                    or site.get("id") != plan["site_id"]
                    or site.get("tenantId") != caller.effective_tenant_id
                ):
                    raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
            return self.result(caller, qa_id)
        body = cpms_get(
            target.cpms_base_url
            + tool_request(TOOL_NAMES[0], run_values(TOOL_NAMES[0], run.order_no))["path"],
            bearer,
            caller.effective_tenant_id,
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
        candidate=False,
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
        from aiops_diagnostics.common.endpoint_policy import canonical_gateway_url

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
                selected_order=order,
                question=question,
                language=language,
                trial=trial_manifest is not None,
                candidate=candidate,
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

    def _authorized_order(self, task, order, *, expected_site=""):
        current = task.authorize()
        if current.scope_fingerprint != task.caller.scope_fingerprint:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        body = cpms_get(
            task.base_url + tool_request(TOOL_NAMES[0], {"order_no": order})["path"],
            task.bearer,
            current.effective_tenant_id,
        )
        validate_body(body, current, order, TOOL_NAMES[0])
        if expected_site and body["data"]["siteId"] != expected_site:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        if task.authorize().scope_fingerprint != current.scope_fingerprint:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        return body

    def _authorize_snapshot(self, task, run):
        from aiops_diagnostics.operator import offline_tool

        refs = run.snapshot.get("evidence", {}).get(offline_tool.NAME, {}).get("offline_references")
        if refs is not None:
            try:
                offline_tool.require_current(self, task, refs)
            except SourceError as exc:
                raise OperatorRefused("OPERATOR_SCOPE_CHANGED") from exc
        scope = operator_scope(task.authorize(), allow_empty=True)
        evidence = run.snapshot.get("evidence", {})
        if run.order_no:
            detail = evidence.get(TOOL_NAMES[0], {}).get("data", {})
            if not detail.get("siteId"):
                raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
            self._authorized_order(task, run.order_no, expected_site=detail["siteId"])
        for name, item in evidence.items():
            data = item["data"]
            sites = [row["id"] for row in data.get("records", [])] if name == SITE_TOOL else []
            if data.get("site_id"):
                sites.append(data["site_id"])
            for site in sites:
                if scope.site_ids is not None and site not in scope.site_ids:
                    raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
                body = cpms_get(
                    task.base_url + "/chSite/" + site, task.bearer, task.caller.effective_tenant_id
                )
                row = body.get("data") if isinstance(body, dict) and body.get("code") == 0 else None
                if not isinstance(row, dict) or (row.get("id"), row.get("tenantId")) != (
                    site,
                    task.caller.effective_tenant_id,
                ):
                    raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        if task.authorize().scope_fingerprint != task.caller.scope_fingerprint:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")

    def _messages(self, task):
        messages = []
        task.history_runs = []
        if task.conversation_turn:
            conversation, scope, before = task.conversation_turn
            store = self.context.runtime.conversation_store
            current = store.get(conversation, scope)
            task.active_order = current.get("active_order_no") or ""
            if task.active_order:
                try:
                    self._authorized_order(task, task.active_order)
                except OperatorRefused:
                    task.active_order = ""
            for turn in store.completed_context(conversation, scope, before=before):
                previous = self.context.agent_run_store.by_qa(turn.get("status_qa_id"))
                if not previous or previous.scope_fingerprint != scope or not previous.snapshot:
                    continue
                if previous.publication_app_id != task.binding.app_id:
                    continue
                try:
                    self._authorize_snapshot(task, previous)
                    inherited = previous.snapshot.get("history_runs", [])
                    if len(inherited) > 60:
                        raise OperatorRefused("OPERATOR_HISTORY_LIMIT")
                    for dependency in inherited:
                        source = self.context.agent_run_store.peek(dependency)
                        if source is None or source.scope_fingerprint != scope or not source.snapshot:
                            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
                        self._authorize_snapshot(task, source)
                except OperatorRefused as exc:
                    if str(exc) == "OPERATOR_HISTORY_LIMIT":
                        raise
                    continue
                answer = "\n".join(b.get("text", "") for b in (turn.get("answer") or {}).get("blocks", []))
                if len(answer) > 10000:
                    continue
                dependencies = [key for key in [previous.run_id, *inherited] if key not in task.history_runs]
                if len(task.history_runs) + len(dependencies) > 60:
                    raise OperatorRefused("OPERATOR_HISTORY_LIMIT")
                task.history_runs.extend(dependencies)
                messages.extend(
                    [
                        {"role": "user", "content": turn["question"][:4000]},
                        {"role": "assistant", "content": answer},
                    ]
                )
        messages.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "question": task.question,
                        "page_selected_order": task.selected_order,
                        "conversation_order": task.active_order,
                        "language": task.language,
                        "current_time": now_utc().isoformat(),
                        "business_timezone": self.context.runtime.diagnostic_settings.mysql.timezone,
                        "history_is_previous_observation": True,
                    },
                    ensure_ascii=False,
                ),
            }
        )
        while len(json.dumps(messages, ensure_ascii=False)) > 48000 and len(messages) > 1:
            del messages[:2]
        return messages

    def _prune(self):
        for key, (_, _, end) in list(self.candidate_authorizations.items()):
            if time.monotonic() >= end:
                del self.candidate_authorizations[key]
        for key in list(self.tasks):
            run = self.context.agent_run_store.get(key)
            if run is None or expired(run) or run.state in {"failed", "revoked", "expired"}:
                del self.tasks[key]

    def _expire_candidate(self, run_id):
        with self.lock:
            self.candidate_authorizations.pop(run_id, None)

    def authenticate(self, run_id, *, node_token=None, manifest=None):
        """协议级闸：run 存在、令牌属实为本 run 的期望节点、manifest 匹配。**所有**节点回调都过。"""
        try:
            return self._authenticate(run_id, node_token=node_token, manifest=manifest)
        except OperatorRefused as exc:
            self._stamp_gate_refusal(run_id, str(exc))
            raise

    def authorize_execution(self, run_id, *, node_token=None, manifest=None, tool=None):
        """执行级闸：协议身份之上，「本次执行仍被许可」。只包铸造/使用执行能力的面。"""
        try:
            run, config = self._authenticate(run_id, node_token=node_token, manifest=manifest)
            return self._authorize_execution(
                run_id, run=run, config=config, node_token=node_token, manifest=manifest, tool=tool
            )
        except OperatorRefused as exc:
            self._stamp_gate_refusal(run_id, str(exc))
            raise

    def _stamp_gate_refusal(self, run_id, code):
        """闸拒把原始码写进 run.reason：授权已死时上报必被同一道闸反杀，归因只剩这里能留。

        fail() 只更新 issued/running——首拒生效、终局幂等，第二次闸拒不改写已落码。
        两类不 stamp：OPERATOR_TOOL_DISABLED 的上报不带 tool、走不到这个判定，
        现有 self-report 路径仍在；FEEDBACK_REFUSALS 是回喂业务码、拒绝点在闸外。
        """
        if code == "OPERATOR_TOOL_DISABLED" or code in FEEDBACK_REFUSALS:
            return
        try:
            if self.context.agent_run_store.fail(run_id, TOOL_REFUSED_PREFIX + code):
                logging.getLogger("aiops.operator").warning(
                    "operator_gate_refused run_id=%s code=%s", run_id, code
                )
        except Exception:
            # 归因动作不能让拒绝本身变形：stamp 失败只留日志，拒绝原样抛回节点。
            logging.getLogger("aiops.operator").warning(
                "operator_gate_refused stamp_failed run_id=%s code=%s", run_id, code
            )

    def _authenticate(self, run_id, *, node_token=None, manifest=None):
        """run 存在 + 节点身份 + manifest——不判断「这次执行还许不许可」。"""
        run = self.context.agent_run_store.get(run_id)
        if run is None or run.operation not in {
            SCENARIO,
            operator_conversation.SCENARIO,
            "operator_agent",
        }:
            raise OperatorRefused("OPERATOR_RUN_INACTIVE")
        binding = chat_execution.binding_for_key(self.context.settings, run.chat_binding)
        if binding is None:
            # 绑定已撤：没有 config 可验证节点身份，连「谁在上报」都存疑。
            raise OperatorRefused("OPERATOR_NODE_REJECTED")
        config = AgentRunConfig.load(Path(binding.config_file))
        if node_token is not None:
            node = config.authenticate_node(node_token)
            if node is None or node.node_id != run.expected_node_id or node.environment != run.environment:
                raise OperatorRefused("OPERATOR_NODE_REJECTED")
        expected = execution_manifest.from_text(run.execution_manifest)
        if expected.identity != run.expected_agent_version:
            raise OperatorRefused("OPERATOR_MANIFEST_MISMATCH")
        if manifest is not None and manifest != expected.identity:
            raise OperatorRefused("OPERATOR_MANIFEST_MISMATCH")
        return run, config

    def _authorize_execution(self, run_id, *, run, config, node_token=None, manifest=None, tool=None):
        task = self.tasks.get(run_id)
        if task is None or run.state not in {"issued", "running"}:
            raise OperatorRefused("OPERATOR_RUN_INACTIVE")
        qa = self.context.store.get_assistant_question(run.qa_id, run.scope_fingerprint)
        if not qa or qa["status"] not in QUESTION.active:
            raise OperatorRefused("OPERATOR_RUN_INACTIVE")
        binding = chat_execution.binding_for_key(self.context.settings, run.chat_binding)
        if not still_authorized(binding, run, trial=task.trial) or binding.entry != "operator":
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
        caller = task.authorize()
        if caller.scope_fingerprint != run.scope_fingerprint:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        binding = chat_execution.binding_for_key(self.context.settings, run.chat_binding)
        if not still_authorized(binding, run, trial=task.trial):
            raise OperatorRefused("OPERATOR_RESULT_RETRACTED")
        # task.authorize() 是对外调用：期间绑定/节点可能已变，身份与配置核验再来一遍。
        run, config = self._authenticate(run_id, node_token=node_token, manifest=manifest)
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
        expected = execution_manifest.from_text(run.execution_manifest)
        if tool is not None and (
            tool not in TOOL_NAMES or tool not in expected.tool_names or not allowed_in(tool, run)
        ):
            # 未声明的工具、没被勾选的工具、以及「这一类运行不该用的工具」都在这里挡住。
            raise OperatorRefused("OPERATOR_TOOL_DISABLED")
        latest = self.context.agent_run_store.get(run_id)
        if latest is None or latest.state not in {"issued", "running"} or expired(latest):
            raise OperatorRefused("OPERATOR_RUN_INACTIVE")
        return task, latest, config

    def _absorb_superseded(self, run_id, patch):
        """授权已死后送达的交付物：收下进终局快照、标记 superseded、永不投递。"""
        store = self.context.agent_run_store
        store.absorb(run_id, {"superseded": True, **patch})
        latest = store.get(run_id)
        return {
            "run_id": run_id,
            "state": latest.state if latest else "failed",
            "delivered": False,
        }

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

    def _note_refusal(self, task, tool, exc):
        """把一次**会回喂模型**的拒绝记到任务上，供答复复核区分「查无此单」与编造。

        只记 `FEEDBACK_REFUSALS` 里的码——其余拒绝终结运行，轮不到复核。
        去重并封顶：模型在同一拒绝上空转时记录不应跟着涨。
        """
        code = str(exc)
        if task is None or code not in FEEDBACK_REFUSALS:
            return
        refusal = {"tool": tool or "", "code": code, "message": FEEDBACK_REFUSALS[code]}
        with task.lock:
            if refusal not in task.refusals and len(task.refusals) < 16:
                task.refusals.append(refusal)

    def grant(self, run_id, token, manifest, tool, contract="", arguments=None):
        task = self.tasks.get(run_id)
        try:
            return self._grant(run_id, token, manifest, tool, contract, arguments)
        except OperatorRefused as exc:
            self._note_refusal(task, tool, exc)
            raise

    def _grant(self, run_id, token, manifest, tool, contract, arguments):
        if contract != (OBJECT_CONTRACT if _declared(tool).kind == "object" else TOOL_CONTRACT):
            # 旧节点不声明契约版本。挡在**领授权**这一步，而不是等它在完成回调里因为
            # 证据缺字段而失败——那既不是稳定错误码，也发生在公司令牌已经用过之后。
            raise OperatorRefused("OPERATOR_TOOL_CONTRACT_OUTDATED")
        task, run, _ = self.authorize_execution(run_id, node_token=token, manifest=manifest, tool=tool)
        # 参数放行发生在**取到公司令牌之前**：模型编的地址不会换来一次真实的对外请求。
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
            candidate = operator_agent.order_candidate(
                arguments,
                task.question,
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
            # 补齐或重复身份条件继续同一计划；真正更换站点/日期才重置。
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
            # 站点工具的范围是**账号可见的站点集合**，不是订单站点：它没有订单。
            "site_ids": self._grant_sites(task, run, tool),
            "inputs": tool_inputs(tool, values),
            "request": tool_request(tool, values),
            "contract": contract,
            "base_url": task.proxy_url + "/v1/internal/operator-runs/" + run_id + "/cpms",
            "bearer": task.grant,
            "expires_at": run.expires_at.isoformat(),
        }

    def _previous_plan(self, task):
        turn = task.conversation_turn
        if not turn:
            return {}
        store = self.context.runtime.conversation_store
        previous = store.previous_completed_qa(*turn)
        run = self.context.agent_run_store.by_qa(previous) if previous else None
        if not run or run.scope_fingerprint != task.caller.scope_fingerprint or not run.snapshot:
            return {}
        return run.snapshot.get("plan_arguments", {})

    def _station_plan(self, task, run):
        from aiops_diagnostics.operator import station_pricing, station_tariff

        arguments = task.arguments.get("cpms_station_plan", {})
        missing = [
            label
            for key, label in (
                ("name", "站点名称"),
                ("date", "日期YYYY-MM-DD"),
                ("start", "最早开始HH:mm"),
                ("end", "最晚结束HH:mm"),
                ("energy", "所需电量kWh"),
                ("power", "可持续功率kW"),
                ("timezone", "计费时区如Asia/Shanghai"),
                ("basis", "确认普通按量、不用优惠、不预约、不买保险、充完离场"),
            )
            if not arguments.get(key)
        ]
        data = {
            **{
                k: arguments.get(k, "")
                for k in ("name", "date", "start", "end", "energy", "power", "basis", "timezone")
            },
            "site_id": "",
            "text": "",
        }
        if missing:
            data["text"] = (
                "请补充：" + "、".join(missing) + "。这些条件决定连续充电费用，不能用额定功率代替。"
            )
            return data

        try:
            station_pricing.validate_conditions(arguments)
        except station_pricing.PlanUnavailable as exc:
            data["text"] = str(exc)
            return data

        def current():
            return self.authorize_execution(run.run_id, tool="cpms_station_plan")[0].caller.scope_fingerprint

        sites = query_sites(
            site_scope(task.caller),
            SiteKeywords(arguments["name"]),
            cpms_site_reader(task.base_url, task.bearer),
            current,
        )
        if len(sites["sites"]) != 1 or sites["limited"]:
            data["text"] = "没有唯一匹配的可见站点，请补充完整站点名称。"
            return data
        data["site_id"] = sites["sites"][0]["id"]
        try:
            rules = station_tariff.read_rules(
                task.caller,
                data["site_id"],
                arguments["date"],
                lambda path: cpms_get(task.base_url + path, task.bearer, task.caller.effective_tenant_id),
                current,
            )
        except OperatorRefused as exc:
            gaps = {
                "OPERATOR_PLAN_PENDING_RULE_CHANGE": "该站点已有待生效的费率变更，需核对生效规则后比较。",
                "OPERATOR_PLAN_SEASON_UNSUPPORTED": "季节时段配置不完整或跨年，需核对适用规则。",
                "OPERATOR_PLAN_SITE_UNSUPPORTED": "当前仅支持四轮充电站的方案比较。",
            }
            if str(exc) not in gaps:
                raise
            data["text"] = gaps[str(exc)]
            return data
        data["rules"] = rules
        try:
            data["text"] = station_pricing.plan_text(rules, arguments)
        except station_pricing.PlanUnavailable as exc:
            data["text"] = str(exc)
        return data

    def _charging_timeline(self, task, run):
        """SOP §2.2 时序过程遥测：设备与窗口**只**取自已交付的订单详情证据。

        tsdata 端点本身无鉴权、无租户隔离（公司内部门面），范围保证不来自请求形状，
        而来自查询构造：``deviceId`` 只可能是授权订单详情里的 ``childDeviceCode``/
        ``deviceCode``，窗口只可能是 ``createdTime``~``stopTime``。返回行上没有
        ``orderNo``/``siteId``/``tenantId`` 可逐行核验 —— 这正是它不走
        ``validate_body``、而像计算工具一样由网关合成证据的原因。
        ``txSerialNo`` 不在详情证据里，设备+窗口本身就是 SOP 的充分谓词。
        """
        base = self.context.settings.operator_tsdata_url.rstrip("/")
        if not base:
            raise OperatorRefused("TSDATA_UNAVAILABLE")
        detail = task.evidence.get(TOOL_NAMES[0], {}).get("data") or {}
        device = detail.get("childDeviceCode") or detail.get("deviceCode") or ""
        if not device:
            raise OperatorRefused("OPERATOR_EVIDENCE_MISSING")
        if not _TIMELINE_DEVICE.fullmatch(str(device)):
            raise OperatorRefused("CPMS_INVALID_RESPONSE")
        start = _timeline_time(detail.get("createdTime"))
        if start is None:
            raise OperatorRefused("OPERATOR_EVIDENCE_MISSING")
        raw_end = detail.get("stopTime")
        end = _timeline_time(raw_end)
        if raw_end and end is None:
            # 订单已停但停时不可读：窗口收不拢，不能拿「现在」冒充上界。
            raise OperatorRefused("OPERATOR_EVIDENCE_MISSING")
        if end is None:
            # 仍在充电的订单：上界取网关本地现在（与 CPMS/tsdata 同一时域）。
            end = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        data = tsdata_query(
            base + "/tdadmin/data/query",
            {
                "table": "charging-gun_property",
                "deviceId": device,
                "start": start,
                "end": end,
                "fields": list(TIMELINE_FIELDS),
                "orderBy": "_ts",
                "order": "ASC",
                "page": 1,
                "pageSize": TIMELINE_LIMIT,
            },
        )
        records = data.get("records") if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or type(data.get("count")) is not int
            or not isinstance(records, list)
            or len(records) > TIMELINE_LIMIT
            or data["count"] < len(records)
        ):
            raise OperatorRefused("TSDATA_UNAVAILABLE")
        shape_keys = _declared(TIMELINE_TOOL).evidence_shape["records"][0]
        rows = []
        for row in records:
            if not isinstance(row, dict):
                raise OperatorRefused("TSDATA_UNAVAILABLE")
            picked = {}
            for key in shape_keys:
                source_key = "_ts" if key == "ts" else key
                if source_key not in row:
                    continue
                value = row[source_key]
                if isinstance(value, (dict, list)) or (isinstance(value, str) and len(value) > 1000):
                    raise OperatorRefused("TSDATA_UNAVAILABLE")
                picked[key] = value
            rows.append(picked)
        total = data["count"]
        value = {
            "records": rows,
            "total": total,
            "sample_limit": TIMELINE_LIMIT,
            "truncated": total > len(rows),
            # `_ts` 升序是查询构造出来的，完整性等于「窗口内全部行都装下了」。
            "ordering_guaranteed": True,
            "complete_timeline": total <= len(rows),
            "samples_returned": len(rows),
            "absence": None if rows else "no_samples",
            "device": device,
            "window_start": start,
            "window_end": end,
        }
        response = {
            "code": 0,
            "data": {
                "orderNo": run.order_no,
                "siteId": task.order_site_id,
                "tenantId": task.caller.effective_tenant_id,
                "value": value,
            },
        }
        return value, response

    def _grant_sites(self, task, run, tool) -> list[str] | None:
        """这一次授权允许碰的站点。

        订单诊断：订单所在的那**一个**站点（保持既有收窄）。
        无订单（站点工具）：账号可见的站点集合；空集合是合法的（「你确实没有站点」），
        不放大成租户。
        """
        if "order_no" in _declared(tool).inputs:
            if not task.order_site_id:
                raise OperatorRefused("OPERATOR_ORDER_REQUIRED")
            return [task.order_site_id]
        scope = operator_scope(task.caller, allow_empty=True)
        return list(scope.site_ids) if scope.site_ids is not None else None

    def proxy(self, run_id, grant, path, query, method="GET"):
        task = self.tasks.get(run_id)
        started = time.monotonic()
        try:
            result = self._proxy(run_id, grant, path, query, method)
            run = self.context.agent_run_store.peek(run_id)
            tool = _proxy_tool(task, run, method, path, query)
            self._trace(
                run_id, "tool", "succeeded", tool=tool, duration_ms=(time.monotonic() - started) * 1000
            )
            self._summarize_tool(run_id, task, tool)
            return result
        except OperatorRefused as exc:
            if task is not None:
                run = self.context.agent_run_store.get(run_id)
                tool = _proxy_tool(task, run, method, path, query) if run else None
                self._note_refusal(task, tool, exc)
                if tool and secrets.compare_digest(task.grant, grant):
                    self._trace(
                        run_id,
                        "tool",
                        "refused",
                        tool=tool,
                        duration_ms=(time.monotonic() - started) * 1000,
                        detail=str(exc),
                    )
                    self._summarize_tool(run_id, task, tool, refusal=str(exc))
            raise

    def _proxy(self, run_id, grant, path, query, method):
        task = self.tasks.get(run_id)
        if task is None or not secrets.compare_digest(task.grant, grant):
            raise OperatorRefused("OPERATOR_GRANT_REJECTED")
        run = self.context.agent_run_store.get(run_id)
        if run is None:
            raise OperatorRefused("OPERATOR_RUN_INACTIVE")
        # 白名单是**完整请求目标**：方法、路径、查询串三者都对才算。拿工具名反推路径
        # 的做法已经去掉——那正是「加一条只读路径就要改 Java」的来源。
        tool = _proxy_tool(task, run, method, path, query)
        if tool is None:
            raise OperatorRefused("OPERATOR_PATH_REJECTED")
        with task.lock:
            self.authorize_execution(run_id, tool=tool)
            self._trace(run_id, "tool", tool=tool)
            if tool in task.evidence:
                if tool == communication.NAME:
                    communication.require_current(self, task, run)
                refs = task.evidence[tool].get("offline_references")
                if refs is not None:
                    from aiops_diagnostics.operator import offline_tool

                    try:
                        offline_tool.require_current(self, task, refs)
                    except SourceError as exc:
                        raise OperatorRefused("OPERATOR_SCOPE_CHANGED") from exc
                return task.evidence[tool]["response"]
            if (
                tool
                in (
                    TOOL_NAMES[1],
                    "cpms_order_tariff",
                    "cpms_order_billing",
                    TIMELINE_TOOL,
                    communication.NAME,
                )
                and TOOL_NAMES[0] not in task.delivered
            ):
                raise OperatorRefused("OPERATOR_DETAIL_REQUIRED")
            if tool == "cpms_order_billing" and "cpms_order_tariff" not in task.delivered:
                raise OperatorRefused("OPERATOR_TARIFF_REQUIRED")

            if tool in {"cpms_order_amount_check", "cpms_order_process_stats"}:
                if tool == "cpms_order_amount_check":
                    required = {"cpms_order_detail", "cpms_order_tariff", "cpms_order_billing"}
                else:
                    # 过程证据二选一即可：监测表有行走它，无行走时序（SOP §2.2）。
                    # 行内容是否真的非空由 analysis 再判——delivered 只证调过。
                    required = {"cpms_order_detail"}
                if not required <= task.delivered or (
                    tool == "cpms_order_process_stats"
                    and not ({"cpms_charging_samples", "cpms_charging_timeline"} & task.delivered)
                ):
                    raise OperatorRefused("OPERATOR_EVIDENCE_MISSING")
                self._authorized_order(task, run.order_no, expected_site=task.order_site_id)
                data = operator_agent.analysis(
                    task.evidence, "amount" if tool == "cpms_order_amount_check" else "process"
                )
                data = order_pricing.project(data, _declared(tool).evidence_shape)
                task.evidence[tool] = {
                    "tool": tool,
                    "inputs": tool_inputs(tool, run_values(tool, run.order_no, task.arguments[tool])),
                    "source": _declared(tool).source,
                    "observed_at": format_stored_time(now_utc()),
                    "data": data,
                    "response": {
                        "code": 0,
                        "data": {
                            "orderNo": run.order_no,
                            "siteId": task.order_site_id,
                            "tenantId": task.caller.effective_tenant_id,
                            "value": data,
                        },
                    },
                }
                return task.evidence[tool]["response"]
            from aiops_diagnostics.operator import offline_tool

            if tool == offline_tool.NAME:
                try:
                    data, refs = offline_tool.query(self, task, run)
                except (SourceError, ValueError) as exc:
                    raise OperatorRefused("OPERATOR_OFFLINE_UNAVAILABLE") from exc
                data = order_pricing.project(data, _declared(tool).evidence_shape)
                task.evidence[tool] = {
                    "tool": tool,
                    "inputs": tool_inputs(tool, task.arguments.get(tool, {})),
                    "source": _declared(tool).source,
                    "observed_at": format_stored_time(now_utc()),
                    "data": data,
                    "offline_references": refs,
                    "response": {
                        "code": 0,
                        "data": {"tenantId": task.caller.effective_tenant_id, "value": data},
                    },
                }
                return task.evidence[tool]["response"]
            if tool == "cpms_station_plan":
                data = self._station_plan(task, run)
                self.authorize_execution(run_id, tool=tool)
                data = order_pricing.project(data, _declared(tool).evidence_shape)
                task.evidence[tool] = {
                    "tool": tool,
                    "inputs": tool_inputs(tool, task.arguments.get(tool, {})),
                    "source": _declared(tool).source,
                    "observed_at": format_stored_time(now_utc()),
                    "data": data,
                    "response": {
                        "code": 0,
                        "data": {"tenantId": task.caller.effective_tenant_id, "value": data},
                    },
                }
                return task.evidence[tool]["response"]
            if tool in ("cpms_order_tariff", "cpms_order_billing"):
                # 快照本身不含siteId；在同次调用中重新读取订单，防止转站后的旧证据放行。
                detail = cpms_get(
                    task.base_url + tool_request(TOOL_NAMES[0], {"order_no": run.order_no})["path"],
                    task.bearer,
                    task.caller.effective_tenant_id,
                )
                validate_body(detail, task.caller, run.order_no, TOOL_NAMES[0])
                if detail["data"]["siteId"] != task.order_site_id:
                    raise OperatorRefused("CPMS_SCOPE_MISMATCH")
            if tool == SITE_TOOL:

                def reauthorize():
                    self.authorize_execution(run_id, tool=tool)
                    return task.caller.scope_fingerprint

                data, response = read_site_tool(
                    site_scope(task.caller),
                    tool,
                    task.arguments.get(tool) or {},
                    cpms_site_reader(task.base_url, task.bearer),
                    reauthorize,
                )
                self.authorize_execution(run_id, tool=tool)
                task.evidence[tool] = {
                    "tool": tool,
                    "inputs": tool_inputs(tool, run_values(tool, run.order_no, task.arguments.get(tool))),
                    "source": operator_tools.tool(tool).source,
                    "observed_at": format_stored_time(now_utc()),
                    "data": data,
                }
                task.evidence[tool]["response"] = response
                return task.evidence[tool]["response"]
            if tool == communication.NAME:
                data = communication.query(self, task, run, tsdata_query)
                task.evidence[tool] = {
                    "tool": tool,
                    "inputs": tool_inputs(tool, run_values(tool, run.order_no, task.arguments.get(tool))),
                    "source": _declared(tool).source,
                    "observed_at": format_stored_time(now_utc()),
                    "data": data,
                    "response": {
                        "code": 0,
                        "data": {
                            "orderNo": run.order_no,
                            "siteId": task.order_site_id,
                            "tenantId": task.caller.effective_tenant_id,
                            "value": data,
                        },
                    },
                }
                return task.evidence[tool]["response"]
            if tool == TIMELINE_TOOL:
                # 范围不经 `validate_body`（行上没有订单/租户字段）：设备与窗口
                # 由已交付的订单详情证据构造，详情未交付走上面的 DETAIL_REQUIRED。
                data, response = self._charging_timeline(task, run)
                self.authorize_execution(run_id, tool=tool)
                task.evidence[tool] = {
                    "tool": tool,
                    "inputs": tool_inputs(tool, run_values(tool, run.order_no, task.arguments.get(tool))),
                    "source": _declared(tool).source,
                    "observed_at": format_stored_time(now_utc()),
                    "data": data,
                    "response": response,
                }
                return task.evidence[tool]["response"]
            body = cpms_get(
                task.base_url + path + ("?" + query if query else ""),
                task.bearer,
                task.caller.effective_tenant_id,
            )
            if tool in ("cpms_order_tariff", "cpms_order_billing"):
                from aiops_diagnostics.operator.order_billing import read_billing

                data = (
                    order_pricing.read_snapshot(body, task.caller, run.order_no)
                    if tool == "cpms_order_tariff"
                    else read_billing(body, task.caller, run.order_no, task.order_site_id)
                )
                self.authorize_execution(run_id, tool=tool)
                task.evidence[tool] = {
                    "tool": tool,
                    "inputs": {"order_no": run.order_no},
                    "source": operator_tools.tool(tool).source,
                    "observed_at": format_stored_time(now_utc()),
                    "data": data,
                    "response": {
                        "code": 0,
                        "data": {
                            "orderNo": run.order_no,
                            "tenantId": task.caller.effective_tenant_id,
                            "siteId": task.order_site_id,
                            "value": data,
                        },
                    },
                }
                return task.evidence[tool]["response"]
            validate_body(body, task.caller, run.order_no, tool)
            rows = [body["data"]["chOrderInfoModel"]] if tool == TOOL_NAMES[0] else body["data"]["records"]
            if any(row["siteId"] != task.order_site_id for row in rows):
                raise OperatorRefused("CPMS_SCOPE_MISMATCH")
            self.authorize_execution(run_id, tool=tool)
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
        request = tool_request(tool, run_values(tool, run.order_no))
        return self.proxy(run_id, grant, request["path"], request["query"], request["method"])

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
            # 成功收尾后的迟到记账：证据链已闭合，只回执不落库。
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
                    raise  # 非致命拒绝（如 TOOL_DISABLED）：照旧 403。
                dead, run = True, latest
        if dead:
            # 死 run 交付的证据落终局快照——收下是取证，不投递是撤回的全部意义。
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
                # 死在写前一刻：收下进终局快照，不投递。
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

    def complete(self, run_id, token, manifest, payload):
        task = self.tasks.get(run_id)
        # 死 run 的 task 可能已被 _prune：上报不需要锁，活 run 的完成才串行化。
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
            # 节点失败照录；只有已取证后的模型失败可以交付证据，诊断仍标未完成。
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
            # 幂等重放：成功已交付，再报一遍只回执。
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
            # 授权已死的成功答复：收下、进终局快照、标记 superseded、不投递——
            # 撤回存在的意义就是不投递，收下的动作与投递的动作是两件事。
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
            result = operator_agent.render(answer, evidence, task.language) if answer is not None else None

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
                # verdict 只落码不拦截（ADR-0252）：allow/reject/uncertain 都是已评估，
                # 只有 unavailable 才是复核这一步自己没跑成。
                detail=review.get("reason") or ("allowed" if review["verdict"] == "allow" else None),
            )
        task.answer_review = review
        # 语义复核暂降为观察（ADR-0252）：verdict 仍写入快照、轨迹与行级明细，但不再
        # 拦截答复。只有模型根本没交出答复（model_failure）时才退回已授权证据。
        if model_failure is not None:
            from aiops_diagnostics.operator.operator_evidence import render_evidence

            result = render_evidence(evidence, task.language, secrets=(task.bearer, task.grant))
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
        """这次失败**是不是模型自己没交出可接受的答案**——重派安全且值得。

        2026-11-03 部署后实测改过一次：第一版要求「一次工具都没领过」，于是**已经取完证据
        才被拒**的 run 全都不会重派 —— 而那正是生产上最常见的一种（工具轮先跑完，
        模型最后一轮才吐错）。判据本身写错了方向。

        重派实际上很安全，因为：

        * **证据有缓存**：`grant` 里 `if tool in task.evidence: return …` —— 重派时
          工具返回同一份证据，**不会**再请求一次 CPMS，也不会改动已冻结的事实；
        * **参数已固定**：`task.arguments` 的 `setdefault` 让重派只能沿用同一组关键词，
          模型换关键词会被 `OPERATOR_TOOL_ARGUMENTS_CHANGED` 挡下；
        * **run 仍是 issued/running**：被推进过的 run 不会再走这条路。

        所以这里只需要排除「run 已经不在了/已经终局」这一种情况。
        """
        run = self.context.agent_run_store.get(run_id)
        return run is not None and run.state in {"issued", "running"}

    def _raise_node_failure(self, run_id):
        run = self.context.agent_run_store.get(run_id)
        if run is not None and run.state == "failed":
            reason = (run.reason or "").removeprefix("operator_node_failed: ")
            if reason in NODE_FAILURE_CODES:
                raise OperatorRefused(NODE_FAILURE_CODES[reason])
            if reason.startswith(TOOL_REFUSED_PREFIX):
                # 节点保留原始拒绝码上报：归因在 run.reason，对**用户**仍归一类——
                # 工具被它自己无法改变的拒绝打断，是依赖不可用，不是答复不合格。
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
            task.messages = self._messages(task)
            dispatcher.question = task.question
            # 已有取证固定在本run，最多一次重派，持续失败明确终止。
            while True:
                try:
                    self._trace(
                        run_id,
                        "dispatch",
                        detail=f"tools={manifest_tool_count}" if manifest_tool_count else None,
                    )
                    dispatcher.dispatch(config, run_id)
                except AgentRunDispatchError:
                    self._raise_node_failure(run_id)
                    # 节点已回报模型失败并完成证据交付时，运输层错误不能撤销该交付。
                    if self.context.agent_run_store.get(run_id).state == "succeeded":
                        break
                    # `_complete` 的终局闸拒（码未落账、run 仍活动）也不能重派：
                    # 同一 run 重派必撞同一道闸，直接归原码。
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
                # 派发本身成功、回调也走完了，但 run 没被推进 —— 说明 `_complete` 拒了答案。
                # 复核 verdict 不再拦截（ADR-0252）：闸拒在 task 上带原码，同一 run 重派必撞
                # 同一道闸，直接归码；其余「没交出可接受答复」的才重采样，上限后判不稳定。
                if task.completion_refusal:
                    raise OperatorRefused(task.completion_refusal)
                if task.resamples >= MAX_RESAMPLES:
                    raise OperatorRefused("OPERATOR_MODEL_OUTPUT_UNSTABLE")
                task.resamples += 1
                self._trace(run_id, "resample", detail="no_answer_accepted")
                logging.getLogger("aiops.operator").warning(
                    "operator_resample attempt=%d reason=no_answer_accepted", task.resamples
                )
            task = self.tasks[run_id]
            current = task.authorize()
            result = self.authorize_readback(
                current, qa_id, task.bearer, candidate_authorize=task.authorize if task.candidate else None
            )
            if store.update_assistant_question(qa_id, status="completed", result=result):
                self._trace(run_id, "completed", "succeeded")
                self.context.runtime._complete_conversation_turn(
                    task.conversation_turn,
                    task.question,
                    result,
                    status_qa_id=qa_id,
                    active_order_no=self.context.agent_run_store.get(run_id).order_no or None,
                )
        except Exception as exc:  # noqa: BLE001 - 后台失败必须收敛，凭据及上游正文不进日志。
            failure_code = _failure_code(exc)
            self._trace(run_id, "failed", "failed", detail=failure_code)
            reason = _failure_reason(exc)
            if not self.context.agent_run_store.fail(run_id, failure_code):
                self.context.agent_run_store.refuse(run_id, failure_code)
            store.update_assistant_question(qa_id, status="failed", error_code=_failure_code(exc))
            # 失败**原因**单独记一行，和上面按码写的日志分开：这条是给值班的人看的，
            # 而 `OPERATOR_DIAGNOSIS_FAILED` 一个码覆盖了「模型不吐 JSON」「上游没钱」
            # 「答案不合格」等完全不同的成因。2026-11-03 实测：额度耗尽时用户看到的
            # 与「模型写错 JSON」一模一样，于是排查方向被引到判据和提示词上。
            logging.getLogger("aiops.operator").warning(
                "operator_run_failed reason=%s code=%s", reason, _failure_code(exc)
            )
            task = self.tasks.get(run_id)
            if task is not None:
                self.context.runtime._release_conversation_turn(task.conversation_turn)
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

    def retract(self, route):
        with self.lock:
            tasks = list(self.tasks.items())
        for run_id, task in tasks:
            run = self.context.agent_run_store.get(run_id)
            if run and run.chat_binding == route.key and run.route_revision <= route.revision:
                self.context.runtime.cancel_assistant_qa(task.caller, run.qa_id)
