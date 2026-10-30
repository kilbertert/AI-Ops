"""把「本人订单状态」交给客户执行节点跑，并把它接进**现有 App 聊天**。

## 为什么需要这一层

`POST /v1/orders/{order_no}/charging-status/delegated` 已经能把一次读取交给节点，但它
是**同步**的：调用方一直等到平台派发、节点跑完整轮、结果回到网关。这条路对「App 聊天」
不成立 —— 用户的 POST 会挂住整轮节点时间，前端早就超时了。

而 `_consumer_order_chat` 今天是**确定性直读**：网关自己读 SQL + CPMS 并当场作答，
与节点无关。

本模块把两件事接起来，同时**不复制第二套逻辑**：

* `select_binding` / `issue_for_store` / `dispatch_run` / `settled_run` / `final_release`
  是**从既有委派入口抽出来的共享操作**。两个入口都调它，授权、TTL、执行配置守卫、
  完成判据、最终放行只有一份实现。
* 聊天入口只多做一件事：**先建 QA、立刻返回**，把「等节点」搬到既有 QA worker 里。

## 不变量（本模块存在的理由）

1. **按租户显式绑定，默认关闭**。没有绑定文件、或调用者的租户没有登记在这一条
   （环境/入口/场景）上时，聊天入口走**原分支**，行为与接线前逐字一致。
   「全局配了一个文件」**不是**绑定：那等于任何能通过本人授权的租户都派到同一个节点。
2. **命中绑定后失败就是失败**。节点失败、超时、平台不可用、配置漂移、执行配置未被核对、
   清单缺失 —— 全部落到 QA 的 `failed` 终态。**不回落直读、不回落旧模型、不换别的节点。**
   回落到任何一条都在把「另一次读取」冒充成本次结果。
3. **答案只来自同一条 run 的 succeeded 快照**，并且必须过与委派入口**同一份**
   `final_release`：同主体、同订单、同环境、同 app/节点、节点此刻仍有效、
   执行配置核对一致、未过期、快照自洽。不另读 CPMS 去「补」一个快照。
4. **过期、撤权、停用、取消都拒绝正文**。TTL 由 `agent_runs` 自己管，不因为轮询而延长；
   取消先落终态并撤销已关联的 run。停用之后**无正文**的终态仍可按原协议读到 ——
   用户至少要知道自己那条是取消成功还是失败。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiops_diagnostics.common.private_files import PrivatePathError, validate_private_file
from aiops_diagnostics.data.order_status import KNOWN_STATUS_CODES, StatusRole, role_of
from aiops_diagnostics.execution.agent_runs import (
    SUCCEEDED,
    AgentRun,
    AgentRunConfig,
    AgentRunConfigError,
    AgentRunDispatchError,
    SnailDispatcher,
)
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.identity.scope_context import ScopeContext

#: QA 的 ``internal_run_id`` 前缀。它**只是**给人看的标签；
#: 「哪条 run 承载哪个 QA」由 `agent_runs.qa_id` 这一列决定，不从字符串反推。
CHAT_RUN_PREFIX = "consumer-order-chat-node:"

#: Dify 路线（APP-RUNTIME-02）的 QA 前缀。两条路线各有自己的前缀，因为它们的读取判据
#: 完全不同 —— 一个看 `agent_runs`，一个看 `chat_dify_runs`。
DIFY_RUN_PREFIX = "consumer-order-chat-dify:"

#: 绑定表的 schema。不匹配就拒绝整份文件，不做兼容读取。
BINDINGS_SCHEMA = "aiops-chat-execution/1"

#: 本片唯一实现的场景。别的取值在**加载时**拒绝，而不是静默当成本场景：
#: 一份写错场景名的绑定若被当成本场景，就是「配了一个不生效的开关」。
SCENARIO_OWN_ORDER_STATUS = "consumer_own_order_status"
SCENARIO_CONSUMER_SERVICE = "consumer_service"
SCENARIO_OPERATOR_DIAGNOSIS = "operator_order_diagnosis"
OPERATOR_TOOL_NAMES = ("cpms_order_detail", "cpms_charging_samples")

#: 唯一允许的入口。operator/代查不走这条路线（它们的授权判据完全不同）。
ENTRY_CONSUMER = "consumer"

#: 答案渲染支持的语言。回读一致性校验按这一组重算（见 `answer_matches_snapshot`）。
ANSWER_LANGUAGES = ("zh", "zh-Hant", "en")

#: 快照必须**恰好**是这五个字段：多一个字段意味着有人往里塞了别的来源。
SNAPSHOT_FIELDS = frozenset({"order_no", "status_code", "status_role", "observed_at", "source"})

#: 绑定文件的上限。它是运维手写的登记表，不是数据集。
MAX_BINDINGS_BYTES = 262144

#: 这条登记把请求交给**哪个引擎**。`node` 是已交付的客户执行节点路线，`dify` 是
#: APP-RUNTIME-02 的 Workflow 路线。**一条登记只能指向一个引擎** —— 没有隐式优先级，
#: 也不存在"节点失败就试 Dify"（ADR-0079：一次请求只有一个模型循环，无故障自动跨路线回落）。
ENGINE_NODE = "node"
ENGINE_DIFY = "dify"
ENGINES = (ENGINE_NODE, ENGINE_DIFY)


class ChatBindingError(ValueError):
    """绑定表缺失、不可读、权限不对或不自洽。调用方把它变成**启动失败**。"""


class ChatRunUnavailable(RuntimeError):
    """本场景不能走节点，或节点的结果此刻不能交付。

    ``code`` 是回给调用方的稳定错误码，``http_status`` 是它在 HTTP 面的形状。
    与 `AgentRunDispatchError` 的区别：后者是「平台那一跳失败」，这个是「这一条请求
    不该/不能交给节点」或「这条结果不能再交出去」。两者在聊天入口都**落到失败终态**，
    不回落。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 503,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ChatBinding:
    """一条可信服务端登记：这个（环境，租户，入口，场景）交给这个节点跑。

    只由服务端配置决定。客户端、模型、请求体都不能影响其中任何一项 ——
    它们连「有没有这条绑定」都看不见。
    """

    environment: str
    tenant_id: str
    entry: str
    scenario: str
    #: 节点引擎用；Dify 登记留空。
    config_file: str = ""
    manifest_file: str = ""
    #: `node` 或 `dify`。旧登记没有这个字段 ⇒ 按 `node` 读（兼容既有部署）。
    engine: str = ENGINE_NODE
    #: 仅 `dify` 使用：已发布 Workflow 的固定身份与它的服务 API 凭据文件。
    #: 两者成对出现，缺一即整份绑定表校验失败 —— 半配就是「配了一半就开门」。
    workflow_id: str = ""
    workflow_key_file: str = ""
    #: **只有管理行**（`chat_route_bindings`）才有：它由哪一版 AgentVersion 发布而来。
    #: 文件行恒为 0。回读守卫拿它核"这条绑定仍指向签发时那一版"。
    agent_version_no: int = 0
    agent_id: str = ""
    app_id: str = ""
    route_revision: int = 0
    trial_app_id: str = ""
    manifest_text: str = ""
    content_file: str = ""
    content_snapshot: dict | None = None
    knowledge_base_ids: tuple[str, ...] = ()
    live_order_status: bool = False
    live_billing_record: bool = False
    photo_enabled: bool = False
    callback_version: int = 1

    @property
    def key(self) -> str:
        """写进 run 记录的那把钥匙。回读与回调都按它找回**签发时那一条**绑定。"""
        if self.trial_app_id:
            return "operator-trial:" + self.trial_app_id
        return f"{self.scenario}:{self.environment}:{self.tenant_id}"


