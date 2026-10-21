"""Dify **控制台** API 的客户端：装插件、写 provider 凭据、导入模板 DSL（#881）。

与 :mod:`dify_inner_api` 的分工是**两条不同的门**，不能混：

| 用途 | 门 | 凭据 |
|---|---|---|
| 建 workspace / 把人 join 进去 | ``inner_api`` | ``X-Inner-Api-Key`` |
| 装插件 / 写 provider 凭据 / 导 DSL | ``console_api`` | ``ADMIN_API_KEY`` + ``X-WORKSPACE-ID`` |

后者是**按 workspace** 的：`ADMIN_API_KEY` 让请求以该 workspace 的 owner 身份执行
（``ext_login.py`` 按 ``X-WORKSPACE-ID`` 找 owner 行，不写 ``current``），所以同一个
admin key 可以为任意 workspace 做管理动作 —— 这正是"每个 workspace 自己的凭据"能写进去
的原因。

## 三条实测事实（2026-10-21，36 上，不是推断）

1. **`/plugin/install/marketplace` 走不通**：本机 ``MARKETPLACE_ENABLED=false``，它回
   400 ``marketplace is not enabled``。可用的是 ``/plugin/install/pkg``，body 是
   ``{"plugin_unique_identifiers": [...]}``，回 ``{"all_installed": true}``。**幂等**。
2. **provider 凭据必须每个 workspace 各写一份**：Dify 的数据模型里没有共享层
   （``providers.tenant_id`` 非空、凭据用**每租户一把 RSA 密钥**加解密），所以复制行到
   别的 workspace 也解不开。
3. **写凭据会拿它去真实校验**：假 key 回 400 ``Credentials validation failed ... 401``。
   所以这一步必须用**真** key，且失败形态是"凭据无效"而不是"服务坏了"——两者要分开报。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aiops_diagnostics.bounded_http import (
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    join_url,
    json_body,
    request_json,
)

PLUGIN_INSTALL_PKG_PATH = "/console/api/workspaces/current/plugin/install/pkg"
PLUGIN_LIST_PATH = "/console/api/workspaces/current/plugin/list"
MODEL_PROVIDERS_PATH = "/console/api/workspaces/current/model-providers"
APP_IMPORT_PATH = "/console/api/apps/imports"

#: 导入 DSL 的模式（Dify ``ImportMode``）。我们只走"正文直接给"这条：另一条要 URL，
#: 而模板在仓库里，没有理由为了它再起一个 HTTP 服务。
IMPORT_MODE_YAML_CONTENT = "yaml-content"


class ConsoleApiError(RuntimeError):
    """控制台 API 拒绝了这次动作。

    ``outcome`` 与 :class:`~aiops_diagnostics.dify_inner_api.InnerApiError` 同义但更窄：
    这里的每个动作都是**幂等**的（装插件、写凭据、导 DSL），所以没有"结果未知"那一侧 ——
    重跑一次是安全的，而"已经存在"也算成功。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ConsoleApiSettings:
    """``AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL`` + ``_API_KEY``（与发布/拉取同一把）。"""

    base_url: str
    api_key: str = field(repr=False, default="")
    timeout: float = 30.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)


def _error(failure: HttpFailure) -> Exception:
    code = {
        "auth_rejected": "DIFY_CONSOLE_KEY_REJECTED",
        "http_error": "DIFY_CONSOLE_REJECTED",
        "unavailable": "DIFY_CONSOLE_UNREACHABLE",
        "invalid_body": "DIFY_CONSOLE_UNREADABLE",
        "invalid_envelope": "DIFY_CONSOLE_UNREADABLE",
    }[failure.kind]
    detail = failure.detail or ""
    # 凭据校验失败是**最常见**的一种，且它要人换 key 而不是重启服务 —— 单独一个码。
    if "Credentials validation failed" in detail:
        code = "DIFY_CREDENTIAL_INVALID"
    return ConsoleApiError(code, f"{failure.status or ''} {detail}".strip())


def _mapping() -> ErrorMapping:
    return ErrorMapping(
        auth_rejected=_error,
        http_error=_error,
        unavailable=_error,
        invalid_body=_error,
        invalid_envelope=_error,
    )


