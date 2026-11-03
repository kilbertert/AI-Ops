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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

from aiops_diagnostics.common.async_job_lifecycle import QUESTION
from aiops_diagnostics.common.bounded_http import ErrorMapping, RequestSpec, request_json
from aiops_diagnostics.execution import chat_execution, execution_manifest
from aiops_diagnostics.execution.agent_runs import (
    AgentRunConfig,
    AgentRunDispatchError,
    SnailDispatcher,
)
from aiops_diagnostics.identity.query_scope import resolve_query_scope
from aiops_diagnostics.identity.scope_context import is_tenant_level_account
from aiops_diagnostics.operator import operator_conversation, operator_tools, site_note_review
from aiops_diagnostics.operator.operator_conversation import OperatorRefused, claims_mutation

SCENARIO = "operator_order_diagnosis"
PREFIX = "operator-cpms-node:"
#: 工具名、路径、输入与证据字段全部来自 `operator_tools.json` —— 仓内唯一声明。
TOOL_NAMES = operator_tools.names()

#: 追加在节点那条用户消息末尾的强制提醒。**实测出来的，不是措辞偏好**：
#: 带工具轮次的最后一步，`response_format=json_object` 有 36–44% 不生效（通篇正文），
#: 加上这一句后同一实验降到 8%。它不改变协议 —— 交付与否仍由 `_complete` 的校验决定。
JSON_ONLY_REMINDER = (
    "\n\n最后一步只输出那个JSON对象本身：不要任何前言、标题、列表、"
    "推理过程或解释，第一个字符必须是 {，最后一个字符必须是 }。"
    "即使证据不足，也照样输出带缺证说明的JSON。"
)

#: 一次 run 因为「上游没给可接受的答案」最多重派几次。**1 是刻意的**：
#: 实测单次失败率 36–44%，再派一次后剩余失败约 1–2%，再加一次只到 0.2% 却把最坏延迟
#: 翻倍。更重要的理由是不掩盖故障：上游**持续**不守约时，多试几次会让它看起来像"慢"。
MAX_RESAMPLES = 1

#: 站点查询的三个上限：一页条数、最多页数、最多展示条数。
#:
#: 合起来是**「列举不能变成导出」**这条边界。改任何一个都要重新论证，不能当调参。
SITE_PAGE_SIZE = 50
SITE_MAX_PAGES = 4
#: 展示上限的唯一定义在渲染模块（它被本模块导入，反向导入会成环），这里只是别名。
SITE_MAX_SHOWN = operator_conversation.SITE_MAX_SHOWN

#: 站点列表的 CPMS 路径。查询参数由 :class:`SiteKeywords` 组装，不由调用方拼接。
SITE_PATH = "/chSite/page"

#: 工具授权的**契约版本**。节点必须逐字声明它，否则在领取授权那一步就被拒。
#:
#: 这不是版本号装饰：授权载荷从「订单号」换成「这一次要发的请求」是破坏性变更，
#: 而两端的证据字段集合是**精确相等**比对的，混跑不会「碰巧还能用」，只会在某一次
#: 完成回调里以缺字段的形状炸开。把拒绝移到授权那一步，混跑就有一个稳定的错误码。
TOOL_CONTRACT = "request-target/1"
TOOL_CONTRACT_HEADER = "X-AI-Ops-Tool-Contract"


#: 每个工具这一次要发的请求。**路径在这里物化**，节点只执行拿到的那个请求 ——
#: 这正是「新增一条只读路径不必改 Java」的那一步。
def _declared(tool: str):
    """取声明；未声明的工具名仍然被拒绝，只是判据从常量变成了数据。"""
    try:
        return operator_tools.tool(tool)
    except operator_tools.ToolDeclarationError as exc:
        raise OperatorRefused("OPERATOR_TOOL_DISABLED") from exc


def tool_request(tool: str, values: dict[str, str]) -> dict[str, str]:
    """按工具名把这一次的请求目标物化出来。**这里是路径与查询串物化的唯一位置。**"""
    return _declared(tool).request(values)


def tool_inputs(tool: str, values: dict[str, str]) -> dict[str, str]:
    """这一次运行的输入。订单号是**输入**，不再是授权载荷的中心。"""
    return _declared(tool).declared_inputs(values)


def run_values(tool: str, order_no: str, arguments: dict[str, str] | None = None) -> dict[str, str]:
    """把「运行绑定的输入」与「模型提议的参数」合成一次取值。

    订单诊断的输入来自运行本身（订单号），站点查询的输入来自模型 —— 两者在这里汇合，
    于是下游（物化、证据比对、代理白名单）只有一种形状可处理。
    """
    values = dict(arguments or {})
    if "order_no" in _declared(tool).inputs:
        values["order_no"] = order_no
    return values


def allowed_in(tool: str, run) -> bool:
    """这个工具能不能出现在这一类运行里。

    无订单回合可以带工具，但**不是任何工具**：订单诊断工具没有订单就没有取证对象。
    """
    context = "order" if diagnoses_an_order(run) else "conversation"
    return context in _declared(tool).applies_to


#: 工具参数的字符纪律（ADR-0176）。与对话协议的搜索词同源：
#: 长度 1–80、不含 `%_\` 与换行。**另一半**——「必须是提问原文的子串」——只对模型提议的
#: 参数成立，因此在 :func:`validate_tool_arguments` 里单独判。
_ARGUMENT_FORBIDDEN = ("%", "_", "\\", "\r", "\n")


def validate_tool_arguments(tool: str, arguments, question: str) -> dict[str, str]:
    """放行模型提议的工具参数。**模型可以提议，能不能用由这里决定。**

    两条判据缺一不可：

    1. 键必须是声明里列过的那几个 —— 模型不能给工具加参数。
    2. 每个值必须能在用户提问原文里找到（只忽略空白与大小写，仍为连续子串）——
       编一个地址，或把上一个用户的地址搬过来，都在这里被判掉。
    """
    declared = _declared(tool)
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict) or set(arguments) - set(declared.inputs):
        raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
    cleaned: dict[str, str] = {}
    for key, value in arguments.items():
        if not isinstance(value, str):
            raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
        if len(value) > 80 or any(char in value for char in _ARGUMENT_FORBIDDEN):
            raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
        stripped = value.strip()
        if not stripped:
            continue
        original = operator_conversation.question_span(stripped, question)
        if not original:
            raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
        cleaned[key] = original
    return cleaned