# --------------------------------------------------------------------------
# 绑定表：按租户登记，启动时整表校验
# --------------------------------------------------------------------------


def load_bindings(path: str | Path | None) -> tuple[ChatBinding, ...]:
    """读整张绑定表并**逐条**校验。任何一条不合格 ⇒ 整份拒绝（不降级、不跳过）。"""
    if not path:
        return ()
    declared = Path(str(path)).expanduser().absolute()
    try:
        validate_private_file(declared)
        if declared.stat().st_size > MAX_BINDINGS_BYTES:
            raise ChatBindingError("chat execution bindings are too large")
        raw = json.loads(declared.read_text(encoding="utf-8"))
    except (OSError, ValueError, PrivatePathError) as exc:
        raise ChatBindingError("chat execution bindings are unavailable") from exc
    if not isinstance(raw, dict) or raw.get("schema") != BINDINGS_SCHEMA:
        raise ChatBindingError("unsupported chat execution bindings schema")
    rows = raw.get("bindings")
    if not isinstance(rows, list) or not rows:
        raise ChatBindingError("chat execution bindings declare no entries")
    bindings: list[ChatBinding] = []
    seen: set[str] = set()
    for row in rows:
        binding = _parse_binding(row)
        if binding.key in seen:
            # 同一（环境，租户，入口，场景）出现两次，就是"这个场景该走哪个引擎"没有唯一答案。
            # 用文件顺序或"后写的赢"来裁决等于让配置的顺序承载语义 —— 拒绝。
            raise ChatBindingError("duplicate chat execution binding")
        seen.add(binding.key)
        _validate_binding(binding)
        bindings.append(binding)
    return tuple(bindings)


