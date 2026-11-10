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
from urllib.parse import urlencode

from aiops_diagnostics.common.async_job_lifecycle import QUESTION
from aiops_diagnostics.common.bounded_http import (
    ErrorMapping,
    RequestSpec,
    json_body,
    parse_code_data_envelope,
    request_json,
)
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.data.sources import GUN_COLUMN_NAMES, SourceError
from aiops_diagnostics.execution import chat_execution, execution_manifest
from aiops_diagnostics.execution.agent_runs import (
    AgentRunConfig,
    AgentRunDispatchError,
    SnailDispatcher,
)
from aiops_diagnostics.execution.run_deliverability import deliverable, expired
from aiops_diagnostics.identity.order_identity import is_order_identifier
from aiops_diagnostics.identity.query_scope import resolve_query_scope
from aiops_diagnostics.identity.scope_context import is_tenant_level_account
from aiops_diagnostics.operator import (
    operator_agent,
    operator_answer_review,
    operator_conversation,
    operator_tools,
    order_pricing,
)
from aiops_diagnostics.operator.operator_conversation import OperatorRefused

SCENARIO = "operator_order_diagnosis"
PREFIX = "operator-cpms-node:"
#: 工具名、路径、输入与证据字段全部来自 `operator_tools.json` —— 仓内唯一声明。
TOOL_NAMES = operator_tools.names()

# 同一run最多一次有界重派，复用其已经读取的证据。
MAX_RESAMPLES = 1

NODE_FAILURE_CODES = {
    "model_quota_exhausted": "OPERATOR_MODEL_QUOTA_EXHAUSTED",
    "model_failed": "OPERATOR_DEPENDENCY_UNAVAILABLE",
    "node_failed": "OPERATOR_DEPENDENCY_UNAVAILABLE",
    "model_stream_incomplete": "OPERATOR_MODEL_OUTPUT_UNSTABLE",
}

#: 节点上报的工具拒绝 reason 前缀（原始拒绝码跟在冒号后）。
TOOL_REFUSED_PREFIX = "tool_refused:"

#: **可以回喂模型的业务拒绝**——模型拿到结果后能自己修正参数、先补依赖，
#: 或如实向用户说明「查无此单/没有权限/缺少条件」。这类拒绝在响应体里带
#: `feedback` 标记与一段提示，新节点把它当工具结果交给模型而不是终结整轮。
#:
#: 不在这张表里的拒绝保持原样：授权/发布/节点完整性、并发抢占、运行已终结——
#: 那些不是「换个参数再来一次」能解决的，回喂只会让模型在死局上空转。
#: 提示语写给模型看，措辞必须导向用户可理解的结局，不得带出内部实现。
FEEDBACK_REFUSALS = {
    "OPERATOR_OFFLINE_UNAVAILABLE": (
        "离线资料暂不可用或范围过大，请缩小范围或联系管理员；不能据此判断没有离线。"
    ),
    "OPERATOR_ORDER_REQUIRED": "缺少订单号；请向用户确认要查询的订单后再调用。",
    "OPERATOR_ORDER_NOT_REFERENCED": "只能查询用户本句提到或已选定的订单；请向用户确认订单号。",
    "OPERATOR_ORDER_CONFLICT": "每轮只能查询一笔订单；与当前订单不同请向用户说明不能同时查询。",
    "OPERATOR_TOOL_ARGUMENTS_INVALID": "参数未通过校验；参数值必须来自用户原话，不能猜测。",
    "OPERATOR_TOOL_ARGUMENTS_CHANGED": "同一工具本轮只能使用同一组参数；请沿用之前的参数。",
    "OPERATOR_ANALYSIS_INVALID": "该工具不接受参数；需先取得订单证据后再调用。",
    "OPERATOR_DETAIL_REQUIRED": "需先调用 cpms_order_detail 读取订单详情。",
    "OPERATOR_TARIFF_REQUIRED": "需先调用 cpms_order_tariff 读取计费模板。",
    "OPERATOR_EVIDENCE_MISSING": "需先取得该工具依赖的订单证据再调用。",
    "OPERATOR_SITE_SCOPE_REQUIRED": "当前账号没有可见站点；请如实告知用户。",
    "CPMS_INVALID_RESPONSE": "上游返回的数据无法识别；如实告知暂时无法查询，不要编造。",
    "CPMS_SCOPE_MISMATCH": "该订单不在当前账号可见范围或不存在；如实告知查无此单或无权查看。",
}

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
OBJECT_CONTRACT = "request-target/2"
TOOL_CONTRACT_HEADER = "X-AI-Ops-Tool-Contract"

