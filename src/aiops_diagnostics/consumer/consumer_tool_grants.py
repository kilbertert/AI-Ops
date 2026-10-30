"""C 端运行里的 CPMS 只读工具：授权、代理与证据（#1209）。

## 为什么这是网关侧的一块，节点一行都不用改

节点把请求发到**网关代理**，带的是网关签发的 grant token；公司令牌从头到尾在网关手里
（`operator_cpms.proxy()` 用 `task.bearer` 调 CPMS）。所以 C 端要的只是「另一条授权与
代理的路径」，不是另一套节点协议 —— 契约版本、`CpmsReadTool`、清单解析全都不用动。

## 与管家那条路的差别

| | 管家 | C 端 |
| --- | --- | --- |
| 运行 | `operator_*` 两种 operation | C 端订单状态运行 |
| 任务状态 | `OperatorCpms.tasks`（内存，持有公司令牌） | 这里（内存，**不持有任何业务凭据**） |
| CPMS 凭据 | 公司令牌 + `client-type: admin` | `tenant-id` + `user-id`，无令牌 |
| 可见范围 | 订单站点 / 账号站点集合 | **本人订单里出现过的站点** |
| 范围外行 | 拒绝（上游带了集合还越界 = 违约） | 跳过（上游按租户返回，越界是预期） |

## 边界

- 只接受声明里 `applies_to` 含 `conversation` 的工具；订单诊断工具没有订单就没有取证对象。
- 参数按 ADR-0176 判据放行：必须是用户提问原文的子串。放行发生在**调用 CPMS 之前**。
- 范围推导不复制第二份：用 `consumer_sites.own_site_scope`，与 `GET /v1/sites` 同一个函数。
- 单页校验不复制第二份：用 `operator_cpms.site_page`，与整份查询同一条判据。
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

from aiops_diagnostics.consumer import consumer_sites
from aiops_diagnostics.execution.agent_runs import scope_from_json
from aiops_diagnostics.operator import operator_cpms, operator_tools

#: 本模块只服务这一类工具：声明里说它能在无订单回合里用。
CONVERSATION = "conversation"


class ConsumerToolRefused(RuntimeError):
    """C 端工具授权/代理/证据不成立。``code`` 是回给调用方的稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _Grant:
    token: str
    fingerprint: str
    tenant_id: str
    c_user_id: str
    tool: str
    arguments: dict
    request: dict
    site_ids: tuple[str, ...]
    base_url: str
    expires_at: datetime
    lock: threading.RLock = field(default_factory=threading.RLock)
    evidence: dict | None = None