#: 站点查询工具的名字。这里是**唯一**按名字认它的地方：判断「这一轮是否已经用工具
#: 取过站点」是运行级的事实，不是工具自身的属性。
SITE_TOOL = "cpms_site_search"


def still_authorized(binding, run, *, trial=False) -> bool:
    """冻结运行的模型/工具必须仍被当前应用授权，提示词可单独换版。"""
    if binding is None or binding.entry != "operator" or binding.route_revision < run.route_revision:
        return False
    if not run.publication_app_id:
        return binding.route_revision == run.route_revision
    if binding.app_id != run.publication_app_id:
        return False
    if trial or str(run.chat_binding).startswith("operator-trial:"):
        return binding.route_revision == run.route_revision
    try:
        frozen = execution_manifest.from_text(run.execution_manifest)
        current_text = (
            binding.manifest_text
            if diagnoses_an_order(run)
            else operator_conversation.manifest(binding.manifest_text)
        )
        current = execution_manifest.from_text(current_text)
        old_tools = {t["name"]: t["input_schema"] for t in json.loads(run.execution_manifest)["tools"]}
        new_tools = {t["name"]: t["input_schema"] for t in json.loads(current_text)["tools"]}
    except (ValueError, TypeError, KeyError):
        return False
    return (
        frozen.identity == run.expected_agent_version
        and all(
            getattr(frozen, key) == getattr(current, key)
            for key in ("provider_key", "adapter_key", "model_key", "endpoint", "history_policy")
        )
        and all(new_tools.get(name) == schema for name, schema in old_tools.items())
    )


def _sites_from_evidence(caller, task, answer):
    """这一轮已经用工具取过站点时，**复用那份证据**，不再查第二次。

    两件事同时成立才算复用：证据在、且回答里的关键词与工具参数一致。不一致说明模型
    在两次输出里说了不同的地方，那不是同一次查询 —— 拒绝比挑一个更安全。

    这就是「两个入口同一份事实」的真正含义：不是两次查询碰巧相同，而是一次查询被两个
    入口共用。
    """
    if SITE_TOOL not in task.delivered:
        return None
    evidence = task.evidence.get(SITE_TOOL)
    if evidence is None:
        raise OperatorRefused("OPERATOR_EVIDENCE_MISSING")
    arguments = task.arguments.get(SITE_TOOL, {})
    if answer["name"] != arguments.get("name", "") or (
        "".join(site_keywords(answer).address.split()).casefold()
        != "".join(arguments.get("address", "").split()).casefold()
    ):
        raise OperatorRefused("OPERATOR_SITE_SEARCH_MISMATCH")
    data = evidence["data"]
    scope = operator_scope(caller, allow_empty=True)
    return {
        "source": evidence["source"],
        "observed_at": evidence["observed_at"],
        "sites": data["records"],
        # `limited` 只说「查询可能没穷尽」；展示只列 10 条由渲染层的「另有 N 个」说明 ——
        # 两件事混用会让一句「结果可能不完整」盖住本来完整的 11 条页。
        "limited": bool(data["truncated"]),
        "empty_scope": scope.site_ids == (),
        "total": int(data["total"]),
        "scope_limited": scope.site_ids is not None,
    }