def _parse_binding(row: object) -> ChatBinding:
    if not isinstance(row, dict):
        raise ChatBindingError("chat execution binding is not an object")
    # `engine` 与 Dify 的两项先读：它们决定**哪些字段是必需的**。
    # 缺 `engine` ⇒ `node`（旧登记原样可读）；写了别的值 ⇒ 拒绝整份表，
    # 不静默当成本片认识的那两个之一。
    engine = row.get("engine", ENGINE_NODE)
    if not isinstance(engine, str) or engine not in ENGINES:
        raise ChatBindingError("chat execution binding engine is unsupported")
    required = ["environment", "tenant_id", "entry", "scenario"]
    if engine == ENGINE_NODE:
        required += ["config_file", "manifest_file"]
    else:
        required += ["workflow_id", "workflow_key_file"]
    values: dict[str, str] = {}
    for field in required:
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ChatBindingError(f"chat execution binding {field} is missing")
        values[field] = value.strip()
    if engine == ENGINE_NODE:
        # Dify 专属键出现在节点登记上，说明有人以为它在起作用 —— 拒绝比忽略诚实。
        if row.get("workflow_id") or row.get("workflow_key_file"):
            raise ChatBindingError("node chat execution binding must not declare a workflow")
    elif row.get("config_file") or row.get("manifest_file"):
        # 反过来同理：Dify 登记带节点配置，会让读者以为它还在跑节点。
        raise ChatBindingError("dify chat execution binding must not declare node config")
    if row.get("active") is False:
        # 停用一条绑定就是把它从这里拿掉；留一条 active:false 会让人以为它还在生效。
        raise ChatBindingError("inactive chat execution bindings must be removed")
    if values["entry"] != ENTRY_CONSUMER:
        raise ChatBindingError("chat execution binding entry must be consumer")
    if values["scenario"] == SCENARIO_CONSUMER_SERVICE:
        if engine != ENGINE_DIFY:
            raise ChatBindingError("consumer service requires a Dify Chatflow")
        agent_id, version = row.get("agent_id", ""), row.get("agent_version_no", 0)
        app_id = row.get("app_id", "")
        from aiops_diagnostics.dify.dify_chat_workflow import WORKFLOW_ID_PATTERN

        if not isinstance(app_id, str) or not WORKFLOW_ID_PATTERN.fullmatch(app_id):
            raise ChatBindingError("consumer service requires an app UUID")
        if (
            not re.fullmatch(r"agt_[A-Za-z0-9]{8,64}", str(agent_id))
            or type(version) is not int
            or version < 1
        ):
            raise ChatBindingError("consumer service requires a published agent identity")
        content = row.get("content_file", "")
        libraries = row.get("knowledge_base_ids", [])
        if (
            not isinstance(content, str)
            or not isinstance(libraries, list)
            or any(
                not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", k) for k in libraries
            )
        ):
            raise ChatBindingError("invalid consumer knowledge configuration")
        live = row.get("live_order_status", False)
        if type(live) is not bool or (live and not content):
            raise ChatBindingError("live order status requires reviewed content")
        billing = row.get("live_billing_record", False)
        if type(billing) is not bool or (billing and not content):
            raise ChatBindingError("billing records require reviewed content")
        photo = row.get("photo_enabled", False)
        if type(photo) is not bool or (photo and not content):
            raise ChatBindingError("photos require reviewed content")
        callback_version = row.get("callback_version", 1)
        if type(callback_version) is not int or callback_version not in {1, 2, 3, 4}:
            raise ChatBindingError("unsupported consumer callback version")
        return ChatBinding(
            callback_version=callback_version,
            photo_enabled=photo,
            live_billing_record=billing,
            live_order_status=live,
            engine=engine,
            agent_id=agent_id,
            app_id=app_id,
            agent_version_no=version,
            content_file=content,
            knowledge_base_ids=tuple(libraries),
            **values,
        )
    if values["scenario"] != SCENARIO_OWN_ORDER_STATUS:
        raise ChatBindingError("unsupported chat execution binding scenario")
    return ChatBinding(engine=engine, **values)  # type: ignore[arg-type]


def _validate_binding(binding: ChatBinding) -> None:
    """这条绑定**此刻**是否真的可执行。按引擎分流，两种引擎的判据互不借用。"""
    if binding.engine == ENGINE_DIFY:
        _validate_dify_binding(binding)
        return
    _validate_binding_files(binding)


def _validate_dify_binding(binding: ChatBinding) -> None:
    """Dify 登记：凭据文件必须私有可读，且 workflow 身份形状正确。

    这里**不**去连 Dify 验证 workflow 是否存在：那是运行期的一次外部调用，放在启动路径上
    会让一个 Dify 抖动变成网关起不来。真正"是不是那个已发布 workflow"由执行记录在
    调用前后各核对一次（见 `dify_chat_workflow`）。
    """
    from aiops_diagnostics.dify.dify_chat_workflow import WORKFLOW_ID_PATTERN, load_workflow_key

    if WORKFLOW_ID_PATTERN.fullmatch(binding.workflow_id) is None:
        raise ChatBindingError("chat execution binding workflow id is malformed")
    try:
        load_workflow_key(binding.workflow_key_file)
    except ValueError as exc:
        raise ChatBindingError("chat execution binding workflow key is unusable") from exc