class DifyConsoleAdmin:
    """以 ``ADMIN_API_KEY`` 的身份、**针对某一个 workspace** 做管理动作。"""

    def __init__(self, settings: ConsoleApiSettings) -> None:
        self.settings = settings

    def _headers(self, workspace_id: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.api_key}",
            "X-WORKSPACE-ID": workspace_id,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _call(self, workspace_id: str, path: str, *, method: str = "GET", body: Any = None) -> Any:
        try:
            return request_json(
                RequestSpec(
                    url=join_url(self.settings.base_url, path),
                    method=method,
                    headers=self._headers(workspace_id),
                    body=json_body(body) if body is not None else None,
                    timeout=self.settings.timeout,
                ),
                mapping=_mapping(),
            )
        except ConsoleApiError as exc:
            # 每个动作都标上是给**哪个 workspace** 做的：多租户之后，"装插件失败"这句话
            # 没有租户就等于没有信息 —— 运维得猜是哪一行映射出了问题。
            exc.message = f"[workspace {workspace_id}] {exc.message}"
            raise

    # ── 插件 ──────────────────────────────────────────────────────────

    def installed_plugin_ids(self, workspace_id: str) -> set[str]:
        """这个 workspace 已装的插件（``provider/name:version@sha``）。

        用它做**幂等**判定：装过的就不再发一次安装请求 —— 安装是异步任务型的，
        重复触发只会多出一次任务，而我们要的是"跑两次结果一样"。
        """
        payload = self._call(workspace_id, f"{PLUGIN_LIST_PATH}?page=1&page_size=200")
        plugins = payload.get("plugins") if isinstance(payload, dict) else None
        found: set[str] = set()
        for item in plugins or []:
            meta = item.get("meta") if isinstance(item, dict) else None
            identifier = str((meta or {}).get("plugin_unique_identifier") or "")
            if identifier:
                found.add(identifier)
        return found

    def install_plugins(self, workspace_id: str, identifiers: list[str]) -> list[str]:
        """装一组插件；返回**这次真的发了**的那些（已装的跳过）。"""
        have = self.installed_plugin_ids(workspace_id)
        missing = [item for item in identifiers if item not in have]
        if not missing:
            return []
        self._call(
            workspace_id,
            PLUGIN_INSTALL_PKG_PATH,
            method="POST",
            body={"plugin_unique_identifiers": missing},
        )
        return missing

    # ── provider 凭据 ─────────────────────────────────────────────────

    def provider_credential_names(self, workspace_id: str, provider: str) -> set[str]:
        """该 provider 在这个 workspace 里已有凭据的名字（**不含明文**）。"""
        payload = self._call(workspace_id, f"{MODEL_PROVIDERS_PATH}/{provider}/credentials", method="GET")
        entries = payload.get("credentials") if isinstance(payload, dict) else None
        names: set[str] = set()
        for item in entries or []:
            name = str((item or {}).get("credential_name") or "")
            if name:
                names.add(name)
        return names

    def write_provider_credential(
        self, workspace_id: str, provider: str, *, credentials: dict[str, str], name: str
    ) -> bool:
        """写一份 provider 凭据；**已存在同名则跳过**，返回是否真的写了。

        Dify 会拿这份凭据去**真实校验**（实测：假 key 回 400 ``Credentials validation
        failed``），所以这里的失败一定是"这把 key 不能用"，而不是"服务坏了" ——
        错误码因此分开（``DIFY_CREDENTIAL_INVALID``）。
        """
        if name in self.provider_credential_names(workspace_id, provider):
            return False
        self._call(
            workspace_id,
            f"{MODEL_PROVIDERS_PATH}/{provider}/credentials",
            method="POST",
            body={"credentials": credentials, "name": name},
        )
        return True

    # ── 模板 DSL ──────────────────────────────────────────────────────

    def import_app_dsl(self, workspace_id: str, *, yaml_content: str, name: str) -> str:
        """把模板 DSL 导进这个 workspace；返回新 app 的 id。"""
        payload = self._call(
            workspace_id,
            APP_IMPORT_PATH,
            method="POST",
            body={"mode": IMPORT_MODE_YAML_CONTENT, "yaml_content": yaml_content, "name": name},
        )
        app_id = str((payload or {}).get("app_id") or "") if isinstance(payload, dict) else ""
        if not app_id:
            raise ConsoleApiError("DIFY_IMPORT_UNREADABLE", "导入接口回了成功但没有 app_id，无法确认结果")
        return app_id

    def app_names(self, workspace_id: str) -> set[str]:
        """这个 workspace 里现有的 app 名字（导入是幂等的判据）。"""
        payload = self._call(workspace_id, "/console/api/apps?page=1&limit=100")
        apps = payload.get("data") if isinstance(payload, dict) else None
        return {str((item or {}).get("name") or "") for item in apps or []}


__all__ = [
    "ConsoleApiError",
    "ConsoleApiSettings",
    "DifyConsoleAdmin",
    "IMPORT_MODE_YAML_CONTENT",
    "PLUGIN_INSTALL_PKG_PATH",
]