def diagnoses_an_order(run) -> bool:
    """这次运行是不是**订单诊断**。

    判据是运行类型 ``run.operation``，**不是订单号是否为空**。两者今天恰好一致——
    签发时就是按有没有订单二选一的——但一致是结果，不是判据。一旦有不需要订单的能力
    要进工具面，这个区别就是全部差别：拿订单号当判据，那种能力在类型上就写不出来。

    订单号仍承担它真正的工作（拼路径、冻结站点、比证据），只是不再兼职表达
    "这次跑的是什么"。
    """
    return run.operation == SCENARIO


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
    #: 每个工具这一次的取值（订单号或模型提议的关键词）。放行一次就固定，后续步骤只读它。
    arguments: dict = field(default_factory=dict)
    conversation_turn: tuple | None = None
    question: str = ""
    language: str = "zh"
    trial: bool = False
    #: 本次运行已经因为「模型没吐 JSON」重派过几次。**上限是 1**，见 `_complete`。
    #: 放在任务上而不是模块上：它属于这一次 run，跨 run 复用就没有"有界"可言了。
    resamples: int = 0
    lock: object = field(default_factory=threading.RLock)
    completion_lock: object = field(default_factory=threading.Lock)


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
        caller = self.refresh_scope(caller, allow_empty=bool(run and not diagnoses_an_order(run)))
        if run is None or run.scope_fingerprint != caller.scope_fingerprint:
            raise OperatorRefused("QA_NOT_FOUND")
        from aiops_diagnostics.execution.node_publication import all_targets

        binding = chat_execution.binding_for_key(self.context.settings, run.chat_binding)
        if not still_authorized(binding, run, trial=bool((run.snapshot or {}).get("trial"))):
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
        if not diagnoses_an_order(run):
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
            manifest_text = operator_conversation.manifest(manifest_text)
        manifest = execution_manifest.from_text(manifest_text)
        if order and (
            TOOL_NAMES[0] not in manifest.tool_names or not set(manifest.tool_names).issubset(TOOL_NAMES)
        ):
            raise OperatorRefused("OPERATOR_DETAIL_TOOL_DISABLED")
        # 查询只用于确认当前订单归属，不把它冒充节点本轮取得的诊断证据。
        order_site_id = ""
        if order:
            body = cpms_get(
                base + tool_request(TOOL_NAMES[0], run_values(TOOL_NAMES[0], order))["path"],
                bearer,
                caller.effective_tenant_id,
            )
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
                publication_app_id=binding.app_id,
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
        if not still_authorized(binding, run, trial=task.trial):
            raise OperatorRefused("OPERATOR_RESULT_RETRACTED")
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
        if manifest is not None and manifest != expected.identity:
            raise OperatorRefused("OPERATOR_MANIFEST_MISMATCH")
        if tool is not None and (
            tool not in TOOL_NAMES or tool not in expected.tool_names or not allowed_in(tool, run)
        ):
            # 未声明的工具、没被勾选的工具、以及「这一类运行不该用的工具」都在这里挡住。
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
        }

    def grant(self, run_id, token, manifest, tool, contract="", arguments=None):
        if contract != TOOL_CONTRACT:
            # 旧节点不声明契约版本。挡在**领授权**这一步，而不是等它在完成回调里因为
            # 证据缺字段而失败——那既不是稳定错误码，也发生在公司令牌已经用过之后。
            raise OperatorRefused("OPERATOR_TOOL_CONTRACT_OUTDATED")
        task, run, _ = self._current(run_id, node_token=token, manifest=manifest, tool=tool)
        # 参数放行发生在**取到公司令牌之前**：模型编的地址不会换来一次真实的对外请求。
        arguments = validate_tool_arguments(tool, arguments, task.question)
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
            "contract": TOOL_CONTRACT,
            "base_url": task.proxy_url + "/v1/internal/operator-runs/" + run_id + "/cpms",
            "bearer": task.grant,
            "expires_at": run.expires_at.isoformat(),
        }

    def _grant_sites(self, task, run, tool) -> list[str] | None:
        """这一次授权允许碰的站点。

        订单诊断：订单所在的那**一个**站点（保持既有收窄）。
        无订单（站点工具）：账号可见的站点集合；空集合是合法的（「你确实没有站点」），
        不放大成租户。
        """
        if diagnoses_an_order(run):
            return [task.order_site_id]
        scope = operator_scope(task.caller, allow_empty=True)
        return list(scope.site_ids) if scope.site_ids is not None else None

    def proxy(self, run_id, grant, path, query, method="GET"):
        task = self.tasks.get(run_id)
        if task is None or not secrets.compare_digest(task.grant, grant):
            raise OperatorRefused("OPERATOR_GRANT_REJECTED")
        run = self.context.agent_run_store.get(run_id)
        if run is None:
            raise OperatorRefused("OPERATOR_RUN_INACTIVE")
        # 白名单是**完整请求目标**：方法、路径、查询串三者都对才算。拿工具名反推路径
        # 的做法已经去掉——那正是「加一条只读路径就要改 Java」的来源。
        tool = next(
            (
                name
                for name in TOOL_NAMES
                if allowed_in(name, run)
                and tool_request(name, run_values(name, run.order_no, task.arguments.get(name)))
                == {"method": method, "path": path, "query": query}
            ),
            None,
        )
        if tool is None:
            raise OperatorRefused("OPERATOR_PATH_REJECTED")
        with task.lock:
            self._current(run_id, tool=tool)
            if tool in task.evidence:
                return task.evidence[tool]["response"]
            if tool == TOOL_NAMES[1] and TOOL_NAMES[0] not in task.delivered:
                raise OperatorRefused("OPERATOR_DETAIL_REQUIRED")

            body = (
                cpms_get(
                    task.base_url + path + ("?" + query if query else ""),
                    task.bearer,
                    task.caller.effective_tenant_id,
                )
                if tool != SITE_TOOL or site_scope(task.caller).site_ids != ()
                else {"code": 0, "data": {"records": [], "total": 0, "current": 1, "size": 50}}
            )
            if tool == SITE_TOOL:
                data, response = site_tool_result(
                    site_scope(task.caller), body, tool, task.arguments.get(tool) or {}
                )
                self._current(run_id, tool=tool)
                task.evidence[tool] = {
                    "tool": tool,
                    "inputs": tool_inputs(tool, run_values(tool, run.order_no, task.arguments.get(tool))),
                    "source": operator_tools.tool(tool).source,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "data": data,
                }
                task.evidence[tool]["response"] = response
                return task.evidence[tool]["response"]
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
        request = tool_request(tool, run_values(tool, run.order_no))
        return self.proxy(run_id, grant, request["path"], request["query"], request["method"])

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
                or set(evidence) != {"tool", "inputs", "source", "observed_at", "data"}
                or evidence.get("tool") != tool
                or evidence.get("data") != expected["data"]
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
        task, _, _ = self._current(run_id, node_token=token, manifest=manifest)
        with task.completion_lock:
            return self._complete(run_id, token, manifest, payload)

    def _complete(self, run_id, token, manifest, payload):
        if set(payload) - {"status", "answer", "reason"}:
            raise OperatorRefused("OPERATOR_INVALID_BODY")
        task, run, _ = self._current(run_id, node_token=token, manifest=manifest)
        store = self.context.agent_run_store
        if payload.get("status") not in {"completed", "failed"}:
            raise OperatorRefused("OPERATOR_INVALID_COMPLETION")
        if payload["status"] == "failed":
            # 节点给的原因码进运行记录，不再统一糊成 `operator_node_failed`。
            # 它只用于**归因**，不改变任何判决：这条 run 仍然失败。
            reason = str(payload.get("reason") or "")
            store.fail(run_id, "operator_node_failed: " + reason if reason else "operator_node_failed")
            logging.getLogger("aiops.operator").warning(
                "operator_node_reported_failure reason=%s", reason or "-"
            )
            return {"run_id": run_id, "state": "failed"}
        with task.lock:
            try:
                raw = payload.get("answer", "")
                if not isinstance(raw, str) or len(raw) > 24000:
                    raise ValueError("invalid answer")
                answer = parse_answer(raw)
            except ValueError as exc:
                # `json_parse` 一个码覆盖**四种**完全不同的情形，此前一律记成
                # 「模型没吐 JSON」，这是过度归因（2026-11-03 复核）。
                # 只记形态与长度，**不记原文**：回答是用户可见文本，可能带单号与金额。
                #
                # `json_syntax` 尤其需要多说一句：它意味着**开头像 JSON 却解析失败**，
                # 也就是被截断或带尾逗号。2026-11-03 线上 11 次里有 5 次是它，
                # 而光凭 `json_syntax` 分不出「上游 max_tokens 截断」与「模型自己拼错」。
                # 记下长度与是否以 `}` 收尾，就能分开这两件事 —— 长度贴着某个上限
                # 反复出现且没有收尾，是截断；长度散落且收尾完整，是语法错。
                logging.getLogger("aiops.operator").warning(
                    "operator_answer_rejected reason=json_parse %s%s",
                    _json_shape(raw),
                    _json_truncation_hint(raw),
                )
                # **重采样不在这里**：这是节点回调，从这里抛异常只能让本次回调失败，
                # 不会让模型再答一次。有界重派在 `_worker` 里（见 `_resample_used`）。
                raise OperatorRefused("OPERATOR_INVALID_ANSWER") from exc
            from aiops_diagnostics.common.redaction import contains_secret

            if contains_secret(raw, (task.bearer, task.grant)):
                _answer_refused("credential")
            sites = None
            if diagnoses_an_order(run):
                if "{{fact:order_facts}}" in run.execution_manifest and "reply" not in answer:
                    _answer_refused("reply_required")
                validate_answer(answer, task.evidence)
                if not set(answer["evidence_tools"]).issubset(task.delivered):
                    raise OperatorRefused("OPERATOR_EVIDENCE_NOT_DELIVERED")
            else:
                answer = operator_conversation.validate(answer, task.question, check_site_note=False)
                if SITE_TOOL in task.delivered and answer["kind"] != "site_search":
                    raise OperatorRefused("OPERATOR_SITE_SEARCH_MISMATCH")
                if answer["kind"] == "site_search":
                    sites = _sites_from_evidence(task.caller, task, answer)
                    if sites is None:
                        # 节点没调工具：说明是在**看到结果之前**写的，不能当查询结果交付。
                        # 这里拒而不是静默丢掉：静默丢掉会让提示词作者以为它在生效。
                        if answer["text"].strip():
                            raise OperatorRefused("OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE")

                        def current_search_scope():
                            current_task, _, _ = self._current(run_id, node_token=token, manifest=manifest)
                            return current_task.caller.scope_fingerprint

                        sites = query_sites(
                            site_scope(task.caller),
                            site_keywords(answer),
                            cpms_site_reader(task.base_url, task.bearer),
                            current_search_scope,
                        )
        # 审核不持有证据锁；取消、撤权和证据回调仍能进行。完成前重新授权。
        review = None
        if sites is not None:
            review = site_note_review.review(
                answer, sites, task.language, self.context.runtime.diagnostic_settings.agent
            )
            if review["verdict"] != "allow":
                answer = {**answer, "text": ""}
        with task.lock:
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
            if not diagnoses_an_order(run):
                snapshot["sites"] = sites
                snapshot["language"] = task.language
                snapshot["rendered_result"] = operator_conversation.render(
                    answer, sites, task.language, reviewed=True
                )
                if review is not None:
                    snapshot["site_note_review"] = review
                    if task.trial and review["verdict"] not in {"allow", "empty"}:
                        snapshot["rendered_result"]["blocks"][0]["text"] += (
                            "\n\n查询结果已交付；说明未通过审核，已隐藏。"
                            if task.language.startswith("zh")
                            else "\n\nQuery results delivered; the unapproved explanation is hidden."
                        )
            else:
                snapshot["language"] = task.language
                # **在完成时渲染并冻结**：事实段的语言按运行所选语言，历史回读不再重算 ——
                # 升级渲染器不会改写已经交付的答复。
                snapshot["rendered_report"] = render_report(
                    answer, snapshot["evidence"], language=task.language
                )
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

    def _worker(self, run_id, qa_id, config):
        store = self.context.store
        try:
            if not store.update_assistant_question(qa_id, status="running"):
                raise OperatorRefused("OPERATOR_RUN_INACTIVE")
            dispatcher = SnailDispatcher()
            dispatcher.deadline_at = self.context.agent_run_store.get(run_id).expires_at
            task = self.tasks[run_id]
            dispatcher.question = (
                (
                    "诊断本次授权订单，按已发布SOP读取证据并输出规定JSON。用户描述仅供理解问题："
                    if diagnoses_an_order(self.context.agent_run_store.get(run_id))
                    else "本轮未指定订单，按管家对话协议回答或提出站点搜索条件。用户问题："
                )
                + json.dumps(task.question, ensure_ascii=False)
                + "。回答语言："
                + task.language
                # 这一句是实测出来的，不是客套：`response_format=json_object` 对供应商只是**提示**，
                # 带工具轮次的最后一轮实测有 36–44% 的响应是通篇正文、根本没有 JSON
                # （2026-11-03，41 生产，每组 25 次）。把「最后一步只输出 JSON」写进模型
                # 看得到的用户消息后，同一实验里失败率降到 8%（2/25）。
                #
                # 放在这里而不是节点的 system 提示词里：节点对 system 消息集合做**逐字**
                # 校验（GuardedChatModel.verifySystemMessages），改它要换节点 jar 与清单；
                # 而这条用户消息正是节点看到的那一条，只改 Python。
                # 它也不越过协议 —— 最终用什么由 `_complete` 的校验决定，这里只是让模型少犯。
                + JSON_ONLY_REMINDER
            )
            # **有界重采样**：`response_format=json_object` 对供应商只是提示，上游失约是概率性的
            # （2026-11-03 实测最后一轮 36–44% 不吐 JSON）。再要一次是合理的，**但只再要一次** ——
            # 无限重试会把「上游持续不守约」伪装成「慢」，而它其实是故障。
            #
            # 重采样的位置只能在这里：`_complete` 是节点回调，从那里抛异常只会让**这次回调**
            # 失败，节点收到非成功回执后**不重试**（`ReportingChatSessionRuntime.reportCompleted`
            # 返回 false 即终局），模型不会再答一次。所以「再答一次」= 重新派发一次完整对话。
            #
            # **拒绝是以异常形式回来的，不是以状态形式回来的**（2026-11-03 复审指出）。
            # 网关回 4xx/409 给平台 → 平台那次调用失败 → `SnailDispatcher` 把它映射成
            # `AgentRunDispatchError` 抛出来。所以「回调被拒」根本走不到下面那行状态判断，
            # 会直接掉进外层 except。必须在这里先接住。
            while True:
                try:
                    dispatcher.dispatch(config, run_id)
                except AgentRunDispatchError:
                    if not self._untouched(run_id, task):
                        raise
                    if task.resamples >= MAX_RESAMPLES:
                        raise
                    task.resamples += 1
                    logging.getLogger("aiops.operator").warning(
                        "operator_resample attempt=%d reason=dispatch_refused", task.resamples
                    )
                    continue
                if not self._untouched(run_id, task):
                    break
                # 派发本身成功、回调也走完了，但 run 没被推进 —— 说明 `_complete` 拒了答案。
                if task.resamples >= MAX_RESAMPLES:
                    raise OperatorRefused("OPERATOR_MODEL_OUTPUT_UNSTABLE")
                task.resamples += 1
                logging.getLogger("aiops.operator").warning(
                    "operator_resample attempt=%d reason=no_answer_accepted", task.resamples
                )
            task = self.tasks[run_id]
            current = task.authorize()
            result = self.authorize_readback(current, qa_id, task.bearer)
            if store.update_assistant_question(qa_id, status="completed", result=result):
                self.context.runtime._complete_conversation_turn(
                    task.conversation_turn, task.question, result, status_qa_id=qa_id
                )
        except Exception as exc:  # noqa: BLE001 - 后台失败必须收敛，凭据及上游正文不进日志。
            reason = _failure_reason(exc)
            if not self.context.agent_run_store.fail(run_id, "operator_failed"):
                self.context.agent_run_store.refuse(run_id, "operator_failed")
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
            or run.operation not in {SCENARIO, operator_conversation.SCENARIO}
        ):
            raise OperatorRefused("QA_NOT_FOUND")
        binding = chat_execution.binding_for_key(self.context.settings, run.chat_binding)
        if not still_authorized(
            binding, run, trial=bool((run.snapshot or {}).get("trial"))
        ) or run.expires_at <= datetime.now(UTC):
            raise OperatorRefused("OPERATOR_RESULT_RETRACTED")
        config = AgentRunConfig.load(Path(binding.config_file))
        if execution_manifest.from_text(run.execution_manifest).identity != run.expected_agent_version:
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
        if not diagnoses_an_order(run):
            sites = run.snapshot.get("sites")
            scope = operator_scope(caller, allow_empty=True)
            if (
                sites
                and scope.site_ids is not None
                and any(s["id"] not in scope.site_ids for s in sites["sites"])
            ):
                raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
            return run.snapshot.get("rendered_result") or operator_conversation.render(
                run.snapshot["answer"], sites, run.snapshot["language"]
            )
        scope = operator_scope(caller)
        detail = run.snapshot["evidence"][TOOL_NAMES[0]]["data"]
        if scope.site_ids is not None and detail["siteId"] not in scope.site_ids:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        answer = run.snapshot["answer"]
        if "{{fact:order_facts}}" in run.execution_manifest and "reply" not in answer:
            _answer_refused("reply_required")
        validate_answer(answer, run.snapshot["evidence"])
        # 历史 run 用**当时冻结**的正文；没有冻结文本的旧 run 按中文标签重算，
        # 与它们签发时的呈现一致（升级不改写历史）。
        text = run.snapshot.get("rendered_report") or render_report(
            answer, run.snapshot["evidence"], language="zh"
        )
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
DETAIL_FIELDS = list(operator_tools.tool("cpms_order_detail").evidence_fields)
SAMPLE_FIELDS = list(operator_tools.tool("cpms_charging_samples").evidence_fields)


