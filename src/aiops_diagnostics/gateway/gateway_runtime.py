"""Gateway 业务任务线程池、在线取消登记与执行器统一调度容器。"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from aiops_diagnostics.common.config import Settings
from aiops_diagnostics.common.i18n import DEFAULT_LANGUAGE
from aiops_diagnostics.common.model_request import resolve_provider_api_key
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.consumer import consumer_chat_service, consumer_intent
from aiops_diagnostics.consumer.consumer_photo import PhotoStore
from aiops_diagnostics.conversation.conversation_store import (
    BUSY_LOCK_SECONDS,
    ConversationError,
    ConversationStore,
    TurnCompletionMetadata,
    reject_mixed_completion_kwargs,
)
from aiops_diagnostics.data import offline_source
from aiops_diagnostics.data import sources as data_sources
from aiops_diagnostics.data.sources import (
    scoped_live_sources,  # noqa: F401 - 由领域 runner 经 runtime_mod 读取，保留测试 monkeypatch 入口
)
from aiops_diagnostics.dify.dify_app_registry import DifyAppRegistry
from aiops_diagnostics.dify.dify_run_store import DifyRunStore
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaces
from aiops_diagnostics.execution import chat_data_grant, chat_execution
from aiops_diagnostics.execution.agent_runs import AgentRunStore
from aiops_diagnostics.gateway import chat_job_runner, gateway_schema, trial_job_runner
from aiops_diagnostics.gateway.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway.gateway_store import ACTIVE_DIAGNOSIS_STATUSES, GatewayStore
from aiops_diagnostics.gateway.metrics_store import MetricsStore
from aiops_diagnostics.health import health_job_runner
from aiops_diagnostics.health.health_report import HEALTH_RULE_VERSION
from aiops_diagnostics.identity.query_scope import resolve_query_scope
from aiops_diagnostics.identity.scope_context import ScopeContext
from aiops_diagnostics.knowledge import media_service
from aiops_diagnostics.knowledge.knowledge_dataset_sync_guard import KnowledgeDatasetSyncGuard
from aiops_diagnostics.knowledge.knowledge_retrieval import (
    KbServiceClient,
    KnowledgeSearchClient,
    MediaProxy,
    MediaResourceSigner,
    MediaResponse,
)
from aiops_diagnostics.knowledge.tenant_scope import namespace_for
from aiops_diagnostics.operator import ops_analysis_runner
from aiops_diagnostics.operator.ops_analysis import (
    OPERATIONS_CARD_FAILURE_RATE,
    OPERATIONS_CARDS,
)
from aiops_diagnostics.operator.ops_analysis import (
    RULE_VERSION as OPS_ANALYSIS_RULE_VERSION,
)
from aiops_diagnostics.publication.agent_lifecycle import VIEW_ROLES, AgentStore

_LOGGER = logging.getLogger("aiops.gateway_runtime")


@runtime_checkable
class OperatorSiteScopeResolver(Protocol):
    """运营商站点范围解析器契约（由装配层注入，避免 gateway_runtime 反向依赖 gateway_api）。"""

    def site_scope_by_b_user_id(self, b_user_id: str, tenant_id: str) -> Any: ...


@runtime_checkable
class OrderAuthorizer(Protocol):
    """本人订单归属授权器契约（API 层与聊天 worker 共用同一判据）。"""

    def can_access(self, context: ScopeContext, order_no: str) -> bool: ...


def package_version() -> str:
    """部署版本串。延迟导入：`__init__` 在部署时注入，模块级导入会把它冻住。"""
    from aiops_diagnostics import __version__

    return __version__


@dataclass
class JobRegistration:
    """保存当前 Dify/节点任务的取消回调与会话轮次。"""

    conversation_turn: tuple[str, str, int] | None = None
    interrupt: Callable[[], None] | None = None
    route_type: str = "qa"
    #: 由停止请求设置；覆盖 worker 在停止之后才开始登记句柄的交错窗口。
    cancelled: bool = False
    #: 原地重生成失败/取消时为 True，释放槽位但不删已有已完成答复。
    preserve_completed: bool = False
    #: 每次生成认领的唯一持有标识，防旧重生成任务延迟收尾误解新认领锁。
    claim_id: str | None = None


class GatewayRuntime:
    """现役业务任务调度池、取消登记与共享 Store 容器。

    各领域执行体分别下沉在：
    - `gateway/chat_job_runner.py`（聊天 Dify/节点执行与撤回收敛）
    - `gateway/trial_job_runner.py`（草稿试运行与并发闸）
    - `health/health_job_runner.py`（充电健康报告）
    - `operator/ops_analysis_runner.py`（运维分析四卡聚合）
    - `knowledge/media_service.py`（签名媒体代理）
    """

    def __init__(
        self,
        store: GatewayStore,
        gateway_settings: GatewayServerSettings,
        diagnostic_settings: Settings,
        *,
        kb_search_client: KnowledgeSearchClient | None = None,
        media_signer: MediaResourceSigner | None = None,
        agent_store: Any = None,
        agent_run_store: Any = None,
        dify_workspaces: Any = None,
        knowledge_dataset_sync_guard: Any = None,
        trial_operator_scope: OperatorSiteScopeResolver | None = None,
        order_authorizer: OrderAuthorizer | None = None,
    ) -> None:
        gateway_schema.startup(store.path)
        self.store = store
        self.gateway_settings = gateway_settings
        self.diagnostic_settings = diagnostic_settings
        self.kb_search_client = kb_search_client
        self.media_signer = media_signer
        self.agent_store = agent_store
        self.dify_workspaces = dify_workspaces
        self.knowledge_dataset_sync_guard = knowledge_dataset_sync_guard
        self.agent_run_store = agent_run_store
        self.dify_run_store = DifyRunStore(store.path)
        self._media_proxy: MediaProxy | None = None
        self.conversation_store = ConversationStore(store.path)
        self._trial_lock = threading.Lock()
        self._trial_jobs: set[str] = set()
        self._trial_contexts: dict[str, tuple[Any, Any, str, str]] = {}
        self._trial_max_concurrent = 1
        #: 运营商站点范围解析器：未注入时试运行落空范围（fail closed）。
        self._trial_operator_scope: OperatorSiteScopeResolver | None = trial_operator_scope
        #: 订单授权器：未注入时聊天 worker 判不出归属直接落 failed（fail closed）。
        self._order_authorizer: OrderAuthorizer | None = order_authorizer
        self.consumer_photos = PhotoStore()
        self.dify_app_registry = DifyAppRegistry(store.path)
        self.metrics_store = MetricsStore(store.path)
        self._executor = ThreadPoolExecutor(
            max_workers=gateway_settings.max_workers,
            thread_name_prefix="aiops-gateway-run",
        )
        self._futures: dict[str, Future[None]] = {}
        self._qa_registrations: dict[str, JobRegistration] = {}

    @classmethod
    def from_settings(
        cls,
        store: GatewayStore,
        gateway_settings: GatewayServerSettings,
        *,
        trial_operator_scope: OperatorSiteScopeResolver | None = None,
        order_authorizer: OrderAuthorizer | None = None,
    ) -> GatewayRuntime:
        diagnostic_settings = gateway_settings.load_data_sources()
        diagnostic_settings.agent.validate()
        diagnostic_settings.ssh.validate()
        kb_client = None
        if gateway_settings.kb_service_base_url:
            kb_client = KbServiceClient(
                gateway_settings.kb_service_base_url,
                tenant_id="aiops",
                environment=namespace_for(gateway_settings),
                timeout=gateway_settings.kb_service_timeout_seconds,
            )
        media_signer = None
        if gateway_settings.media_signing_secret:
            media_signer = MediaResourceSigner(
                gateway_settings.media_signing_secret,
                ttl_seconds=gateway_settings.media_ttl_seconds,
            )
        return cls(
            store,
            gateway_settings,
            diagnostic_settings,
            kb_search_client=kb_client,
            media_signer=media_signer,
            agent_store=AgentStore(store.path),
            agent_run_store=AgentRunStore(store.path),
            dify_workspaces=(
                DifyWorkspaces(store.path) if gateway_settings.dify_external_knowledge_api_id else None
            ),
            knowledge_dataset_sync_guard=(
                KnowledgeDatasetSyncGuard() if gateway_settings.dify_external_knowledge_api_id else None
            ),
            trial_operator_scope=trial_operator_scope,
            order_authorizer=order_authorizer,
        )

    def shutdown(self) -> None:
        self.consumer_photos.close()
        self._executor.shutdown(wait=False, cancel_futures=False)

    def set_trial_operator_scope(self, scope: OperatorSiteScopeResolver | None) -> None:
        """注入运营商站点范围解析器；为 None 时试运行落到空站点集合（fail closed）。"""
        self._trial_operator_scope = scope

    def set_order_authorizer(self, authorizer: OrderAuthorizer | None) -> None:
        """注入本人订单授权器，使聊天 worker 写终态前与 API 层共用同一判据。"""
        self._order_authorizer = authorizer

    # ------------------------------------------------------------------
    # 健康报告与运维分析作业
    # ------------------------------------------------------------------

    def start_health_report(
        self,
        context: ScopeContext,
        order_no: str,
        *,
        language: str = DEFAULT_LANGUAGE,
    ) -> dict[str, Any]:
        job, created = self.store.create_or_reuse_health_job(
            context.scope_fingerprint,
            order_no,
            HEALTH_RULE_VERSION,
            language=language,
            tenant_id=context.effective_tenant_id,
        )
        if not created:
            return job
        future = self._executor.submit(self._execute_health_report, job["job_id"], context, order_no)
        self._futures[job["job_id"]] = future
        future.add_done_callback(lambda _: self._futures.pop(job["job_id"], None))
        return job

    def get_health_report(self, context: ScopeContext, job_id: str) -> dict[str, Any] | None:
        return self.store.get_health_job(job_id, context.scope_fingerprint)

    def _finish_health_job(self, job_id: str, **fields: Any) -> bool:
        return health_job_runner.finish_health_job(self, job_id, **fields)

    def _execute_health_report(
        self,
        job_id: str,
        context: ScopeContext,
        order_no: str,
    ) -> None:
        health_job_runner.execute_health_report(
            self,
            job_id,
            context,
            order_no,
            runtime_mod=sys.modules[__name__],
        )

    def start_operations_analysis(
        self,
        context: ScopeContext,
        *,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
        language: str = DEFAULT_LANGUAGE,
    ) -> dict[str, Any]:
        if site_ids is not None and not site_ids:
            raise ValueError("analysis requires at least one site")
        job = self.store.create_analysis_job(
            context.scope_fingerprint,
            site_ids=site_ids,
            window_start=window_start,
            window_end=window_end,
            rule_version=OPS_ANALYSIS_RULE_VERSION,
            language=language,
            card=OPERATIONS_CARD_FAILURE_RATE,
            tenant_id=context.effective_tenant_id,
        )
        future = self._executor.submit(
            self._execute_operations_analysis,
            job["diagnosis_id"],
            context,
            site_ids,
            window_start,
            window_end,
            language,
        )
        self._futures[job["diagnosis_id"]] = future
        future.add_done_callback(lambda _: self._futures.pop(job["diagnosis_id"], None))
        return job

    def get_operations_analysis(self, context: ScopeContext, analysis_id: str) -> dict[str, Any] | None:
        job = self.store.get_standard_diagnosis(analysis_id, context.scope_fingerprint)
        if job and (job.get("result") or {}).get("_offline_references"):
            with data_sources.scoped_mysql_source(
                self.diagnostic_settings, resolve_query_scope(context)
            ) as source:
                offline_source.require_references(source, job["result"]["_offline_references"])
            job["result"] = {k: v for k, v in job["result"].items() if k != "_offline_references"}
        return job

    def start_operations_card(
        self,
        context: ScopeContext,
        *,
        card: str,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
        language: str = DEFAULT_LANGUAGE,
    ) -> dict[str, Any]:
        if site_ids is not None and not site_ids:
            raise ValueError("analysis requires at least one site")
        if card not in OPERATIONS_CARDS:
            raise ValueError("unknown operations card")
        job = self.store.create_analysis_job(
            context.scope_fingerprint,
            site_ids=site_ids,
            window_start=window_start,
            window_end=window_end,
            rule_version=OPS_ANALYSIS_RULE_VERSION,
            language=language,
            card=card,
            tenant_id=context.effective_tenant_id,
        )
        future = self._executor.submit(
            self._execute_operations_card,
            job["diagnosis_id"],
            context,
            card,
            site_ids,
            window_start,
            window_end,
            language,
        )
        self._futures[job["diagnosis_id"]] = future
        future.add_done_callback(lambda _: self._futures.pop(job["diagnosis_id"], None))
        return job

    def _end_analysis_job(
        self,
        analysis_id: str,
        tenant_id: str,
        *,
        result: dict[str, Any] | None,
        started_ms: float,
        error_code: str | None = None,
        error_message: str = "",
    ) -> None:
        ops_analysis_runner.end_analysis_job(
            self,
            analysis_id,
            tenant_id,
            result=result,
            started_ms=started_ms,
            error_code=error_code,
            error_message=error_message,
        )

    def _narrative_for(self, card: str, aggregate: Any, window: dict[str, Any]) -> Any:
        return ops_analysis_runner.narrative_for(
            self,
            card,
            aggregate,
            window,
            runtime_mod=sys.modules[__name__],
        )

    def _compute_card(
        self,
        card: str,
        sources: Any,
        effective_sites: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
        language: str,
        site_ids: tuple[str, ...] | None,
    ) -> dict[str, Any]:
        return ops_analysis_runner.compute_card(
            self,
            card,
            sources,
            effective_sites,
            window_start,
            window_end,
            language,
            site_ids,
            runtime_mod=sys.modules[__name__],
        )

    def _execute_operations_card(
        self,
        analysis_id: str,
        context: ScopeContext,
        card: str,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
        language: str,
    ) -> None:
        ops_analysis_runner.execute_operations_card(
            self,
            analysis_id,
            context,
            card,
            site_ids,
            window_start,
            window_end,
            language,
            runtime_mod=sys.modules[__name__],
        )

    def _execute_operations_analysis(
        self,
        analysis_id: str,
        context: ScopeContext,
        site_ids: tuple[str, ...] | None,
        window_start: str,
        window_end: str,
        language: str,
    ) -> None:
        ops_analysis_runner.execute_operations_analysis(
            self,
            analysis_id,
            context,
            site_ids,
            window_start,
            window_end,
            language,
            runtime_mod=sys.modules[__name__],
        )

    # ------------------------------------------------------------------
    # 试运行（委托 trial_job_runner）
    # ------------------------------------------------------------------

    def start_trial(
        self,
        initiator: str,
        order_no: str,
        draft: Any,
        language: str,
        trial_initiator: Any = None,
        run_context: ScopeContext | None = None,
    ) -> dict[str, Any] | None:
        return trial_job_runner.start_trial(
            self,
            initiator,
            order_no,
            draft,
            language,
            trial_initiator=trial_initiator,
            run_context=run_context,
        )

    def _release_trial(self, trial_id: str) -> None:
        trial_job_runner.release_trial(self, trial_id)

    def cancel_trial(self, context: ScopeContext, trial_id: str):
        return trial_job_runner.cancel_trial(self, context, trial_id)

    def trial_run_context(self) -> ScopeContext:
        return trial_job_runner.trial_run_context(self)

    def trial_context_for(self, initiator: Any) -> ScopeContext:
        return trial_job_runner.trial_context_for(self, initiator)

    def get_trial(self, context: ScopeContext, trial_id: str) -> dict[str, Any] | None:
        return self.store.get_standard_diagnosis(trial_id, context.scope_fingerprint)

    def _execute_trial(self, trial_id: str) -> None:
        trial_job_runner.execute_trial(self, trial_id)

    def _record_trial_metric(self, context: ScopeContext, outcome: str, started_ms: float) -> None:
        trial_job_runner.record_trial_metric(self, context, outcome, started_ms)

    # ------------------------------------------------------------------
    # 指标记录与查询
    # ------------------------------------------------------------------

    def _record_metric(self, **fields: Any) -> None:
        """Best-effort redacted metric row; never fails the run."""
        with contextlib.suppress(Exception):
            self.metrics_store.record(**fields)

    def record_route_metric(
        self,
        context: ScopeContext,
        route_type: str,
        outcome: str,
        *,
        agent_id: str | None = None,
        agent_version_key: str | None = None,
        conversation_id: str | None = None,
        retrieval_status: str = "",
        searches: int = 0,
        media_count: int = 0,
        duration_ms: int | None = None,
        token_count: int = 0,
        error_code: str | None = None,
    ) -> None:
        tenant_id = getattr(context, "effective_tenant_id", "") or ""
        if not tenant_id:
            return
        self._record_metric(
            tenant_id=tenant_id,
            route_type=route_type,
            outcome=outcome,
            agent_id=agent_id,
            agent_version_key=agent_version_key,
            conversation_id=conversation_id,
            retrieval_status=retrieval_status,
            searches=searches,
            media_count=media_count,
            duration_ms=duration_ms,
            token_count=token_count,
            error_code=error_code,
        )

    def get_agent_metrics_summary(
        self,
        context: ScopeContext,
        *,
        agent_id: str | None = None,
        window_hours: int = 720,
    ) -> dict[str, Any]:
        effective_roles = frozenset(getattr(context, "roles", ()))
        if not effective_roles.intersection(VIEW_ROLES):
            raise RuntimeError("agent access is not permitted")
        tenant_id = getattr(context, "effective_tenant_id", "") or ""
        summary = self.metrics_store.summary(
            tenant_id,
            agent_id=agent_id,
            limit_hours=window_hours,
        )
        totals = dict(summary.get("totals") or {})
        if agent_id is None:
            cutoff = format_stored_time(now_utc() - timedelta(hours=window_hours))
            expired = self.store.count_expired_jobs_by_tenant(since=cutoff)
            totals["expired"] = expired.get(tenant_id, 0)
        summary["totals"] = totals
        return summary

    def list_agent_metrics_runs(
        self,
        context: ScopeContext,
        *,
        agent_id: str | None = None,
        route_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        effective_roles = frozenset(getattr(context, "roles", ()))
        if not effective_roles.intersection(VIEW_ROLES):
            raise RuntimeError("agent access is not permitted")
        return self.metrics_store.list_runs(
            getattr(context, "effective_tenant_id", "") or "",
            agent_id=agent_id,
            route_type=route_type,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # 聊天 / 问答派发、取消与会话轮次收敛
    # ------------------------------------------------------------------

    def start_consumer_chatflow(
        self,
        context,
        question,
        *,
        binding,
        conversation,
        conversation_turn_no,
        language,
        order_no="",
        data_kind="status",
        photo_id="",
        uses_context=True,
        expected_previous_qa_id=None,
    ):
        return consumer_chat_service.start(
            self,
            context,
            question,
            binding=binding,
            conversation=conversation,
            order_no=order_no,
            data_kind=data_kind,
            photo_id=photo_id,
            uses_context=uses_context,
            expected_previous_qa_id=expected_previous_qa_id,
            registration=JobRegistration(
                conversation_turn=(
                    conversation["conversation_id"],
                    context.scope_fingerprint,
                    conversation_turn_no,
                )
            ),
            language=language,
        )

    def start_operator_cpms(
        self,
        service,
        caller,
        binding,
        order,
        question,
        *,
        bearer,
        authorize,
        base,
        proxy,
        conversation_turn,
        language="zh",
        trial_manifest=None,
        agent_id="operator_main",
        attachments=None,
        attempt_no=1,
        parent_qa_id=None,
        claim_id=None,
    ):
        return service.start(
            caller,
            binding,
            order,
            question,
            bearer=bearer,
            authorize=authorize,
            base=base,
            proxy=proxy,
            registration=JobRegistration(
                conversation_turn=conversation_turn,
                route_type="qa",
                preserve_completed=int(attempt_no) > 1,
                claim_id=claim_id,
            ),
            language=language,
            trial_manifest=trial_manifest,
            agent_id=agent_id,
            attachments=attachments,
            attempt_no=attempt_no,
            parent_qa_id=parent_qa_id,
        )

    def start_chat_order_qa(
        self,
        context: ScopeContext,
        question: str,
        *,
        order_no: str,
        binding: Any,
        language: str = DEFAULT_LANGUAGE,
        conversation: dict[str, Any] | None = None,
        conversation_turn_no: int | None = None,
    ) -> dict[str, Any]:
        qa = self.store.create_assistant_question(
            context.scope_fingerprint,
            question,
            internal_run_id=(
                chat_execution.DIFY_RUN_PREFIX
                if getattr(binding, "engine", "node") == "dify"
                else chat_execution.CHAT_RUN_PREFIX
            )
            + order_no,
            tenant_id=context.effective_tenant_id,
        )
        registration = JobRegistration(
            conversation_turn=(
                (conversation["conversation_id"], conversation["scope_fingerprint"], conversation_turn_no)
                if conversation is not None and conversation_turn_no is not None
                else None
            ),
            route_type="qa",
        )
        self._qa_registrations[qa["qa_id"]] = registration
        worker = (
            self._execute_chat_dify_qa
            if getattr(binding, "engine", "node") == "dify"
            else self._execute_chat_order_qa
        )
        future = self._executor.submit(
            worker,
            qa["qa_id"],
            order_no,
            context,
            language,
            registration,
            question,
            binding,
        )
        self._futures[qa["qa_id"]] = future
        future.add_done_callback(lambda _: self._futures.pop(qa["qa_id"], None))
        future.add_done_callback(lambda _: self._qa_registrations.pop(qa["qa_id"], None))
        return qa

    def _execute_chat_dify_qa(
        self,
        qa_id: str,
        order_no: str,
        context: ScopeContext,
        language: str,
        registration: JobRegistration,
        question: str,
        binding: Any,
    ) -> None:
        chat_job_runner.execute_chat_dify_qa(
            self, qa_id, order_no, context, language, registration, question, binding
        )

    def _settle_dify_failed(
        self, dify_store, grant_store, run_id, qa_id: str, code: str, detail: str | None = None
    ) -> None:
        chat_job_runner.settle_dify_failed(self, dify_store, grant_store, run_id, qa_id, code, detail=detail)

    def settle_retracted_runs(self, *, route: Any) -> dict[str, Any]:
        return chat_job_runner.settle_retracted_runs(self, route=route)

    def _settle_dify_cancelled(self, dify_store, grant_store, run_id, qa_id: str) -> None:
        chat_job_runner.settle_dify_cancelled(self, dify_store, grant_store, run_id, qa_id)

    def _release_dify_answer(
        self,
        *,
        run_id: str,
        grant_id: str,
        binding: Any,
        context: ScopeContext,
        order_no: str,
        language: str,
        outputs: dict[str, Any],
    ) -> dict[str, Any]:
        return chat_job_runner.release_dify_answer(
            self,
            run_id=run_id,
            grant_id=grant_id,
            binding=binding,
            context=context,
            order_no=order_no,
            language=language,
            outputs=outputs,
        )

    def _require_dify_binding_current(self, binding: Any) -> None:
        chat_job_runner.require_dify_binding_current(self, binding)

    @staticmethod
    def _chat_unavailable(code: str, message: str):
        return chat_execution.ChatRunUnavailable(code, message)

    def _require_chat_order_visible(self, context: ScopeContext, order_no: str) -> None:
        chat_job_runner.require_chat_order_visible(self, context, order_no)

    def _execute_chat_order_qa(
        self,
        qa_id: str,
        order_no: str,
        context: ScopeContext,
        language: str,
        registration: JobRegistration,
        question: str,
        binding: Any,
    ) -> None:
        chat_job_runner.execute_chat_order_qa(
            self,
            qa_id,
            order_no,
            context,
            language,
            registration,
            question,
            binding,
            gateway_version=package_version(),
        )

    def classify_consumer(
        self, question, *, language, previous_question="", previous_order=False, selected_order=False
    ):
        settings = Settings.from_config(self.gateway_settings.server_config_file)
        provider = settings.agent.select_provider(None)
        return consumer_intent.classify(
            question,
            provider=provider,
            api_key=resolve_provider_api_key(settings.agent, provider=provider),
            language=language,
            previous_question=previous_question,
            previous_order=previous_order,
            selected_order=selected_order,
        )

    def get_assistant_qa(
        self,
        context: ScopeContext,
        qa_id: str,
    ) -> dict[str, Any] | None:
        return self.store.get_assistant_question(qa_id, context.scope_fingerprint)

    def list_assistant_qa(
        self,
        context: ScopeContext,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self.store.list_assistant_questions(context.scope_fingerprint, limit=limit)

    def cancel_assistant_qa(
        self,
        context: ScopeContext,
        qa_id: str,
    ) -> dict[str, Any] | None:
        qa = self.store.get_assistant_question(qa_id, context.scope_fingerprint)
        if qa is None:
            return None
        if qa["status"] in ACTIVE_DIAGNOSIS_STATUSES:
            accepted = self.store.update_assistant_question(qa_id, status="cancelled")
            if accepted and self.agent_run_store is not None:
                linked = self.agent_run_store.by_qa(qa_id)
                if linked is not None:
                    with contextlib.suppress(Exception):
                        self.agent_run_store.revoke(linked.run_id, "cancelled_by_caller")
            if accepted and self.dify_run_store is not None:
                linked_dify = self.dify_run_store.by_qa(qa_id)
                if linked_dify is not None:
                    with contextlib.suppress(Exception):
                        self.dify_run_store.cancel(linked_dify.run_id, "cancelled_by_caller")
                with contextlib.suppress(Exception):
                    chat_data_grant.DataGrantStore(self.store.path).revoke_for_qa(qa_id)
            registration = self._qa_registrations.pop(qa_id, None)
            if registration is not None:
                registration.cancelled = True
                self._interrupt_registered_turn(registration)
                if accepted:
                    self._release_conversation_turn(
                        registration.conversation_turn,
                        preserve_completed=registration.preserve_completed,
                        claim_id=registration.claim_id,
                    )
            if accepted:
                self._record_cancelled_job_metric(context, registration)
            return self.store.get_assistant_question(qa_id, context.scope_fingerprint)
        return qa

    def _record_cancelled_job_metric(
        self,
        context: ScopeContext,
        registration: JobRegistration | None,
    ) -> None:
        conversation_id = (
            registration.conversation_turn[0]
            if registration is not None and registration.conversation_turn is not None
            else None
        )
        self.record_route_metric(
            context,
            registration.route_type if registration is not None else "qa",
            "cancelled",
            conversation_id=conversation_id,
        )

    def _interrupt_registered_turn(self, registration: JobRegistration) -> None:
        interrupt = registration.interrupt
        if interrupt is None:
            return
        try:
            interrupt()
        except Exception as exc:
            _LOGGER.warning("assistant question interrupt failed: %s", exc)

    def _release_conversation_turn(
        self,
        conversation_turn: tuple[str, str, int] | None,
        *,
        preserve_completed: bool = False,
        claim_id: str | None = None,
    ) -> None:
        if conversation_turn is None:
            return
        conversation_id, scope_fingerprint, turn_no = conversation_turn
        # 终态发布（failed/cancelled 落库）必须先调用本函数再发布：
        # 轮询到终态的客户端会立刻续问，槽位未释放就会被 CONVERSATION_BUSY 误拒。
        # 尽力而为的释放绝不向调用方抛出：它常排在终态发布之前，
        # 释放失败顶多让槽位等到过期扫尾，不能反过来吞掉终态。
        try:
            self.conversation_store.release_turn(
                conversation_id,
                scope_fingerprint,
                turn_no,
                preserve_completed=preserve_completed,
                claim_id=claim_id,
            )
        except ConversationError:
            pass
        except Exception:  # noqa: BLE001
            _LOGGER.warning("release conversation turn failed", exc_info=True)

    def _complete_conversation_turn(
        self,
        conversation_turn: tuple[str, str, int] | None,
        question: str,
        answer: dict[str, Any] | None,
        *,
        cancelled: bool = False,
        guarded: bool = False,
        stop_renewer: threading.Event | None = None,
        metadata: TurnCompletionMetadata | None = None,
        status_qa_id: str | None = None,
        active_order_no: str | None = None,
        attempt_no: int | None = None,
        parent_qa_id: str | None = None,
        attachment_ids: list[str] | None = None,
        citations: list[dict[str, Any]] | None = None,
        steps: list[dict[str, Any]] | None = None,
        elapsed_ms: int | None = None,
        delivery_mode: str | None = None,
        preserve_completed: bool = False,
        claim_id: str | None = None,
    ) -> None:
        reject_mixed_completion_kwargs(
            metadata,
            preserve_completed=preserve_completed,
            legacy_values=(
                status_qa_id,
                active_order_no,
                attempt_no,
                parent_qa_id,
                attachment_ids,
                citations,
                steps,
                elapsed_ms,
                delivery_mode,
                claim_id,
            ),
        )
        if guarded:
            cancelled = True
        if stop_renewer is not None:
            stop_renewer.set()
        if conversation_turn is None:
            return
        conversation_id, scope_fingerprint, turn_no = conversation_turn
        estimated_tokens = _estimate_turn_tokens(question, answer)
        resolved_meta = metadata or TurnCompletionMetadata(
            token_count=estimated_tokens,
            status_qa_id=status_qa_id,
            active_order_no=active_order_no,
            attempt_no=attempt_no,
            parent_qa_id=parent_qa_id,
            attachment_ids=attachment_ids,
            citations=citations,
            steps=steps,
            elapsed_ms=elapsed_ms,
            delivery_mode=delivery_mode,
            preserve_completed=preserve_completed,
            claim_id=claim_id,
        )
        if resolved_meta.token_count == 0 and estimated_tokens:
            resolved_meta = replace(resolved_meta, token_count=estimated_tokens)
        with contextlib.suppress(ConversationError):
            self.conversation_store.complete_turn(
                conversation_id,
                scope_fingerprint,
                turn_no,
                answer=answer,
                cancelled=cancelled or answer is None,
                metadata=resolved_meta,
            )

    def _registered_agent(self, tenant_id: str, business_entry: str) -> tuple[str | None, bool]:
        try:
            registry = self.dify_app_registry
            if not registry.is_configured():
                return None, False
            binding = registry.lookup(tenant_id, business_entry or "")
        except Exception:  # noqa: BLE001 - unreadable registry keeps the old rule
            return None, False
        return (binding.agent_name if binding is not None else None), True

    def serve_media(
        self,
        signed_id: str,
        *,
        range_header: str | None = None,
    ) -> MediaResponse | None:
        return media_service.serve_media(self, signed_id, range_header=range_header)


RENEW_TURN_CLAIM_SECONDS = BUSY_LOCK_SECONDS / 3


def _estimate_turn_tokens(question: str, answer: dict[str, Any] | None) -> int:
    text = question or ""
    if answer:
        for block in answer.get("blocks") or []:
            text += str(block.get("text") or "")
        text += str(answer.get("text") or "")
        for field in ("summary", "root_cause"):
            text += str(answer.get(field) or "")
    return max(1, len(text) * 2 // 3)


_intersect_sites = ops_analysis_runner.intersect_sites
_db_window_bound = ops_analysis_runner.db_window_bound
_internal_error_message = ops_analysis_runner._internal_error_message
_tenant_wide_context = trial_job_runner.tenant_wide_context


def close_gateway_runtime(runtime: GatewayRuntime) -> None:
    with contextlib.suppress(Exception):
        runtime.shutdown()