#: 证据 ``data`` 里的派生形状字段：网关投影时一处算定，节点回报时只能**重算同一公式**——
#: 两份实现一旦口径漂移（上游 total / 过滤后行数 / 展示上限哪个是分母），每一次真实运行都
#: 以 ``OPERATOR_EVIDENCE_MISMATCH`` 失败。逐字比对只认出处（上游原值与行内容）；派生字段
#: 由网关自己的投影兜底——快照存的本来就是 ``task.evidence`` 里网关那份，不是节点回报值。
DERIVED_EVIDENCE_FIELDS = frozenset(
    {
        "absence",
        "complete_timeline",
        "current",
        "ordering_guaranteed",
        "sample_limit",
        "samples_returned",
        "size",
        "truncated",
    }
)


def _provenance(data):
    """证据 ``data`` 的出处投影：剥掉派生形状字段，只剩可逐字核验的上游原值。"""
    if not isinstance(data, dict):
        return data
    return {key: value for key, value in data.items() if key not in DERIVED_EVIDENCE_FIELDS}


#: 每个工具这一次要发的请求。**路径在这里物化**，节点只执行拿到的那个请求 ——
#: 这正是「新增一条只读路径不必改 Java」的那一步。
def _declared(tool: str):
    """取声明；未声明的工具名仍然被拒绝，只是判据从常量变成了数据。"""
    try:
        return operator_tools.tool(tool)
    except operator_tools.ToolDeclarationError as exc:
        raise OperatorRefused("OPERATOR_TOOL_DISABLED") from exc


def tool_request(tool: str, values: dict[str, str | list[str]]) -> dict[str, str]:
    """按工具名把这一次的请求目标物化出来。**这里是路径与查询串物化的唯一位置。**"""
    return _declared(tool).request(values)


def tool_inputs(tool: str, values: dict[str, str | list[str]]) -> dict[str, str]:
    """这一次运行的输入。订单号是**输入**，不再是授权载荷的中心。"""
    return _declared(tool).declared_inputs(values)


def run_values(
    tool: str, order_no: str, arguments: dict[str, str | list[str]] | None = None
) -> dict[str, str | list[str]]:
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
    if operator_agent.is_agent(run):
        return tool in TOOL_NAMES
    context = "order" if diagnoses_an_order(run) else "conversation"
    return context in _declared(tool).applies_to or (
        context == "conversation" and "operator_conversation" in _declared(tool).applies_to
    )


#: 工具参数的字符纪律（ADR-0176）。与对话协议的搜索词同源：
#: 长度 1–80、不含 `%_\` 与换行。**另一半**——「必须是提问原文的子串」——只对模型提议的
#: 参数成立，因此在 :func:`validate_tool_arguments` 里单独判。
_ARGUMENT_FORBIDDEN = ("%", "_", "\\", "\r", "\n")