class ConsumerToolGrants:
    """一次运行的 C 端工具授权。**不持有任何业务凭据。**"""

    def __init__(self, context, settings) -> None:
        self.context = context
        self.settings = settings
        self.grants: dict[str, _Grant] = {}
        self.lock = threading.RLock()

    # ── 内部 ────────────────────────────────────────────────────────────
    def _prune(self) -> None:
        now = datetime.now(UTC)
        for key in [k for k, g in self.grants.items() if g.expires_at <= now]:
            self.grants.pop(key, None)

    def _proxy_base(self, run) -> str:
        """节点把请求发回哪个网关地址。

        与管家那条路同源：取这条 run 所属节点目标的 `proxy_base_url`。**不猜**
        `public_base_url` —— 那是浏览器入口，不是节点回调入口，两者可以不同。
        """
        from aiops_diagnostics.execution.node_publication import all_targets

        for target in all_targets(self.settings):
            if (
                target.app_id == run.app_id
                and target.environment == run.environment
                and target.proxy_base_url
            ):
                return target.proxy_base_url.rstrip("/")
        if self.settings.public_base_url:
            return self.settings.public_base_url.rstrip("/")
        raise ConsumerToolRefused("CONSUMER_PROXY_UNRESOLVED", "node callback base is not configured")

    def _cpms_base(self, tenant_id: str) -> str:
        from aiops_diagnostics.gateway.gateway_config import charging_endpoint

        try:
            return charging_endpoint(self.settings.charging_status_base_url, tenant_id)
        except ValueError as exc:
            raise ConsumerToolRefused(
                "TENANT_ENDPOINT_UNRESOLVED", "business endpoint is not registered"
            ) from exc

    def _run(self, run_id: str, node_token: str):
        """认节点 → 取运行。**身份由节点凭据决定，请求体不参与。**"""
        from aiops_diagnostics.execution import chat_execution

        run = self.context.agent_run_store.get(run_id)
        if run is None or run.state not in {"issued", "running"} or run.expires_at <= datetime.now(UTC):
            raise ConsumerToolRefused("CONSUMER_RUN_INACTIVE", "consumer run is inactive")
        config = chat_execution.callback_config(self.context.agent_run_store, run_id, self.settings)
        if config is None:
            raise ConsumerToolRefused("CONSUMER_RUN_INACTIVE", "no callback config for this run")
        node = config.authenticate_node(node_token)
        if (
            node is None
            or node.node_id != run.expected_node_id
            or node.environment != run.environment
            or config.expected_node_id != run.expected_node_id
            or config.environment != run.environment
        ):
            raise ConsumerToolRefused("CONSUMER_NODE_REJECTED", "node is not accepted for this run")
        return run

    def _question(self, run) -> str:
        qa = self.context.store.get_assistant_question(run.qa_id, run.scope_fingerprint)
        if not qa:
            raise ConsumerToolRefused("CONSUMER_RUN_INACTIVE", "consumer run has no question")
        return str(qa["question"])

    def _declared(self, tool: str):
        try:
            declared = operator_tools.tool(tool)
        except operator_tools.ToolDeclarationError as exc:
            raise ConsumerToolRefused("CONSUMER_TOOL_DISABLED", "undeclared tool") from exc
        if CONVERSATION not in declared.applies_to:
            raise ConsumerToolRefused("CONSUMER_TOOL_DISABLED", "tool does not serve this run type")
        return declared

    # ── 对外三步 ────────────────────────────────────────────────────────
    def grant(self, run_id: str, node_token: str, manifest_id: str, tool: str, contract: str, arguments):
        if contract != operator_cpms.TOOL_CONTRACT:
            raise ConsumerToolRefused("CONSUMER_TOOL_CONTRACT_OUTDATED", "tool contract is outdated")
        run = self._run(run_id, node_token)
        if manifest_id != run.expected_agent_version:
            raise ConsumerToolRefused("CONSUMER_MANIFEST_MISMATCH", "manifest does not match")
        declared = self._declared(tool)
        from aiops_diagnostics.execution import execution_manifest

        if tool not in execution_manifest.from_text(run.execution_manifest).tool_names:
            raise ConsumerToolRefused("CONSUMER_TOOL_DISABLED", "tool is not in this manifest")

        caller = scope_from_json(run.scope_json)
        c_user_id = caller.subject.c_user_id or ""
        if not c_user_id:
            raise ConsumerToolRefused("CONSUMER_SCOPE_REQUIRED", "consumer identity required")
        # 参数放行发生在**取数之前**：模型编的地址不会换来一次真实的对外请求。
        cleaned = operator_cpms.validate_tool_arguments(tool, arguments, self._question(run))
        base = self._cpms_base(caller.effective_tenant_id)
        own = consumer_sites.own_site_scope(
            caller.effective_tenant_id,
            c_user_id,
            consumer_sites.order_reader(base, caller.effective_tenant_id, c_user_id),
        )
        token = secrets.token_urlsafe(32)
        values = operator_cpms.run_values(tool, "", cleaned)
        with self.lock:
            self._prune()
            if len(self.grants) >= 20:
                raise ConsumerToolRefused("CONSUMER_BUSY", "too many consumer tool grants")
            self.grants[run_id] = _Grant(
                token=token,
                fingerprint=caller.scope_fingerprint,
                tenant_id=caller.effective_tenant_id,
                c_user_id=c_user_id,
                tool=tool,
                arguments=cleaned,
                request=declared.request(values),
                site_ids=own.site_ids,
                base_url=base,
                expires_at=run.expires_at,
            )
        return {
            "run_id": run_id,
            "tool": tool,
            "manifest_id": run.expected_agent_version,
            "authorization_id": _digest(token),
            "tenant_id": caller.effective_tenant_id,
            "site_ids": list(own.site_ids),
            "inputs": declared.declared_inputs(values),
            "request": declared.request(values),
            "contract": operator_cpms.TOOL_CONTRACT,
            # 节点把请求发回**本网关**的 C 端代理；公司令牌不出网关，C 端也不带任何令牌。
            "base_url": self._proxy_base(run) + "/v1/internal/consumer-tools/" + run_id,
            "bearer": token,
            "expires_at": run.expires_at.isoformat(),
        }

    def _current(self, run_id: str, token: str) -> _Grant:
        with self.lock:
            grant = self.grants.get(run_id)
        if grant is None or not secrets.compare_digest(grant.token, token):
            raise ConsumerToolRefused("CONSUMER_GRANT_REJECTED", "grant rejected")
        if grant.expires_at <= datetime.now(UTC):
            raise ConsumerToolRefused("CONSUMER_GRANT_EXPIRED", "grant expired")
        return grant

    def proxy(self, run_id: str, token: str, path: str, query: str, method: str = "GET"):
        grant = self._current(run_id, token)
        if grant.request != {"method": method, "path": path, "query": query}:
            raise ConsumerToolRefused("CONSUMER_PATH_REJECTED", "request target is not the granted one")
        with grant.lock:
            if grant.evidence is not None:
                return grant.evidence["response"]
            body = consumer_sites._get(  # noqa: SLF001 - 同一凭据形状只有一处实现
                grant.base_url + path + ("?" + query if query else ""),
                grant.tenant_id,
                grant.c_user_id,
            )
            scope = operator_cpms.SiteScope(grant.tenant_id, grant.site_ids, grant.fingerprint)
            page = int(query.rsplit("current=", 1)[1].split("&")[0]) if "current=" in query else 1
            rows, total = operator_cpms.site_page(scope, body, page, outside_scope="skip")
            declared = self._declared(grant.tool)
            data = {
                "records": [{key: row[key] for key in declared.evidence_fields} for row in rows],
                "total": total,
                "sample_limit": declared.page_size,
                "truncated": total > len(rows),
                "ordering_guaranteed": False,
                "complete_timeline": False,
                "samples_returned": len(rows),
                "absence": "no_samples" if not rows else None,
            }
            grant.evidence = {
                "tool": grant.tool,
                "inputs": declared.declared_inputs(operator_cpms.run_values(grant.tool, "", grant.arguments)),
                "source": declared.source,
                "observed_at": datetime.now(UTC).isoformat(),
                "data": data,
            }
            grant.evidence["response"] = data
            return data

    def record(self, run_id: str, node_token: str, manifest_id: str, tool: str, payload):
        grant = self._current(run_id, node_token)
        if set(payload) != {"authorization_id", "evidence"}:
            raise ConsumerToolRefused("CONSUMER_INVALID_BODY", "invalid body")
        if payload["authorization_id"] != _digest(grant.token):
            raise ConsumerToolRefused("CONSUMER_EVIDENCE_MISSING", "authorization does not match")
        with grant.lock:
            expected = grant.evidence
            if expected is None:
                raise ConsumerToolRefused("CONSUMER_EVIDENCE_MISSING", "no evidence was read")
            evidence = payload["evidence"]
            if (
                not isinstance(evidence, dict)
                or set(evidence) != {"tool", "inputs", "source", "observed_at", "data"}
                or evidence.get("tool") != grant.tool
                or evidence.get("inputs") != expected["inputs"]
                or evidence.get("source") != expected["source"]
                or evidence.get("data") != expected["data"]
            ):
                raise ConsumerToolRefused("CONSUMER_EVIDENCE_MISMATCH", "evidence does not match")
        return {
            "run_id": run_id,
            "tool": tool,
            "authorization_id": payload["authorization_id"],
            "accepted": True,
        }


def _digest(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()