def _validate_binding_files(binding: ChatBinding) -> None:
    """这条绑定**此刻**是否真的可执行：两个文件都要能读，且指向同一个环境。

    启动时校验一次，运行时每次选中再读一次 —— 撤销就是改这个文件，下一次请求立刻生效。
    """
    from aiops_diagnostics.execution import execution_manifest

    try:
        config = AgentRunConfig.load(Path(binding.config_file))
    except AgentRunConfigError as exc:
        raise ChatBindingError("chat execution binding config is unusable") from exc
    if config.environment != binding.environment:
        raise ChatBindingError("chat execution binding config environment differs")
    try:
        manifest = (
            execution_manifest.from_text(binding.manifest_text)
            if binding.manifest_text
            else execution_manifest.load(binding.manifest_file)
        )
    except execution_manifest.ExecutionManifestError as exc:
        raise ChatBindingError("chat execution binding manifest is unusable") from exc
    # 聊天路线**必须**有清单：没有清单的 run 走旧协议（领取即 succeeded），
    # 于是「节点只领了业务、整轮没跑完」会被记成成功。这正是复核必修 2。
    if manifest is None:
        raise ChatBindingError("chat execution binding requires an execution manifest")


def key_file_for_app(key_dir: str | Path, app_id: str) -> str:
    """一个 Dify app 的 Service API 凭据文件：`<key_dir>/<app_id>.key`。

    发布**不搬运凭据**：那是运维预置的 0600 文件，发布只按 app_id 推出它的路径。
    这样"发布"这个过程从头到尾不需要读到密钥 —— 也就没有把它写进表的可能。
    """
    return str(Path(str(key_dir)).expanduser().absolute() / f"{app_id}.key")


def _bindings_from_store(path: str | Path, key_dir: str = "") -> tuple[ChatBinding, ...]:
    """把管理表里 `active` 的行读成绑定。`retracted` 的**不出现**在这里。

    这一点是撤回语义的全部：撤回之后这条 key 查不到活动绑定，聊天入口因此不派发；
    但它仍然在表里留着 `retracted` 那一行，回答得了"这一版被撤回了"，而删行会让
    请求掉回直读。
    """
    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    store = ChatRouteBindingStore(Path(str(path)).expanduser().absolute())
    bindings: list[ChatBinding] = []
    for row in store.all():
        if not row.active:
            continue
        binding = ChatBinding(
            environment=row.environment,
            tenant_id=row.tenant_id,
            entry=row.entry,
            scenario=row.scenario,
            config_file="",
            manifest_file="",
            engine=row.engine,
            workflow_id=row.workflow_id,
            workflow_key_file=row.workflow_key_ref
            or (key_file_for_app(key_dir, row.app_id) if key_dir else ""),
            agent_version_no=row.agent_version_no,
            agent_id=row.agent_id,
            app_id=row.app_id,
            route_revision=row.revision,
        )
        if binding.scenario == SCENARIO_CONSUMER_SERVICE:
            from dataclasses import replace

            from aiops_diagnostics.publication.agent_lifecycle import AgentError, AgentStore

            try:
                snapshot = (
                    AgentStore(store.path).version(row.agent_id, row.tenant_id, row.agent_version_no).snapshot
                )
            except AgentError as exc:
                raise ChatBindingError("consumer publication version unavailable") from exc
            frozen = snapshot.get("consumer_service")
            if (
                snapshot.get("app_id") != row.app_id
                or snapshot.get("workflow_id") != row.workflow_id
                or not isinstance(frozen, dict)
                or not isinstance(frozen.get("content"), dict)
                or not isinstance(frozen.get("knowledge_base_ids"), list)
                or any(
                    not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", k)
                    for k in frozen.get("knowledge_base_ids", [])
                )
                or frozen["content"].get("tenant_id") != row.tenant_id
                or frozen["content"].get("environment") != row.environment
                or frozen["content"].get("state") != "published"
                or type(frozen.get("live_order_status")) is not bool
                or type(frozen.get("live_billing_record")) is not bool
                or type(frozen.get("callback_version", 1)) is not int
                or frozen.get("callback_version", 1) not in {1, 2, 3, 4}
            ):
                raise ChatBindingError("consumer publication has no frozen content")
            binding = replace(
                binding,
                callback_version=frozen.get("callback_version", 1),
                photo_enabled=frozen.get("photo_enabled", False),
                content_snapshot=frozen["content"],
                knowledge_base_ids=tuple(frozen["knowledge_base_ids"]),
                live_order_status=frozen["live_order_status"],
                live_billing_record=frozen["live_billing_record"],
            )
        if binding.engine == ENGINE_NODE:
            from dataclasses import replace

            from aiops_diagnostics.publication.agent_lifecycle import AgentError, AgentStore

            try:
                snapshot = (
                    AgentStore(store.path).version(row.agent_id, row.tenant_id, row.agent_version_no).snapshot
                )
            except AgentError as exc:
                raise ChatBindingError("node publication version is unavailable") from exc
            text = snapshot.get("node_manifest")
            if not isinstance(text, str):
                raise ChatBindingError("node publication has no execution manifest")
            binding = replace(binding, config_file=row.node_config_file, manifest_text=text)
        _validate_binding(binding)
        bindings.append(binding)
    return tuple(bindings)