def site_tool_result(scope, body, tool, arguments, *, outside_scope="reject"):
    """证据只含声明字段；节点取数仍需分页回显和已核对的租户身份。"""
    rows, total = site_page(scope, body, 1, outside_scope=outside_scope)
    keywords = SiteKeywords(
        arguments.get("name", ""), (arguments["address"],) if arguments.get("address") else ()
    )
    rows = [row for row in rows if keywords.matches(row)]
    declared = operator_tools.tool(tool)
    data = {
        "records": [{key: row[key] for key in declared.evidence_fields} for row in rows],
        "total": total,
        "sample_limit": declared.page_size,
        # ``truncated`` 是**证据**字段：页面没有覆盖上游的全部行。它与「展示只列 10 条」
        # 是两件事，不能合并 —— Java 节点按 ``total > rows.size()`` 逐字重算这一项并与
        # 网关比对，改口径会让每一次真实运行都以 OPERATOR_EVIDENCE_MISMATCH 失败。
        # 展示上限在 ``operator_conversation.shown_sites`` 收口。
        "truncated": total > len(rows),
        "ordering_guaranteed": declared.flags["ordering_guaranteed"],
        "complete_timeline": declared.flags["complete_timeline"],
        "samples_returned": len(rows),
        "absence": declared.flags["absence_when_empty"] if not rows else None,
    }
    response = {
        "code": 0,
        "data": {
            "records": [dict(row, tenantId=scope.tenant_id) for row in data["records"]],
            "total": total,
            "current": 1,
            "size": declared.page_size,
        },
    }
    return data, response


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