def validate_tool_arguments(tool: str, arguments, question: str) -> dict[str, str | list[str]]:
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
    cleaned: dict[str, str | list[str]] = {}
    for key, value in arguments.items():
        if tool == SITE_TOOL and key == "address_terms":
            if not isinstance(value, list) or len(value) > 4:
                raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
            terms = []
            for term in value:
                if (
                    not isinstance(term, str)
                    or not term.strip()
                    or len(term) > 80
                    or any(char in term for char in _ARGUMENT_FORBIDDEN)
                ):
                    raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
                original = operator_conversation.question_span(term, question)
                if not original:
                    raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
                if original not in terms:
                    terms.append(original)
            if sum(map(len, terms)) > 80:
                raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
            cleaned[key] = terms
            continue
        if not isinstance(value, str):
            raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
        forbidden = tuple(
            c
            for c in _ARGUMENT_FORBIDDEN
            if not (tool == "cpms_station_plan" and key == "timezone" and c == "_")
        )
        if len(value) > 80 or any(char in value for char in forbidden):
            raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
        stripped = value.strip()
        if not stripped:
            continue
        original = operator_conversation.question_span(stripped, question)
        if not original:
            raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
        if tool == "cpms_station_plan" and key in {"energy", "power"}:
            units = r"(?:kwh|千瓦时|度)" if key == "energy" else r"(?:kw(?!h)|千瓦(?!时))"
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", original) or not re.search(
                r"(?<![0-9.])" + re.escape(original) + r"\s*" + units, question, re.I
            ):
                raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
        cleaned[key] = original
    if tool == SITE_TOOL and cleaned.get("address") and "address_terms" in cleaned:
        raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
    return cleaned


#: 站点查询工具的名字。这里是**唯一**按名字认它的地方：判断「这一轮是否已经用工具
#: 取过站点」是运行级的事实，不是工具自身的属性。
SITE_TOOL = "cpms_site_search"
TIMELINE_TOOL = "cpms_charging_timeline"
#: 节点 object 投影的数组上限（`CpmsReadTool.project` 拒收 >64）。窗口内按 _ts 升序
#: 取前 N 行，装不下的部分由 ``truncated``/``total`` 如实标出 —— 不翻页。
TIMELINE_LIMIT = 64
#: 发给 tsdata `QueryRequest.fields` 的列清单：SOP §2.2 那一份，`GUN_COLUMN_NAMES`
#: 是仓内唯一权威（诊断直查与只读代理白名单也由它派生），前面补上 `_ts`。
#: 实测例外（41 生产回放，2026-11-05）：tsdata 门面的 `charging-gun_property`
#: 没有 `batteryMinTemperature` 列（充电生产 TDengine 的同名表有），整表原样
#: 发会被门面按列名拒（TDengine error: Invalid column name）。该列在行投影里
#: 本来就可缺席（picked 跳过缺键、节点 project 跳过缺键），剔除不退化证据。
TIMELINE_FIELDS = ("_ts", *(name for name in GUN_COLUMN_NAMES if name != "batteryMinTemperature"))
_TIMELINE_DEVICE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TIMELINE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?")


def _proxy_tool(task, run, method, path, query):
    """白名单命中的工具名；查无对应授权目标时返回 ``None``（即 PATH_REJECTED 的前置事实）。"""
    return next(
        (
            name
            for name in TOOL_NAMES
            if name in task.arguments
            and allowed_in(name, run)
            and tool_request(name, run_values(name, run.order_no, task.arguments.get(name)))
            == {"method": method, "path": path, "query": query}
        ),
        None,
    )


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
        current_text = binding.manifest_text
        if not operator_agent.is_agent(run) and not diagnoses_an_order(run):
            return binding.route_revision == run.route_revision
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


def diagnoses_an_order(run) -> bool:
    """统一循环取证后才绑定对象；旧运行按原类型回读。"""
    return bool(run.order_no) if operator_agent.is_agent(run) else run.operation == SCENARIO


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
    selected_order: str = ""
    active_order: str = ""
    messages: list = field(default_factory=list)
    history_runs: list = field(default_factory=list)
    question: str = ""
    language: str = "zh"
    trial: bool = False
    #: 本次运行可接受答复未完成时最多重派一次。
    #: 放在任务上而不是模块上：它属于这一次 run，跨 run 复用就没有"有界"可言了。
    resamples: int = 0
    #: 本轮已回喂模型的业务拒绝（`FEEDBACK_REFUSALS` 里的码）。答复复核据此区分
    #: 「查无此单」与「编造」——被拒的调用不产生证据，模型只能凭这条记录解释结局。
    refusals: list = field(default_factory=list)
    lock: object = field(default_factory=threading.RLock)
    completion_lock: object = field(default_factory=threading.Lock)
    started_at: float = field(default_factory=time.monotonic)
    trace_failed: bool = False
    finished_elapsed_ms: int | None = None