def _bindings(settings: GatewayServerSettings) -> tuple[ChatBinding, ...]:
    """此刻生效的绑定 = 文件登记 + 管理表，**但同一个 key 不许同时出现在两边**。

    管理表（`chat_route_bindings`）是发布/撤回推进的正式指针；文件登记保留给节点
    路线与既有 Dify 部署（兼容是硬要求）。两边都声明同一个 key 时**不裁决优先级**：
    那会让"这次到底谁在跑"从一个事实变成一个猜测。
    """
    raw = str(getattr(settings, "chat_execution_bindings_file", "") or "")
    from_file = load_bindings(raw) if raw else ()
    database = str(getattr(settings, "chat_route_bindings_file", "") or "")
    key_dir = str(getattr(settings, "chat_dify_key_dir", "") or "")
    from_store = _bindings_from_store(database, key_dir) if database else ()
    if from_file and from_store:
        overlap = {item.key for item in from_file} & {item.key for item in from_store}
        if overlap:
            raise ChatBindingError("a chat route binding is declared in both places")
    return tuple(from_file) + tuple(from_store)


def assert_no_binding_collision(settings: GatewayServerSettings) -> None:
    """启动期判据：同一个 key 不许同时被文件与管理表声明。违反 ⇒ 启动失败。

    只比 `active` 行：一条已撤回的管理行不参与路由，与文件行并存不冲突 —— 那正是
    "撤回之后改回手填登记"的迁移路径。
    """
    raw = str(getattr(settings, "chat_execution_bindings_file", "") or "")
    database = str(getattr(settings, "chat_route_bindings_file", "") or "")
    if not raw or not database:
        return
    from_file = {item.key for item in load_bindings(raw)}
    from_store = {item.key for item in _bindings_from_store(database)}
    if from_file & from_store:
        raise ChatBindingError("a chat route binding is declared in both places")


def select_binding(
    settings: GatewayServerSettings,
    caller: ScopeContext,
    *,
    scenario: str = SCENARIO_OWN_ORDER_STATUS,
    entry: str = ENTRY_CONSUMER,
) -> ChatBinding | None:
    """这一条请求该用哪条绑定。**没有登记就是没有绑定**，不猜、不从别的开关推导。

    绑定表读不出来是**拒绝**而不是"当作没配"：后者会让一条本该走节点的请求悄悄
    回落到直读 —— 用户拿到的仍是答案，只是它不是这次派发的结果。
    """
    environment = str(getattr(settings, "execution_environment", "") or "")
    tenant_id = caller.effective_tenant_id
    try:
        bindings = _bindings(settings)
    except ChatBindingError as exc:
        raise ChatRunUnavailable("EXEC_CONFIG_UNAVAILABLE", "agent run unavailable", retryable=True) from exc
    for binding in bindings:
        if (
            binding.environment == environment
            and binding.tenant_id == tenant_id
            and binding.entry == entry
            and binding.scenario == scenario
        ):
            return binding
    # 没有**活动**绑定，但这条路由可能**被撤回过**。这两种情况必须分开：
    #   * 从来没配过 ⇒ `None`，走原直读（兼容与隔离）；
    #   * 被撤回过 ⇒ **拒绝**，绝不回落直读。
    # 撤回的目的是"停止交付"，而直读会照样给出一个答案——那是最难发现的一种失败：
    # 用户拿到了结果，只是它来自另一条路径。
    if _retracted(settings, environment, tenant_id, scenario=scenario, entry=entry):
        raise ChatRunUnavailable("CHAT_ROUTE_RETRACTED", "this version has been retracted", http_status=410)
    return None


def _retracted(
    settings: GatewayServerSettings,
    environment: str,
    tenant_id: str,
    *,
    scenario: str = SCENARIO_OWN_ORDER_STATUS,
    entry: str = ENTRY_CONSUMER,
) -> bool:
    """这条（环境，入口，场景）在管理表里是不是 `retracted`。"""
    database = str(getattr(settings, "chat_route_bindings_file", "") or "")
    if not database:
        return False
    from pathlib import Path

    from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore

    try:
        row = ChatRouteBindingStore(Path(database).expanduser().absolute()).get(
            environment, tenant_id, entry, scenario
        )
    except Exception as exc:  # noqa: BLE001 - 管理状态不可知，不能回落业务读取。
        raise ChatRunUnavailable(
            "EXEC_CONFIG_UNAVAILABLE", "route state unavailable", retryable=True
        ) from exc
    return row is not None and not row.active