def _json_shape(raw) -> str:
    """回答为什么不是 JSON —— **复用** `consumer_documents._json_failure_shape`。

    不另写一份：那份已经解决了同一个问题（描述形态、不泄露原文），
    而且它自己的注释就写着这个缺口是 2026-11-01 订单链路发现的。
    """
    from aiops_diagnostics.consumer.consumer_documents import _json_failure_shape

    return _json_failure_shape(raw)


def _json_truncation_hint(raw) -> str:
    """`json_syntax` 的两种成因要分得开：被截断，还是自己拼错。

    ``closed=`` 只看末尾字符是不够的：截断恰好落在字符串里的 ``}`` 上也会得到 True，
    而尾逗号语法错得到 False —— 单凭它判不了成因（2026-11-03 复审指出）。
    所以再给一个**括号配对**事实：``depth=`` 是去掉字符串内容后从头累计的括号差，
    ``depth>0`` 才说明结构没闭合（截断）；``depth=0`` 而解析失败才是语法错。
    两个数字都不含原文，只是结构。
    """
    from aiops_diagnostics.consumer.consumer_documents import _json_failure_shape

    if not isinstance(raw, str) or _json_failure_shape(raw) != "json_syntax":
        return ""
    text = raw.rstrip()
    depth, quoted, escaped = 0, False, False
    for ch in text:
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
            continue
        if ch == '"':
            quoted = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
    return f" len={len(text)} closed={text.endswith('}')} depth={depth} in_string={quoted}"


def _failure_code(exc: BaseException) -> str:
    """这次失败对**用户**报哪个码。

    此前一律 `OPERATOR_DIAGNOSIS_FAILED`，而那个码同时覆盖「模型不吐 JSON」「上游没钱」
    「答案不合格」「节点掉线」—— 值班的人分不出该找谁。这里只把**已经能区分**的几类
    分开，其余保持原码：宁可不分，不能猜错。
    """
    if isinstance(exc, OperatorRefused):
        code = str(exc)
        # 这些原因说明**上游/环境**有问题，不是这次回答不合格 —— 分开报。
        if code in {
            "OPERATOR_NODE_REJECTED",
            "OPERATOR_NODE_CHANGED",
            "OPERATOR_TARGET_MISSING",
            "CPMS_UNAVAILABLE",
            "OPERATOR_SCOPE_UNAVAILABLE",
        }:
            return "OPERATOR_DEPENDENCY_UNAVAILABLE"
        if code == "OPERATOR_RESULT_UNSTABLE":
            return "OPERATOR_MODEL_OUTPUT_UNSTABLE"
    return "OPERATOR_DIAGNOSIS_FAILED"


def _failure_reason(exc: BaseException) -> str:
    """失败原因的一句话摘要，**只用于日志归因**，不进用户可见文本。

    不记异常正文里可能带的凭据或上游原文：`AgentRunDispatchError` 的文案本来就是
    「平台不可用」这种不含细节的句子，这里只额外带上类型与错误码。
    """
    if isinstance(exc, OperatorRefused):
        return str(exc)
    return type(exc).__name__