class OperatorCpms:
    def __init__(self, context):
        self.context = context
        self.tasks = {}
        self.lock = threading.RLock()

    def _trace(self, run_id, phase, status="running", *, tool=None, duration_ms=None):
        task = self.tasks.get(run_id)
        if task is None or task.trace_failed:
            return
        with task.lock:
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
            try:
                self.context.agent_run_store.record_trace(run_id, event)
            except sqlite3.Error:
                # 观测失败不能打断业务收尾，尤其不能留下永久 running 的问题。
                task.trace_failed = True
                logging.getLogger("aiops.operator").warning("operator_trace_unavailable")

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
        if run.state == "expired" and qa is not None and qa["status"] in {"queued", "running"}:
            self.context.store.update_assistant_question(qa_id, status="expired")
            qa.update(status="expired", result=None)
        terminal = run.state not in {"issued", "running"}
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
                lambda: self.refresh_scope(caller, allow_empty=True),
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
    ):
        conversation_turn = registration.conversation_turn
        if order and not is_order_identifier(order):
            raise OperatorRefused("ORDER_INVALID")
        operator_scope(caller, allow_empty=not order)
        current = authorize()
        if current.scope_fingerprint != caller.scope_fingerprint:
            raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
        from aiops_diagnostics.gateway.gateway_config import canonical_gateway_url

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
        for key in list(self.tasks):
            run = self.context.agent_run_store.get(key)
            if run is None or expired(run) or run.state in {"failed", "revoked", "expired"}:
                del self.tasks[key]

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
            return result
        except OperatorRefused as exc:
            if task is not None:
                run = self.context.agent_run_store.get(run_id)
                tool = _proxy_tool(task, run, method, path, query) if run else None
                self._note_refusal(task, tool, exc)
                if tool and secrets.compare_digest(task.grant, grant):
                    self._trace(
                        run_id, "tool", "refused", tool=tool, duration_ms=(time.monotonic() - started) * 1000
                    )
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
                )
                and TOOL_NAMES[0] not in task.delivered
            ):
                raise OperatorRefused("OPERATOR_DETAIL_REQUIRED")
            if tool == "cpms_order_billing" and "cpms_order_tariff" not in task.delivered:
                raise OperatorRefused("OPERATOR_TARIFF_REQUIRED")

            if tool in {"cpms_order_amount_check", "cpms_order_process_stats"}:
                required = {"cpms_order_detail"} | (
                    {"cpms_order_tariff", "cpms_order_billing"}
                    if tool == "cpms_order_amount_check"
                    else {"cpms_charging_samples"}
                )
                if not required <= task.delivered:
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
        if payload["status"] == "failed":
            # 失败上报永远照录：节点给的原因码进运行记录，不再统一糊成 `operator_node_failed`。
            # 它只用于**归因**，不改变任何判决：这条 run 仍然失败。
            reason = payload.get("reason")
            if not isinstance(reason, str) or not (
                reason in NODE_FAILURE_CODES
                or re.fullmatch(TOOL_REFUSED_PREFIX + r"[A-Za-z0-9_]{1,80}", reason)
            ):
                reason = "node_failed"
            store.fail(run_id, "operator_node_failed: " + reason)
            logging.getLogger("aiops.operator").warning(
                "operator_node_reported_failure reason=%s", reason or "-"
            )
            return {"run_id": run_id, "state": "failed"}
        raw = payload.get("answer", "")
        if not isinstance(raw, str) or not 0 < len(raw.strip()) <= 12000:
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

            if contains_secret(answer, (task.bearer, task.grant)):
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
            result = operator_agent.render(answer, evidence, task.language)

        history_evidence = []
        for previous_id in task.history_runs:
            previous = store.peek(previous_id)
            if previous is None or not previous.snapshot:
                raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
            self._authorize_snapshot(task, previous)
            history_evidence.append(previous.snapshot.get("evidence", {}))
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
            "succeeded" if review["verdict"] == "allow" else "refused",
            duration_ms=(time.monotonic() - review_started) * 1000,
        )
        if review["verdict"] != "allow":
            raise OperatorRefused("OPERATOR_ANSWER_NOT_GROUNDED")
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
            except OperatorRefused as exc:
                self._stamp_gate_refusal(run_id, str(exc))
                latest = store.get(run_id)
                if latest is None or latest.state in {"issued", "running"}:
                    raise
                return self._absorb_superseded(run_id, superseded)
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
            snapshot = {
                "answer": answer,
                "trial": task.trial,
                "evidence": evidence,
                "language": task.language,
                "answer_review": review,
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
            self._trace(run_id, "completed", "succeeded")
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
            dispatcher.deadline_at = self.context.agent_run_store.get(run_id).expires_at
            task = self.tasks[run_id]
            task.messages = self._messages(task)
            dispatcher.question = task.question
            # 已有取证固定在本run，最多一次重派，持续失败明确终止。
            while True:
                try:
                    self._trace(run_id, "dispatch")
                    dispatcher.dispatch(config, run_id)
                except AgentRunDispatchError:
                    self._raise_node_failure(run_id)
                    if not self._untouched(run_id, task):
                        raise
                    if task.resamples >= MAX_RESAMPLES:
                        raise
                    task.resamples += 1
                    self._trace(run_id, "resample")
                    logging.getLogger("aiops.operator").warning(
                        "operator_resample attempt=%d reason=dispatch_refused", task.resamples
                    )
                    continue
                self._raise_node_failure(run_id)
                if not self._untouched(run_id, task):
                    break
                # 派发本身成功、回调也走完了，但 run 没被推进 —— 说明 `_complete` 拒了答案。
                if task.resamples >= MAX_RESAMPLES:
                    raise OperatorRefused("OPERATOR_MODEL_OUTPUT_UNSTABLE")
                task.resamples += 1
                self._trace(run_id, "resample")
                logging.getLogger("aiops.operator").warning(
                    "operator_resample attempt=%d reason=no_answer_accepted", task.resamples
                )
            task = self.tasks[run_id]
            current = task.authorize()
            result = self.authorize_readback(current, qa_id, task.bearer)
            if store.update_assistant_question(qa_id, status="completed", result=result):
                self.context.runtime._complete_conversation_turn(
                    task.conversation_turn,
                    task.question,
                    result,
                    status_qa_id=qa_id,
                    active_order_no=self.context.agent_run_store.get(run_id).order_no or None,
                )
        except Exception as exc:  # noqa: BLE001 - 后台失败必须收敛，凭据及上游正文不进日志。
            self._trace(run_id, "failed", "failed")
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


def read_site_tool(scope, tool, arguments, read, reauthorize, *, outside_scope="reject"):
    keywords = tool_site_keywords(arguments)
    rows, total, _ = read_site_pages(
        scope,
        keywords,
        read,
        reauthorize,
        outside_scope=outside_scope,
        max_pages=1,
    )
    body = {
        "code": 0,
        "data": {
            "records": [dict(row, tenantId=scope.tenant_id) for row in rows[:SITE_PAGE_SIZE]],
            "total": total,
            "current": 1,
            "size": SITE_PAGE_SIZE,
        },
    }
    return site_tool_result(scope, body, tool, arguments, outside_scope=outside_scope)


def tool_site_keywords(arguments):
    terms = arguments.get("address_terms")
    if terms is None:
        terms = [arguments["address"]] if arguments.get("address") else []
    return SiteKeywords(arguments.get("name", ""), tuple(terms))


def site_tool_result(scope, body, tool, arguments, *, outside_scope="reject"):
    """证据只含声明字段；节点取数仍需分页回显和已核对的租户身份。"""
    rows, total = site_page(scope, body, 1, outside_scope=outside_scope)
    keywords = tool_site_keywords(arguments)
    rows = [row for row in rows if keywords.matches(row)]
    declared = operator_tools.tool(tool)
    data = {
        "records": [{key: row[key] for key in declared.evidence_fields} for row in rows],
        "total": total,
        "sample_limit": declared.page_size,
        # ``truncated`` 是**证据**字段：页面没有覆盖上游的全部行。它与「展示只列 10 条」
        # 是两件事，不能合并 —— Java 节点按 ``total > rows.size()`` 逐字重算这一项并与
        # 网关比对，改口径会让每一次真实运行都以 OPERATOR_EVIDENCE_MISMATCH 失败。
        # 展示上限在工具投影中收口，不是模型意图分类。
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
        "observed_at": format_stored_time(now_utc()),
        "data": selected,
    }


def _timeline_time(value) -> str | None:
    """CPMS 时间（``yyyy-MM-dd HH:mm:ss`` 或 ISO）归一成 tsdata 接受的本地时间串。"""
    if not isinstance(value, str) or not _TIMELINE_TIME.fullmatch(value.strip()):
        return None
    return value.strip().replace("T", " ")


def _failure_code(exc: BaseException) -> str:
    """这次失败对**用户**报哪个码。

    此前一律 `OPERATOR_DIAGNOSIS_FAILED`，而那个码同时覆盖「模型不吐 JSON」「上游没钱」
    「答案不合格」「节点掉线」—— 值班的人分不出该找谁。这里只把**已经能区分**的几类
    分开，其余保持原码：宁可不分，不能猜错。
    """
    if isinstance(exc, OperatorRefused):
        code = str(exc)
        if code in NODE_FAILURE_CODES.values() or code == "OPERATOR_HISTORY_LIMIT":
            return code
        # 这些原因说明**上游/环境**有问题，不是这次回答不合格 —— 分开报。
        if code in {
            "OPERATOR_NODE_REJECTED",
            "OPERATOR_NODE_CHANGED",
            "OPERATOR_TARGET_MISSING",
            "CPMS_UNAVAILABLE",
            "TSDATA_UNAVAILABLE",
            "OPERATOR_SCOPE_UNAVAILABLE",
        }:
            return "OPERATOR_DEPENDENCY_UNAVAILABLE"
        if code in {"OPERATOR_RESULT_UNSTABLE", "OPERATOR_ANSWER_NOT_GROUNDED"}:
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


def tsdata_query(url, payload, *, timeout=10):
    """POST tsdata ``QueryRequest``；一切失败形状都归一成依赖不可用，不落成空页。"""

    def unavailable(_):
        return OperatorRefused("TSDATA_UNAVAILABLE")

    return request_json(
        RequestSpec(
            url=url,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            body=json_body(payload),
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
        envelope=parse_code_data_envelope,
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

    地址词在取证前确定，最多4条。CPMS每次只收一个子串，查询器逐词读取再去重。
    旧address字符串作为一条字面关键词，不在这里猜测地理边界。
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

    def params(self, page: int, address: str | None = None) -> dict[str, object]:
        """这一页的 CPMS 查询参数。

        ``address`` 由调用方逐词给（见 :meth:`address_queries`）：上游的 ``address``
        是**子串匹配**，把「广州天河」整串传过去一条都匹配不到 —— 没有任何一条地址
        同时包含这两个相连的字。
        """
        params: dict[str, object] = {"current": page, "size": SITE_PAGE_SIZE, "type": 0}
        if self.name:
            params["name"] = self.name
        chosen = self.address if address is None else address
        if chosen:
            params["address"] = chosen
        return params

    def address_queries(self) -> tuple[str, ...]:
        """这一次要**分别**发给上游的地址词。空元组表示不按地址过滤。

        2026-11-04 实测（41 生产）：

        * ``address=广州天河`` → ``total: 0``（上游子串匹配，整串无人命中）
        * ``address=天河`` → 命中 ``HuaJing Station``

        用户说「广州天河」指的是**上下级地名叠在一起**，不是一个字面串。所以按词
        分别查询再合并 —— 这正是用户脑子里"这一带"的意思。
        """
        return tuple(term for term in self.address_terms if term.strip())

    def matches(self, site: dict[str, str]) -> bool:
        """这一行是不是在用户说的那个地点。

        地址词之间是 ``any`` 而不是 ``all``（2026-11-04 修正）。用户说「广州天河」指的是
        **广州市天河区这一带**，不是「同时出现『广州』与『天河』两个字面串」。用 ``all``
        的实际后果是：HuaJing Station 的地址写作
        ``… 天河公园 Tian He Qu, Guang Zhou Shi …`` ——「天河」命中、「广州」命不中
        （地址里是拼音 ``Guang Zhou``），于是 ``all`` 一票否决，**一条真实存在的站点被
        自己的过滤器丢掉了**，上游返回的总数却是对的。

        站名仍然要求**全部命中**（它通常只有一个词，``all``/``any`` 等价；真给了多个词时
        「都要出现」才是对的）。地点词放宽成 ``any`` 与「扩大范围」的产品直觉一致：
        多给一个地名只会查得更宽，不会把一个真实结果筛掉。
        """
        haystack = (site["name"] + " " + site["address"]).casefold()
        if self.name.casefold() not in site["name"].casefold():
            return False
        if not self.address_terms:
            return True
        return any(term.casefold() in haystack for term in self.address_terms)


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


def read_site_pages(scope, keywords, read, reauthorize, *, outside_scope="reject", max_pages=SITE_MAX_PAGES):
    """所有入口共用取数与逐页授权；节点保持每词一页，直接查询最多每词四页。"""
    rows = []
    seen = set()
    totals = []
    limited = False
    terms = keywords.address_queries() or (None,)
    if len(terms) > 4:
        raise OperatorRefused("OPERATOR_INVALID_SEARCH")
    if scope.site_ids != ():
        for term in terms:
            total = 0
            for page in range(1, max_pages + 1):
                if reauthorize() != scope.fingerprint:
                    raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
                body = read(SITE_PATH + "?" + urlencode(keywords.params(page, address=term)), scope.tenant_id)
                page_rows, total = site_page(scope, body, page, outside_scope=outside_scope)
                for row in page_rows:
                    if row["id"] not in seen and keywords.matches(row):
                        seen.add(row["id"])
                        rows.append(row)
                more = page * SITE_PAGE_SIZE < total
                if not more or not body["data"]["records"]:
                    limited = limited or more
                    break
                if page == max_pages:
                    limited = True
            totals.append(total)
    if reauthorize() != scope.fingerprint:
        raise OperatorRefused("OPERATOR_SCOPE_CHANGED")
    # 多查询计数只在全读完时等于去重结果；否则保留上界并标记未穷尽。
    total = sum(totals) if limited else len(rows)
    if len(terms) == 1:
        total = max(totals, default=0)
    return rows, total, limited


def query_sites(scope: SiteScope, keywords: SiteKeywords, read, reauthorize, *, outside_scope="reject"):
    rows, total, limited = read_site_pages(scope, keywords, read, reauthorize, outside_scope=outside_scope)
    return {
        "source": "cpms.sitePage",
        "observed_at": format_stored_time(now_utc()),
        "sites": rows[:SITE_MAX_SHOWN],
        "limited": limited or len(rows) > SITE_MAX_SHOWN,
        "empty_scope": scope.site_ids == (),
        "total": total,
        "scope_limited": scope.site_ids is not None,
    }