def binding_for_key(settings: GatewayServerSettings, key: str | None) -> ChatBinding | None:
    """按 run 上冻结的那把钥匙找回**签发时**那一条绑定。

    找不到 ⇒ ``None``：那条 run 的绑定已经被拿掉（停用），结果不能再交付。
    这不是「回落到当前配置」—— 回落等于用另一份配置给旧结果背书。
    """
    if not key:
        return None
    if key.startswith("operator-trial:"):
        return trial_binding(settings, key.removeprefix("operator-trial:"))
    try:
        bindings = _bindings(settings)
    except ChatBindingError as exc:
        raise ChatRunUnavailable("EXEC_CONFIG_UNAVAILABLE", "agent run unavailable", retryable=True) from exc
    for binding in bindings:
        if binding.key == key:
            return binding
    return None


def trial_binding(settings, app_id):
    from aiops_diagnostics.dify.self_service_store import SelfServiceStore

    if not getattr(settings, "operator_profiles_file", ""):
        return None
    row = SelfServiceStore(settings.database_file).app(app_id)
    if not row:
        return None
    from aiops_diagnostics.dify.self_service_config import profiles

    profile = profiles(settings)[row["profile_id"]]
    target = profile["target"]
    return ChatBinding(
        environment=target["environment"],
        tenant_id=row["tenant_id"],
        entry="operator",
        scenario="operator_order_diagnosis",
        config_file=target["config_file"],
        manifest_file=target["manifest_file"],
        app_id=app_id,
        route_revision=row["revision"],
        trial_app_id=app_id,
    )


def callback_config(store: Any, run_id: str, settings: GatewayServerSettings) -> AgentRunConfig | None:
    """这条 run 的回调（领取/完成）该用**哪份配置**。

    判据落在**这条 run 自己**身上，而不是「哪个键非空」：

    * run 上冻着绑定钥匙 ⇒ 它是聊天入口签发的 ⇒ 用**那条**绑定；
    * 否则用委派绑定。

    返回 ``None`` 表示「这条 run 对应的绑定当前没配置」—— 调用方按不可用处理，
    **不**悄悄换另一份配置去认证。
    """
    record = store.peek(run_id)
    if record is None:
        return None
    if getattr(record, "qa_id", None):
        binding = binding_for_key(settings, getattr(record, "chat_binding", None))
    else:
        binding = _delegation_binding(settings)
    if binding is None:
        return None
    if record.route_revision and record.route_revision != binding.route_revision:
        return None
    try:
        return AgentRunConfig.load(Path(binding.config_file))
    except AgentRunConfigError:
        return None


def _delegation_binding(settings: GatewayServerSettings) -> ChatBinding:
    return ChatBinding(
        environment=str(getattr(settings, "execution_environment", "") or ""),
        tenant_id="",
        entry=ENTRY_CONSUMER,
        scenario=SCENARIO_OWN_ORDER_STATUS,
        config_file=str(getattr(settings, "agent_run_config_file", "") or ""),
        manifest_file=str(getattr(settings, "execution_manifest_file", "") or ""),
    )


# --------------------------------------------------------------------------
# 签发与派发（两个入口共用）
# --------------------------------------------------------------------------


def issue_for_store(
    *,
    store: Any,
    settings: GatewayServerSettings,
    binding: ChatBinding,
    caller: ScopeContext,
    order_no: str,
    gateway_version: str,
) -> tuple[AgentRun, AgentRunConfig]:
    """签发一条**绑定在案**的 run，并返回本次使用的配置（派发需要它）。"""
    from aiops_diagnostics.execution import execution_manifest

    if binding.environment != str(getattr(settings, "execution_environment", "") or ""):
        raise ChatRunUnavailable("AGENT_RUN_ENVIRONMENT_MISMATCH", "agent run unavailable", retryable=True)
    try:
        config = AgentRunConfig.load(Path(binding.config_file))
    except AgentRunConfigError as exc:
        raise ChatRunUnavailable(
            "AGENT_RUN_CONFIG_UNAVAILABLE", "agent run unavailable", retryable=True
        ) from exc
    if config.environment != binding.environment:
        raise ChatRunUnavailable("AGENT_RUN_ENVIRONMENT_MISMATCH", "agent run unavailable", retryable=True)
    try:
        manifest = (
            execution_manifest.from_text(binding.manifest_text)
            if binding.manifest_text
            else execution_manifest.load(binding.manifest_file)
        )
    except execution_manifest.ExecutionManifestError as exc:
        raise ChatRunUnavailable("EXEC_CONFIG_UNAVAILABLE", "agent run unavailable", retryable=True) from exc
    if manifest is None:
        # 签发时也做非空防御：`AgentRunStore.complete` 对没有期望身份的记录会按旧协议
        # 直接转 succeeded，「节点只领了业务」于是变成成功。聊天路线不接受这份旧语义。
        raise ChatRunUnavailable("EXEC_CONFIG_UNAVAILABLE", "agent run unavailable", retryable=True)
    run = store.issue(
        scope=caller,
        order_no=order_no,
        config=config,
        gateway_version=gateway_version,
        expected_agent_version=manifest.identity,
        chat_binding=binding.key,
        execution_manifest=binding.manifest_text or None,
        route_revision=binding.route_revision,
    )
    return run, config