def _answer_refused(reason, detail=""):
    """拒绝一次回答，并尽量记下**哪条规则**被触发。

    只记 `reason=unsupported_fact` 时，下一跳只能靠猜：这条码同时覆盖「事实段外写了金额」
    「写了状态」「写了支付结果」，成因完全不同。失败的 run 又**不保存模型的原始输出**
    （`snapshot_json.answer` 恒为 `None`，实测），所以拒绝那一刻是唯一的取证点。

    `detail` 由调用点给：命中规则名 + 行号 + 该行长度。**不记原文、不记金额** ——
    日志没有脱敏层，而这行正是最容易带出金额的地方。
    """
    logging.getLogger("aiops.operator").warning(
        "operator_answer_rejected reason=%s%s", reason, f" {detail}" if detail else ""
    )
    raise OperatorRefused("OPERATOR_INVALID_ANSWER")


ANSWER_FIELDS = frozenset(
    {"direct_answer", "summary", "probable_cause", "next_steps", "limitations", "evidence_tools"}
)


def validate_answer(answer, evidence):
    if not isinstance(answer, dict) or set(answer) not in (ANSWER_FIELDS, ANSWER_FIELDS | {"reply"}):
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
        # 同一个码覆盖**四种**完全不同的情形，日志里必须分得开 —— 否则下一跳只能猜
        # （2026-11-03 线上 3 次这个码，无法归因）。区分的是原因，不是判决：
        # 四种都仍然拒绝，只是写清是哪一种。
        if TOOL_NAMES[0] not in evidence:
            detail = "detail=not_read"  # 订单详情这一轮根本没取到
        elif TOOL_NAMES[0] not in answer["evidence_tools"]:
            detail = "detail=not_cited"  # 取到了，但模型没在 evidence_tools 里引它
        else:
            extra = sorted(set(answer["evidence_tools"]) - set(evidence))
            detail = "detail=unknown_tool"  # 引了本轮没取过的工具
            if extra:
                detail += f" name={extra[0][:32]}"
        logging.getLogger("aiops.operator").warning("operator_evidence_missing %s", detail)
        raise OperatorRefused("OPERATOR_EVIDENCE_MISSING")
    if "reply" in answer and (not isinstance(answer["reply"], str) or not 1 <= len(answer["reply"]) <= 6000):
        _answer_refused("reply")
    if "reply" in answer:
        _validate_reply_facts(answer["reply"])
    text = json.dumps(answer, ensure_ascii=False)
    if claims_mutation(text):
        _answer_refused("mutation_claim")


def _validate_reply_facts(text):
    from aiops_diagnostics.consumer.consumer_facts import check_notice, check_status_prose

    slot = "{{fact:order_facts}}"
    if text.splitlines().count(slot) != 1:
        _answer_refused("order_facts_required")
    for index, line in enumerate(text.splitlines(), 1):
        if line == slot:
            continue
        if "{{" in line or "}}" in line:
            _answer_refused("fact_segment")
        try:
            check_notice(line)
        except chat_execution.ChatRunUnavailable:
            # 带行号与检查名：`unsupported_fact` 一条码覆盖多种成因，只记码等于没取证。
            # **不记原文** —— 这几行正是最容易带出金额的。
            _answer_refused("unsupported_fact", f"detail=notice line={index} len={len(line)}")
        try:
            check_status_prose(line)
        except chat_execution.ChatRunUnavailable:
            _answer_refused("unsupported_fact", f"detail=status line={index} len={len(line)}")


#: 管家事实段的字段标签。**运行语言决定标签**（值与来源不变）。
#: zh/en 有完整译文；其余语种退回英文并在 ADR 里记为缺口 —— 不把中文标签混进外文句子。
_FACT_LABELS = {
    "zh": {
        "order": "订单号",
        "status": "订单状态",
        "isPrePay": "是否预付费",
        "prePayAmount": "预付费",
        "electricityFee": "电费",
        "serviceFee": "服务费",
        "dsElectricFee": "电损电费",
        "dsServiceFee": "电损服务费",
        "launchFee": "启动费用",
        "marketAmount": "优惠金额",
        "vat": "增值税比例",
        "vatFee": "增值税费用",
        "totalAmount": "合计",
        "payAmount": "系统记录的支付金额",
        "payTime": "系统记录的付款时间",
        "occupyFee": "占位费",
        "refundAmount": "系统记录的退款金额",
        "electricity": "电量（kWh）",
        "time": "充电时长",
        "voltage": "电压",
        "gunPower": "额定功率",
        "stoppedReasonContent": "系统记录的停止原因",
        "samples": "本次取得监测样本",
        "missing": "未提供",
        "limits": "币种和时长单位需与原订单核对；账务记录不证明银行到账，当前状态不证明历史故障原因。",
        "colon": "：",
    },
    "en": {
        "order": "Order",
        "status": "Order status",
        "isPrePay": "Prepaid",
        "prePayAmount": "Prepaid amount",
        "electricityFee": "Electricity fee",
        "serviceFee": "Service fee",
        "dsElectricFee": "Line-loss electricity fee",
        "dsServiceFee": "Line-loss service fee",
        "launchFee": "Startup fee",
        "marketAmount": "Discount amount",
        "vat": "VAT rate",
        "vatFee": "Tax fee",
        "totalAmount": "Total",
        "payAmount": "Recorded payment amount",
        "payTime": "Recorded payment time",
        "occupyFee": "Occupancy fee",
        "refundAmount": "Recorded refund amount",
        "electricity": "Energy (kWh)",
        "time": "Charging duration",
        "voltage": "Voltage",
        "gunPower": "Rated power",
        "stoppedReasonContent": "Recorded stop reason",
        "samples": "Monitoring samples returned",
        "missing": "not provided",
        "limits": (
            "Currency and duration units must be checked against the original order; "
            "a recorded payment does not prove the bank received it, and the current "
            "status does not prove the historical root cause."
        ),
        "colon": ": ",
    },
}


def _status_word(code, language: str) -> str:
    """状态**值**也按运行语言：复用 `consumer_language.COPY` 的现役译文。"""
    from aiops_diagnostics.consumer.consumer_language import COPY
    from aiops_diagnostics.data.order_status import role_of
    from aiops_diagnostics.health.rules import ORDER_STATUS

    role = role_of(code).value
    if language.startswith("zh"):
        return ORDER_STATUS.get(code, str(code))
    table = COPY.get(language)
    if table and isinstance(table.get(role), str):
        return table[role]
    if language.startswith("zh"):
        return ORDER_STATUS.get(code, str(code))
    return role.replace("_", " ")


