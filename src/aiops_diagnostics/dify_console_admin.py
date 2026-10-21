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

## 五条实测事实（2026-10-21，36 上只读核对过源码与接口，不是推断）

1. **`/plugin/install/marketplace` 走不通**：本机 ``MARKETPLACE_ENABLED=false``，它回
   400 ``marketplace is not enabled``。可用的是 ``/plugin/install/pkg``，body 是
   ``{"plugin_unique_identifiers": [...]}``，回 ``{"all_installed": bool, "task_id": str}``。
2. **安装是异步的**：``task_id`` 对应的任务要轮询 ``/plugin/tasks/<id>`` 到 ``success``，
   插件才真的出现在 ``/plugin/list`` 里。不等就去写凭据，会得到"provider 不存在"。
3. **provider 凭据必须每个 workspace 各写一份**：Dify 的数据模型里没有共享层
   （``providers.tenant_id`` 非空、凭据用**每租户一把 RSA 密钥**加解密），所以复制行到
   别的 workspace 也解不开。
4. **写凭据会拿它去真实校验**：假 key 回 400 ``Credentials validation failed ... 401``。
   所以这一步必须用**真** key，且失败形态是"凭据无效"而不是"服务坏了"——两者要分开报。
   同名再写一次回 400 ``Credential with name '…' already exists``：**那是已达成，不是失败**。
5. **凭据名字只能从 provider 列表读**：``GET …/model-providers`` 的每一项带
   ``custom_configuration.available_credentials[{credential_id, credential_name}]``；
   而 ``GET …/<provider>/credentials`` 回的是**当前生效那一份的字段字典**（``api_key``
   已打码），里面没有名字列表 —— 拿它做幂等判据会永远"没找到"。