def dispatch_run(*, store: Any, config: AgentRunConfig, run: AgentRun, dispatcher: Any = None) -> None:
    """派发一次。失败把记录钉成 failed 并抛 `ChatRunUnavailable`。"""
    try:
        (dispatcher or SnailDispatcher()).dispatch(config, run.run_id)
    except AgentRunDispatchError as exc:
        store.fail(run.run_id, "platform_unavailable")
        raise ChatRunUnavailable("AGENT_RUN_UNAVAILABLE", "agent run unavailable") from exc


def settled_run(*, store: Any, run_id: str) -> AgentRun:
    """等一条已经派发的 run 落定，并断言它真的成功了。

    「平台回了话」不等于「节点跑完了」—— 这是 M3 那一片的核心教训，这里沿用同一判据。
    """
    try:
        settled = store.get(run_id)
    except (ValueError, TypeError) as exc:
        raise ChatRunUnavailable("AGENT_RUN_NO_RESULT", "agent run produced no result") from exc
    if settled is None:
        raise ChatRunUnavailable("AGENT_RUN_UNAVAILABLE", "agent run unavailable", retryable=True)
    if settled.state != SUCCEEDED:
        raise ChatRunUnavailable("AGENT_RUN_NO_RESULT", "agent run produced no result")
    return settled


# --------------------------------------------------------------------------
# 最终放行：**一份**判据，三个调用方
# --------------------------------------------------------------------------


def final_release(
    *,
    store: Any,
    settings: GatewayServerSettings,
    caller: ScopeContext,
    run_id: str,
) -> AgentRun:
    """交出去之前的最后一道判定。委派入口、聊天 worker、聊天回读**共用这一份**。

    顺序是刻意的（R2-1）：**先**完成可能阻塞的外部授权，**再**读记录做本地判定 ——
    所以这里不碰授权，只做本地判定，调用方必须已经授权完（并在这之后调用）。

    绑定在这一刻**重新解析**（`callback_config` 按 run 上冻着的钥匙找回签发时那一条）：
    节点被停用、配置被换掉、绑定被拿掉，都在这里变成拒绝，而不是拿签发时那一份
    内存里的对象继续背书。

    判据（一条都不能少，少一条就是给某种失效开了后门）：

    * 记录仍是 ``succeeded``、未过期、且属于**这个**调用者；
    * 记录里的执行位置与**此刻**的配置逐项相等（环境/app/agent/预期节点）；
    * 实际领取的节点就是预期节点，且此刻**仍然有效**；
    * 清单期望的身份**已核对一致**（``matched``），不是"签发时说好了要一致"；
    * 快照自洽：五字段齐全、状态码与角色相符、观察时间落在领取与完成之间。

    失败时把记录钉成 ``failed``（``refuse``）：这一笔授权不会再被交出去。

    读取顺序是**三次**，不是两次（Codex 复核必修 1）：``get`` → 读绑定/配置 → **再 ``get``**。
    少了最后一次，配置读取期间发生的撤销/到期会被漏掉 —— 复核实测 GET 仍 ``200`` + 正文，
    而库里已经是 ``revoked`` / ``expired``。``get`` 本身就是读事务并且顺手收敛过期，
    所以这里不加锁、不加分布式协议，只是**不要把早先读到的那一份当最终依据**。
    """
    seen = store.get(run_id)  # 第一次：确认它曾经是 succeeded（否则连读配置都不必）
    if seen is None or seen.state != SUCCEEDED:
        raise ChatRunUnavailable("AGENT_RUN_NO_RESULT", "agent run produced no result", http_status=404)
    try:
        config = callback_config(store, run_id, settings)
    except ChatBindingError as exc:
        raise ChatRunUnavailable(
            "AGENT_RUN_CONFIG_UNAVAILABLE", "agent run unavailable", retryable=True
        ) from exc
    if config is None:
        store.refuse(run_id, "binding_unavailable")
        raise ChatRunUnavailable("AGENT_RUN_CONFIG_UNAVAILABLE", "agent run unavailable", retryable=True)
    # 第二次：配置读完之后重读。这是唯一被允许据以放行的对象；上面那份只用来决定
    # "值不值得继续读配置"。撤销/到期/别人钉的终态都在这里变成拒绝。
    latest = store.get(run_id)
    if latest is None or latest.state != SUCCEEDED:
        raise ChatRunUnavailable("AGENT_RUN_NO_RESULT", "agent run produced no result", http_status=404)
    _refuse_unless_consistent(store, latest, settings, caller, config)
    _refuse_unless_snapshot_valid(store, latest)
    return latest