def _fact_labels(language: str) -> dict[str, str]:
    return _FACT_LABELS["zh" if language.startswith("zh") else "en"]


def _order_fact_text(evidence, language="zh"):

    labels = _fact_labels(language)
    colon = labels["colon"]
    item = evidence[TOOL_NAMES[0]]
    detail = item["data"]
    lines = [
        labels["order"] + colon + str(item.get("order_no") or detail.get("orderNo") or labels["missing"])
    ]
    # 顺序按后台「费用信息」那一栏排：先问是否预付、预付了多少，再是各项费用，最后是合计与
    # 实付。只列**上游给了值**的项 —— 缺字段就是「后台没有这一列」，不补零也不补空。
    fields = (
        ("status", "status"),
        ("isPrePay", "isPrePay"),
        ("prePayAmount", "prePayAmount"),
        ("electricityFee", "electricityFee"),
        ("serviceFee", "serviceFee"),
        ("dsElectricFee", "dsElectricFee"),
        ("dsServiceFee", "dsServiceFee"),
        ("launchFee", "launchFee"),
        ("occupyFee", "occupyFee"),
        ("marketAmount", "marketAmount"),
        ("vat", "vat"),
        ("vatFee", "vatFee"),
        ("totalAmount", "totalAmount"),
        ("payAmount", "payAmount"),
        ("payTime", "payTime"),
        ("refundAmount", "refundAmount"),
        ("electricity", "electricity"),
        ("time", "time"),
        ("voltage", "voltage"),
        ("gunPower", "gunPower"),
        ("stoppedReasonContent", "stoppedReasonContent"),
    )
    for key, label_key in fields:
        value = detail.get(key)
        if value is None:
            continue
        if key == "status":
            shown = _status_word(value, language)
        elif key == "isPrePay":
            # 布尔值直接印 `True`/`False` 是给开发者看的；后台那一列写的是「是/否」。
            shown = _yes_no(value, language)
        elif key == "payTime":
            # 与报告末尾的取证时间同一个写法：CPMS 给的是不带时区的本地时间，原样显示，
            # 不补一个假的 UTC 后缀。
            shown = _readable_time(value)
        elif key == "vat":
            # CPMS 的 `vat` 是**百分数**（这单实测 `2.0`，后台那一列写「2%」）。
            # 直接印 `2.0` 会被读成「2 元增值税」，所以带单位。
            shown = _percent(value)
        else:
            shown = str(value)
        lines.append(labels[label_key] + colon + shown)
    samples = evidence.get("cpms_charging_samples", {}).get("data")
    if samples is not None:
        lines.append(
            labels["samples"] + colon + str(samples.get("samples_returned", len(samples.get("records", []))))
        )
    lines.append(labels["limits"])
    return "\n".join(lines)


def _percent(value) -> str:
    """`2.0` → `2%`。**只用于本来就是百分数的字段**（`vat`），不是通用格式化。

    这单的真实响应给的是 `vat: 2.0`，而后台那一列写「增值税率 2%」。印成 `2.0`
    会被读成金额 —— 同一行下面正好是「增值税费用 0.16」，两个数挨着，读错的代价是真金白银。
    解析不了就原样返回，不猜。
    """
    try:
        shown = format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)
    return shown + "%"


def _yes_no(value, language: str) -> str:
    """布尔证据按人话写。后台那一列是「是/否」，不是 `True`/`False`。"""
    if value is True:
        return "是" if language.startswith("zh") else "Yes"
    if value is False:
        return "否" if language.startswith("zh") else "No"
    return str(value)


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
_SOURCE_LABELS_EN = {
    "cpms.orderDetail": "Order detail",
    "cpms.chargingMonitor": "Charging monitor samples",
}


def _source_label(source, language: str) -> str:
    if language.startswith("zh"):
        return _SOURCE_LABELS.get(source, source)
    return _SOURCE_LABELS_EN.get(source) or _SOURCE_LABELS.get(source, source)


_SOURCE_LABELS = {
    "cpms.orderDetail": "订单详情",
    "cpms.chargingMonitor": "充电监测样本",
}


def render_report(answer, evidence, *, language="zh") -> str:
    """把结构化诊断渲染成**人读的报告**。

    之前这里是 ``"\\n\\n".join([...])``：九段文字被摊平成同等重要的段落，列表失去编号、
    没有任何小标题、技术标识（``cpms.orderDetail``、带微秒的 UTC 时间）直接摆给用户。
    内容全对，但没有一处告诉读的人"哪句是结论、哪句是下一步"。

    前端按 ``blocks[]`` 分派渲染、**不解析 Markdown**（frontend-api-brief §9），所以这里
    只用纯文本能表达的层级：小标题、``-`` 列表、缩进、空行。

    顺序是刻意的：先回答问题，再给事实，最后才是排查建议与缺证 —— 运维最常要的是前两段。
    """
    if answer.get("reply"):
        zh = language.startswith("zh")
        sources = "\n".join(
            f"- {_source_label(item.get('source'), language)}"
            + (
                f"（{_readable_time(item.get('observed_at'))}）"
                if zh
                else f" ({_readable_time(item.get('observed_at'))})"
            )
            for item in evidence.values()
        )
        text = answer["reply"].replace("{{fact:order_facts}}", _order_fact_text(evidence, language))
        heading = "【数据来源】" if zh else "Sources"
        return text + (f"\n\n{heading}\n" + sources if sources else "")
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


@dataclass(frozen=True, slots=True)
class SiteScope:
    """一次站点查询的可见范围。

    ``site_ids`` 有三种含义，**不能互相顶替**：

    * ``()`` —— 空范围。不查 CPMS，直接报"尚未分配"。
    * ``None`` —— 未按站点集合收窄（顶层账号的租户口径）。
    * 非空元组 —— 员工账号能看到的那些站点。

    ``fingerprint`` 不参与过滤，只用于逐页复核范围在这期间没变过。
    """

    tenant_id: str
    site_ids: tuple[str, ...] | None
    fingerprint: str