"""

from __future__ import annotations

import time
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
PLUGIN_TASK_PATH = "/console/api/workspaces/current/plugin/tasks"
MODEL_PROVIDERS_PATH = "/console/api/workspaces/current/model-providers"
APP_IMPORT_PATH = "/console/api/apps/imports"

#: 导入 DSL 的模式（Dify ``ImportMode``）。我们只走"正文直接给"这条：另一条要 URL，
#: 而模板在仓库里，没有理由为了它再起一个 HTTP 服务。
IMPORT_MODE_YAML_CONTENT = "yaml-content"

#: 翻页上限。一个租户 workspace 的 app 数量是几十的量级；这个数只是防无界请求的护栏。
MAX_APP_PAGES = 50


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
    #: 等一次插件安装任务落地的最长时间。安装是异步的，紧接着的写凭据要插件已在位。
    install_timeout: float = 180.0
    install_poll_interval: float = 1.0

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
        # Dify 的失败体用 ``{"message": …}``（大多数）或 ``{"error": …}``
        # （``/console/api/apps/imports`` 用后者）。默认表里没有 ``error``，
        # 少了它导入失败会被报成一句没有信息量的 "400"。
        detail_keys=("message", "error", "detail", "msg"),
    )


class DifyConsoleAdmin:
    """以 ``ADMIN_API_KEY`` 的身份、**针对某一个 workspace** 做管理动作。"""

    def __init__(self, settings: ConsoleApiSettings) -> None:
        self.settings = settings
        #: 最近一次导入的告警（``completed-with-warnings`` 才会非空）。
        self.last_import_warnings: tuple[str, ...] = ()

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
        payload = self._call(workspace_id, f"{PLUGIN_LIST_PATH}?page=1&page_size=100")
        plugins = payload.get("plugins") if isinstance(payload, dict) else None
        found: set[str] = set()
        for item in plugins or []:
            if not isinstance(item, dict):
                continue
            # 真实形状（Dify 1.17.1 `PluginListApi` → daemon `ListPlugins`）：
            # ``plugin_unique_identifier`` 在**顶层**，``meta`` 是任意 map。嵌套那份是
            # 早期探测时看到的另一种排布，留着做兜底 —— 两个都认不会误判，只认一个会。
            identifier = str(
                item.get("plugin_unique_identifier")
                or (item.get("meta") or {}).get("plugin_unique_identifier")
                or ""
            )
            if identifier:
                found.add(identifier)
        return found

    def install_plugins(self, workspace_id: str, identifiers: list[str]) -> list[str]:
        """装一组插件；返回**这次真的发了**的那些（已装的跳过）。

        **安装是异步的。** ``/plugin/install/pkg`` 立刻返回一个 ``task_id``，插件真正出现
        在 ``/plugin/list`` 里是几秒之后的事（成功时 ``all_installed`` 为真且无 task）。
        所以这里**等它落地**再返回 —— 不等的话，紧随其后的写凭据会因为插件还没装好而报
        "provider 不存在"，而那个报错会被读成"配置错了"。
        """
        have = self.installed_plugin_ids(workspace_id)
        missing = [item for item in identifiers if item not in have]
        if not missing:
            return []
        started = self._call(
            workspace_id,
            PLUGIN_INSTALL_PKG_PATH,
            method="POST",
            body={"plugin_unique_identifiers": missing},
        )
        task_id = str((started or {}).get("task_id") or "") if isinstance(started, dict) else ""
        if task_id and not (started or {}).get("all_installed"):
            self._await_install_task(workspace_id, task_id, expected=missing)
        return missing

    def _await_install_task(self, workspace_id: str, task_id: str, *, expected: list[str]) -> None:
        """轮询安装任务直到 ``success``；失败/超时都抛 ``ConsoleApiError``。

        超时**不算成功**：返回之后那一步（写凭据）会因此失败，而失败原因会指错地方。
        """
        deadline = time.monotonic() + self.settings.install_timeout
        while True:
            try:
                payload = self._call(workspace_id, f"{PLUGIN_TASK_PATH}/{task_id}")
            except ConsoleApiError as exc:
                # 任务记录被清理时 daemon 回 404。那不是"稍后再来"，重试只会把 180s 计时
                # 走完再报一个"超时，重跑即可"——把人送去重跑，而不是送去看真正的原因。
                if exc.code == "DIFY_CONSOLE_REJECTED" and exc.message.startswith("404"):
                    raise ConsoleApiError(
                        "DIFY_PLUGIN_INSTALL_UNKNOWN",
                        f"安装任务 {task_id} 在 daemon 里已不存在（404），无法确认结果。"
                        "重跑本命令会先读 /plugin/list 判定是否已装好",
                    ) from exc
                raise
            task = payload.get("task") if isinstance(payload, dict) else None
            status = str((task or {}).get("status") or "")
            if status == "success":
                installed = self.installed_plugin_ids(workspace_id)
                left = [item for item in expected if item not in installed]
                if left:
                    raise ConsoleApiError(
                        "DIFY_PLUGIN_INSTALL_INCOMPLETE",
                        f"安装任务 {task_id} 报成功但这些插件仍未出现：{', '.join(left)}",
                    )
                return
            if status == "failed":
                reasons = [
                    f"{item.get('plugin_unique_identifier')}: {item.get('message')}"
                    for item in (task or {}).get("plugins") or []
                    if isinstance(item, dict)
                ]
                raise ConsoleApiError(
                    "DIFY_PLUGIN_INSTALL_FAILED",
                    f"安装任务 {task_id} 失败：" + ("；".join(reasons) or "上游没给原因"),
                )
            if time.monotonic() >= deadline:
                raise ConsoleApiError(
                    "DIFY_PLUGIN_INSTALL_TIMEOUT",
                    f"安装任务 {task_id} 在 {self.settings.install_timeout:.0f}s 内没有结束"
                    f"（当前 {status or '未知'}）。插件可能稍后自己装好 —— 重跑本命令会跳过已装的",
                )
            time.sleep(self.settings.install_poll_interval)

    # ── provider 凭据 ─────────────────────────────────────────────────

    def provider_credential_names(self, workspace_id: str, provider: str) -> set[str]:
        """该 provider 在这个 workspace 里已有凭据的名字（**不含明文**）。

        走的是 provider **列表**端点，不是 ``<provider>/credentials``：后者（实测，
        2026-10-21 只读）回的是**当前生效那一份的字段字典**（``api_key`` 已被打码），
        里面**没有名字列表** —— 拿它做幂等判据会永远"没找到"，于是每次都重发一次
        POST，而 Dify 第二次会回 400 ``Credential with name '…' already exists``。
        """
        payload = self._call(workspace_id, MODEL_PROVIDERS_PATH)
        rows = payload.get("data") if isinstance(payload, dict) else None
        names: set[str] = set()
        for item in rows or []:
            if not isinstance(item, dict) or item.get("provider") != provider:
                continue
            available = (item.get("custom_configuration") or {}).get("available_credentials") or []
            for credential in available:
                name = str((credential or {}).get("credential_name") or "")
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

        "同名已存在"是**另一个**信号，而且它在重跑时才出现（列表与写入之间可能有人手工
        加过）：把它当作"已经达成"而不是失败 —— 我们要的结果是"这个名字的凭据在位"。
        """
        if name in self.provider_credential_names(workspace_id, provider):
            return False
        try:
            self._call(
                workspace_id,
                f"{MODEL_PROVIDERS_PATH}/{provider}/credentials",
                method="POST",
                body={"credentials": credentials, "name": name},
            )
        except ConsoleApiError as exc:
            if "already exists" in exc.message:
                return False
            raise
        return True

    # ── 模板 DSL ──────────────────────────────────────────────────────

    def import_app_dsl(self, workspace_id: str, *, yaml_content: str, name: str) -> str:
        """把模板 DSL 导进这个 workspace；返回新 app 的 id。

        导入接口有三种成功/半成功形态，只看 ``app_id`` 会把后两种说错：

        | HTTP | ``status`` | 含义 | 这里怎么报 |
        |---|---|---|---|
        | 200 | ``completed`` | 真导进去了 | 返回 app_id |
        | 200 | ``completed-with-warnings`` | 导进去了，但版本降级 | 返回 app_id，并记一条 warning |
        | 202 | ``pending`` | **等人确认**（跨大版本），app 还没建 | ``DIFY_IMPORT_PENDING`` |
        | 400 | ``failed`` | 拒绝，原因在 ``error`` | 把 ``error`` 带出来（否则只剩一个状态码） |

        最后一行是实测出来的：Dify 的失败体里原因键叫 ``error``，而 ``bounded_http`` 只读
        ``detail/msg/message`` ⇒ 400 会变成一句没有信息量的“400”。
        """
        payload = self._call(
            workspace_id,
            APP_IMPORT_PATH,
            method="POST",
            body={"mode": IMPORT_MODE_YAML_CONTENT, "yaml_content": yaml_content, "name": name},
        )
        body = payload if isinstance(payload, dict) else {}
        status = str(body.get("status") or "")
        app_id = str(body.get("app_id") or "")
        if status == "pending":
            raise ConsoleApiError(
                "DIFY_IMPORT_PENDING",
                f"导入停在 pending（{body.get('imported_dsl_version') or '?'}），需要确认后才会建 app："
                f"import_id={body.get('id') or '?'}。这是版本跨大版本时的正常形态，不是失败",
            )
        if not app_id:
            reason = str(body.get("error") or "")
            raise ConsoleApiError(
                "DIFY_IMPORT_UNREADABLE",
                "导入接口回了成功但没有 app_id" + (f"：{reason}" if reason else "，无法确认结果"),
            )
        if status == "completed-with-warnings":
            warnings = [str((w or {}).get("message") or w) for w in body.get("warnings") or []]
            self.last_import_warnings = tuple(warnings)
        return app_id

    def app_names(self, workspace_id: str) -> set[str]:
        """这个 workspace 里**全部** app 的名字（导入是幂等的判据）。

        必须翻页：``limit`` 上限 100，而"读第 1 页就断言不存在"会让第 2 页上的模板 app
        被判成"没导过" ⇒ 重跑再导一份同名 app。翻页判据用响应的 ``has_more``，不是
        "这一页是不是满的" —— 满页恰好在最后一页时会多问一次，那无害；反过来漏页有害。
        """
        names: set[str] = set()
        page = 1
        while True:
            payload = self._call(workspace_id, f"/console/api/apps?page={page}&limit=100")
            body = payload if isinstance(payload, dict) else {}
            for item in body.get("data") or []:
                name = str((item or {}).get("name") or "")
                if name:
                    names.add(name)
            if not body.get("has_more"):
                return names
            page += 1
            if page > MAX_APP_PAGES:
                raise ConsoleApiError(
                    "DIFY_APPS_UNREADABLE",
                    f"app 列表超过 {MAX_APP_PAGES} 页仍未结束；停止翻页，避免无界请求",
                )


__all__ = [
    "ConsoleApiError",
    "ConsoleApiSettings",
    "DifyConsoleAdmin",
    "IMPORT_MODE_YAML_CONTENT",
    "PLUGIN_INSTALL_PKG_PATH",
]