def _refuse_unless_consistent(
    store: Any,
    run: AgentRun,
    settings: GatewayServerSettings,
    caller: ScopeContext,
    config: AgentRunConfig,
) -> None:
    # 凭据**轮换**是合法的：同一个 ``node_id`` 可以有多条登记（旧 token 停用、新 token 生效）。
    # 所以"这个节点此刻有效"必须问「**存在**一条 active、同环境、同实际节点的登记吗」，
    # 而不是"取第一条看看"。取第一条会让一次正常轮换把成功结果判成失败
    # （Codex 复核必修 2 实测：GET 403，且 succeeded 被改成 failed）。
    node_valid = any(
        item.node_id == run.actual_node_id and item.active and item.environment == run.environment
        for item in config.node_token_hashes.values()
    )
    if (
        run.scope_fingerprint != caller.scope_fingerprint
        or run.environment != settings.execution_environment
        or config.environment != run.environment
        or config.snail_app_id != run.app_id
        or str(config.snail_agent_id) != run.agent_id
        or config.expected_node_id != run.expected_node_id
        # 归属是独立的一条：上面的 node_valid 只回答"这台节点此刻有效"，
        # 不回答"这条结果确实是它交回来的"。
        or run.actual_node_id != run.expected_node_id
        or not node_valid
        or (run.expected_agent_version is not None and not run.exec_config_confirmed)
    ):
        store.refuse(run.run_id, "consumer_release_mismatch")
        raise ChatRunUnavailable("AGENT_RUN_NOT_ACTIVE", "agent run refused", http_status=403)


def _refuse_unless_snapshot_valid(store: Any, run: AgentRun) -> None:
    snapshot = run.snapshot
    try:
        if not isinstance(snapshot, dict) or set(snapshot) != SNAPSHOT_FIELDS:
            raise ValueError("invalid snapshot fields")
        code = snapshot["status_code"]
        if isinstance(code, bool) or not isinstance(code, (str, int)) or len(str(code)) > 32:
            raise ValueError("invalid status code")
        expected_role = (
            role_of(code).value
            if str(code) in {str(value) for value in KNOWN_STATUS_CODES}
            else StatusRole.UNKNOWN.value
        )
        observed = datetime.fromisoformat(snapshot["observed_at"])
        if (
            snapshot["order_no"] != run.order_no
            or snapshot["status_role"] != expected_role
            or snapshot["source"] != "cpms.chargingInfo"
            or observed.utcoffset() != timedelta(0)
            or run.claimed_at is None
            or run.completed_at is None
            or not run.claimed_at <= observed <= run.completed_at
        ):
            raise ValueError("inconsistent snapshot")
    except (ValueError, TypeError, KeyError) as exc:
        store.refuse(run.run_id, "snapshot_invalid")
        raise ChatRunUnavailable(
            "AGENT_RUN_NO_RESULT", "agent run produced no result", http_status=502
        ) from exc


# --------------------------------------------------------------------------
# 结果适配与回读一致性
# --------------------------------------------------------------------------


def answer_from_run(run: AgentRun, *, language: str) -> dict[str, Any]:
    """把该 run 的**成功快照**适配成原回答格式。不读第二个来源。

    快照的字段集合与确定性直读那条路一致，所以渲染复用同一个 `status_answer` ——
    App 看到的 blocks 形状不会因为「谁来读」而变。
    """
    from aiops_diagnostics.consumer.consumer_order_chat import status_answer

    if run.snapshot is None:
        raise ChatRunUnavailable("AGENT_RUN_NO_RESULT", "agent run produced no result")
    return status_answer(run.snapshot, language)


def answer_matches_snapshot(result: Any, snapshot: dict[str, Any]) -> bool:
    """缓存下来的 QA 正文是不是**这条快照**的渲染。

    回读时重算一次，不一致就拒绝：一份被改过（或被写错 run）的正文不能被当成
    同一条 run 的结果交出去。语言按受支持的那几种都试一遍 —— 缓存正文的渲染语言
    与本次请求头无关，用请求头去比对会把一次正常的换语言回读判成篡改。
    """
    from aiops_diagnostics.consumer.consumer_order_chat import status_answer

    return any(result == status_answer(snapshot, language) for language in ANSWER_LANGUAGES)


__all__ = [
    "ANSWER_LANGUAGES",
    "BINDINGS_SCHEMA",
    "CHAT_RUN_PREFIX",
    "ChatBinding",
    "ChatBindingError",
    "ChatRunUnavailable",
    "answer_from_run",
    "answer_matches_snapshot",
    "binding_for_key",
    "callback_config",
    "dispatch_run",
    "final_release",
    "issue_for_store",
    "load_bindings",
    "select_binding",
    "settled_run",
]