@dataclass(frozen=True, slots=True)
class SiteKeywords:
    """一次站点查询的关键词。四个字段全空表示**列举**，不是搜索（ADR-0172）。

    ``address_terms`` 可以有多条（对话协议允许至多 4 条），而 CPMS 的 ``address``
    只有一个：取最长的那个，与放开列举之前的行为逐字一致。
    """

    name: str = ""
    address_terms: tuple[str, ...] = ()

    @property
    def listing(self) -> bool:
        return not self.name and not self.address_terms

    @property
    def address(self) -> str:
        """答复可把同一地址拆成多个关键词；仅用于对齐已执行的工具参数。"""
        return "".join(self.address_terms)

    def params(self, page: int) -> dict[str, object]:
        params: dict[str, object] = {"current": page, "size": SITE_PAGE_SIZE, "type": 0}
        if self.name:
            params["name"] = self.name
        if self.address_terms:
            params["address"] = max(self.address_terms, key=len)
        return params

    def matches(self, site: dict[str, str]) -> bool:
        haystack = (site["name"] + " " + site["address"]).casefold()
        return self.name.casefold() in site["name"].casefold() and all(
            term.casefold() in haystack for term in self.address_terms
        )


def site_scope(caller, *, allow_empty: bool = True) -> SiteScope:
    """把调用者的数据范围翻译成站点查询的可见范围。

    账号层面的判据（要有 B 端主体、不接受代查、范围类型必须是 all/organ）仍在
    :func:`operator_scope` 里，本函数不复制它 —— 换一个入口要换的是那套判据，
    不是「站点怎么查」。
    """
    scope = operator_scope(caller, allow_empty=allow_empty)
    return SiteScope(caller.effective_tenant_id, scope.site_ids, caller.scope_fingerprint)


def site_keywords(answer) -> SiteKeywords:
    """对话协议的四个字段 → 关键词。空关键词是列举，不是错误。"""
    return SiteKeywords(answer["name"], tuple(answer["address_terms"]))


def cpms_site_reader(base: str, bearer: str):
    """直连 CPMS 的读取函数。

    :func:`query_sites` 只要求「给路径和租户、回 JSON 信封」这一个形状，所以节点代理
    那条路（节点持令牌、网关只放行）可以换一个读取函数，而不用改查询本身。
    """

    def read(path: str, tenant: str):
        return cpms_get(base + path, bearer, tenant, timeout=3)

    return read


def site_page(scope: SiteScope, body, page: int, *, outside_scope="reject"):
    """校验站点接口的**一页**并抽出可用的行。

    与 :func:`query_sites` 是同一份判据：信封、分页回显、逐行租户与站点集合、字段形状。
    抽出来的理由是 C 端工具只要一页（它按声明的固定分页取），而它必须与整份查询用
    同一条核对 —— 否则「同一个接口两套事实」只是时间问题。
    """
    data = body.get("data") if isinstance(body, dict) else None
    if (
        not isinstance(body, dict)
        or type(body.get("code")) is not int
        or body["code"] != 0
        or not isinstance(data, dict)
        or not isinstance(data.get("records"), list)
        or len(data["records"]) > SITE_PAGE_SIZE
        or type(data.get("total")) is not int
        or data["total"] < 0
        or data.get("current") != page
        or type(data.get("current")) is not int
        or data.get("size") != SITE_PAGE_SIZE
    ):
        raise OperatorRefused("CPMS_INVALID_RESPONSE")
    rows: list[dict[str, str]] = []
    for item in data["records"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or str(item.get("tenantId") or "") != scope.tenant_id
        ):
            raise OperatorRefused("CPMS_SCOPE_MISMATCH")
        if scope.site_ids is not None and str(item["id"]) not in scope.site_ids:
            if outside_scope == "skip":
                continue
            raise OperatorRefused("CPMS_SCOPE_MISMATCH")
        selected = {key: str(item.get(key) or "") for key in ("id", "name", "address")}
        if (
            not isinstance(item.get("name"), str)
            or not isinstance(item.get("address"), (str, type(None)))
            or not selected["name"]
            or any(len(v) > 1000 for v in selected.values())
        ):
            raise OperatorRefused("CPMS_INVALID_RESPONSE")
        rows.append(selected)
    return rows, data["total"]


def query_sites(scope: SiteScope, keywords: SiteKeywords, read, reauthorize, *, outside_scope="reject"):
    """按显式范围与关键词读一次站点。

    三段逻辑在这里合流：范围判定（含逐页复核）、CPMS 读取、结果对齐与计数口径。
    对话回合、受控工具与只读接口都调这一处 —— 站点事实只能有一个来源，否则
    「换个入口换一套事实」就只是时间问题。

    ``read(path, tenant)`` 是传输，``reauthorize() -> fingerprint`` 是「读取期间撤权
    就停止后续交付」那条判据；两者都由调用方提供。空范围不发任何 CPMS 请求。

    ``outside_scope`` 说明「上游返回了一条不在可见范围里的站点」该怎么处理，两个入口
    的答案不同且都不该被对方沿用：

    * ``reject``（管家）—— 上游请求已经带上站点集合，再出现范围外的行说明上游没有按
      约定收窄，那是一处**违约**，整份响应不可信。
    * ``skip``（C 端）—— 上游按租户返回，范围外的行是**预期内**的，本地过滤即可；
      把它当违约会让「本人只有一个站点」的用户永远查不到自己的站点。

    租户不符在两种模式下都是硬拒绝：那不是范围宽窄问题，是数据归属错误。
    """
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    limited = False
    total = 0
    if scope.site_ids != ():
        for page in range(1, SITE_MAX_PAGES + 1):
            if reauthorize() != scope.fingerprint:
                raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
            body = read(SITE_PATH + "?" + urlencode(keywords.params(page)), scope.tenant_id)
            page_rows, page_total = site_page(scope, body, page, outside_scope=outside_scope)
            for selected in page_rows:
                if selected["id"] not in seen and keywords.matches(selected):
                    seen.add(selected["id"])
                    rows.append(selected)
            data = body["data"]
            more = page * SITE_PAGE_SIZE < data["total"]
            total = max(total, data["total"])
            if len(rows) >= SITE_MAX_SHOWN or not more or not data["records"]:
                limited = more or len(rows) > SITE_MAX_SHOWN
                break
            limited = more
    if reauthorize() != scope.fingerprint:
        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
    return {
        "source": "cpms.sitePage",
        "observed_at": datetime.now(UTC).isoformat(),
        "sites": rows[:SITE_MAX_SHOWN],
        "limited": limited,
        "empty_scope": scope.site_ids == (),
        # 列举问法要回答"我有多少个"，只给前 10 条等于没回答。``total`` 是 CPMS 对
        # 这次查询自己的计数；``scope_limited`` 说明它是否还经过站点集合过滤 ——
        # 员工账号的 total 是租户口径，不能直接当成"你有多少个"。
        "total": total,
        "scope_limited": scope.site_ids is not None,
    }
