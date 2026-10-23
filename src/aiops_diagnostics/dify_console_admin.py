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
import urllib.error
from dataclasses import dataclass, field
from typing import Any

from aiops_diagnostics.bounded_http import (
    TRANSPORT_FAILURES,
    ErrorMapping,
    HttpFailure,
    RequestSpec,
    join_url,
    json_body,
    open_response,
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
#: dataset 翻页上限。一页 100，与 app 同一条护栏；一个 workspace 的库是几十的量级。
MAX_DATASET_PAGES = 50


class ConsoleApiError(RuntimeError):
    """控制台 API 拒绝了这次动作。

    ``outcome`` 与 :class:`~aiops_diagnostics.dify_inner_api.InnerApiError` 同义但更窄：
    这里的每个动作都是**幂等**的（装插件、写凭据、导 DSL），所以没有"结果未知"那一侧 ——
    重跑一次是安全的，而"已经存在"也算成功。
    """

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        #: 上游的 HTTP 状态（有的分支要按状态分流，例如安装任务被清理时 daemon 回的 4xx）。
        self.status = status


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
    # ``install/pkg`` 与 ``upload/pkg`` 的区别就在这里：装包要求 daemon 的**包桶里已经有**
    # 那个包（``GetPackage`` → ``plugin package not found, please upload it firstly``）。
    # 这个失败要单独一个码并点名下一步 —— 否则它看起来像"插件标识写错了"。
    if "package not found" in detail.lower():
        code = "DIFY_PLUGIN_PACKAGE_MISSING"
        detail = (
            detail + " —— 这是**主机级**前置：daemon 的包桶里没有这个包（装包不等于取包）。"
            "先 `POST /console/api/workspaces/current/plugin/upload/pkg` 把 .difypkg 送上去，"
            "或者让 Dify 的出口能取包；然后再跑本命令。它与租户无关，整个 Dify 做一次即可"
        )
    return ConsoleApiError(code, f"{failure.status or ''} {detail}".strip(), status=failure.status)


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

    def _call(
        self,
        workspace_id: str,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        follow_redirects: bool = True,
    ) -> Any:
        try:
            return request_json(
                RequestSpec(
                    url=join_url(self.settings.base_url, path),
                    method=method,
                    headers=self._headers(workspace_id),
                    body=json_body(body) if body is not None else None,
                    timeout=self.settings.timeout,
                    follow_redirects=follow_redirects,
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
                # Dify 1.17.1 的 ``PluginFetchInstallTaskApi`` 把 daemon 侧"任务不存在"
                # 折成 **400** ``{"code":"plugin_error"}``（不是 404）。两种都按"任务没了"处理，
                # 因为它们的下一步相同：别再轮询，回去读 ``/plugin/list`` 判真实状态。
                gone = exc.status == 404 or (exc.status == 400 and "not found" in exc.message.lower())
                if gone:
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

    def active_credential_name(self, workspace_id: str, provider: str) -> str:
        """这个 provider 当前**生效**的凭据名（Dify 回空串表示没有可用的）。

        与"凭据存在"是两件事：Dify 按 ``created_at DESC`` 选默认，所以一个**迁移过的**
        workspace 里，刚写进去的新凭据不一定生效 —— 旧的那份仍排在前面。
        2026-10-22 在 41 的租户 1942 上实测：`ensure-ready` 写成功后对话仍 401，因为生效的
        是迁移时复制过来、key 已失效的 `deepseek-official`。
        """
        payload = self._call(workspace_id, MODEL_PROVIDERS_PATH)
        for item in (payload.get("data") if isinstance(payload, dict) else None) or []:
            if isinstance(item, dict) and item.get("provider") == provider:
                custom = item.get("custom_configuration") or {}
                return str(custom.get("current_credential_name") or "")
        return ""

    def activate_provider_credential(self, workspace_id: str, provider: str, name: str) -> bool:
        """把名为 ``name`` 的凭据切成本 provider 的**当前**凭据；返回是否切了。

        已经是它就不发请求（幂等）。``switch`` 用的是 **provider 级**端点
        ``…/credentials/switch``；``…/models/credentials/switch`` 是 model 级的，对
        provider 级凭据会回 ``Credential record not found``（runbook 2026-10-13 记过）。
        """
        if self.active_credential_name(workspace_id, provider) == name:
            return False
        credential_id = ""
        payload = self._call(workspace_id, MODEL_PROVIDERS_PATH)
        for item in (payload.get("data") if isinstance(payload, dict) else None) or []:
            if not (isinstance(item, dict) and item.get("provider") == provider):
                continue
            for credential in (item.get("custom_configuration") or {}).get("available_credentials") or []:
                if (credential or {}).get("credential_name") == name:
                    credential_id = str(credential.get("credential_id") or "")
        if not credential_id:
            raise ConsoleApiError(
                "DIFY_CREDENTIAL_NOT_FOUND",
                f"{provider} 下没有名为 {name!r} 的凭据，无法切换当前凭据",
            )
        self._call(
            workspace_id,
            f"{MODEL_PROVIDERS_PATH}/{provider}/credentials/switch",
            method="POST",
            body={"credential_id": credential_id},
        )
        return True

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
            if "already exists" not in exc.message:
                raise
            # 被拒 ≠ 已达成。Dify 的重名判据是 ``tenant_id + provider_name.in_(…) +
            # credential_name``，所以**另一个 provider** 上的同名凭据也会回这句，而
            # 错误消息里带的名字正是我们要写的那个 —— 靠字符串区分不了 provider
            # （评审指出这一点；第一版修法只是多比了一个子串，仍会错判）。
            #
            # 判据改成**回读**：这个 provider 下现在能看到这个名字，才算已达成；
            # 看不到就说明被拒的是别的 provider 上的同名行，我们要的那份仍然没写进去。
            if name in self.provider_credential_names(workspace_id, provider):
                return False
            raise ConsoleApiError(
                "DIFY_CREDENTIAL_NAME_TAKEN",
                f"写入被拒：{exc.message}。但该 provider 下没有名为 {name!r} 的凭据 —— "
                "这个名字被同一个 workspace 里的**另一个 provider** 占用了。"
                "换一个名字（或先处理那条同名凭据）后重跑",
                status=exc.status,
            ) from exc
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

    # ── 外部知识库 dataset（#1001）──────────────────────────────────────

    def delete_dataset(self, workspace_id: str, dataset_id: str) -> None:
        """删一条 dataset（**不可逆**，Dify 没有恢复入口）。

        Dify 的 `DELETE /console/api/datasets/{id}` 回 **204 空 body**，所以这里不用
        `request_json` —— 那会在 decode 阶段抛 `JSONDecodeError`（2026-10-23 实测）。
        走骨架的 `open_response`：只看状态码，不读 body。这与
        `knowledge_retrieval.fetch_media` 处理二进制响应是同一个手法。

        它顺带删掉 `external_knowledge_bindings` 里对应的行（`dataset_service.py:1371`），
        所以不会留下"指向不存在 dataset 的 binding"。

        **调用方负责判断该不该删。** 这个方法只做"发请求"，不猜"这个库是不是真的没了" ——
        那个判断需要上游可达性，而那个事实在 `dify_knowledge_datasets.run()` 的调用方手里。
        """
        spec = RequestSpec(
            url=join_url(self.settings.base_url, f"/console/api/datasets/{dataset_id}"),
            method="DELETE",
            headers=self._headers(workspace_id),
            timeout=self.settings.timeout,
        )
        try:
            with open_response(spec) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            raise ConsoleApiError(
                "DIFY_DATASET_DELETE_FAILED", f"dataset {dataset_id}: HTTP {exc.code}"
            ) from exc
        except TRANSPORT_FAILURES as exc:
            raise ConsoleApiError("DIFY_DATASET_DELETE_FAILED", f"dataset {dataset_id}: {exc}") from exc
        if status not in (200, 204):
            raise ConsoleApiError("DIFY_DATASET_DELETE_FAILED", f"dataset {dataset_id}: HTTP {status}")

    def list_datasets(self, workspace_id: str) -> list[dict[str, Any]]:
        """这个 workspace 的**全部** dataset，含 ``only_me``。

        ``include_all=true`` 只在调用者是 owner 时生效（``DatasetService.get_datasets``
        的分支），而 admin key 正是按 ``X-WORKSPACE-ID`` 取 owner 行（``ext_login.py``），
        所以这里拿得到 owner 视角 —— 少了它，一条 ``only_me`` 的 dataset 在同步看来
        "不存在"，于是每次跑都再建一条同 id 的。

        翻页判据用响应的 ``has_more``，与 :meth:`app_names` 同一条理由：读第 1 页就断言
        "不存在"会让第 2 页上的库被重复创建。
        """
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self._call(
                workspace_id, f"/console/api/datasets?page={page}&limit=100&include_all=true"
            )
            body = payload if isinstance(payload, dict) else {}
            out.extend(item for item in body.get("data") or [] if isinstance(item, dict))
            if not body.get("has_more"):
                return out
            page += 1
            if page > MAX_DATASET_PAGES:
                raise ConsoleApiError(
                    "DIFY_DATASETS_UNREADABLE",
                    f"dataset 列表超过 {MAX_DATASET_PAGES} 页仍未结束；停止翻页，避免无界请求",
                )

    def create_external_dataset(
        self,
        workspace_id: str,
        *,
        name: str,
        external_knowledge_id: str,
        external_knowledge_api_id: str,
    ) -> str:
        """建一条 external dataset；返回它的 id。

        **这个接口不接受 ``permission``**（``ExternalDatasetCreatePayload`` 没有这个字段），
        落库默认 ``only_me`` —— 也就是"只有创建者看得见"。建完必须紧跟一次
        :meth:`update_external_dataset`，否则编排页里选不到它。
        """
        payload = self._call(
            workspace_id,
            "/console/api/datasets/external",
            method="POST",
            body={
                "name": name,
                "external_knowledge_id": external_knowledge_id,
                "external_knowledge_api_id": external_knowledge_api_id,
            },
        )
        dataset_id = str((payload or {}).get("id") or "") if isinstance(payload, dict) else ""
        if not dataset_id:
            raise ConsoleApiError(
                "DIFY_DATASET_CREATE_UNREADABLE",
                f"[workspace {workspace_id}] 建 dataset {external_knowledge_id} 回了成功但没有 id",
            )
        return dataset_id

    def update_external_dataset(
        self,
        workspace_id: str,
        dataset_id: str,
        *,
        name: str,
        external_knowledge_id: str,
        external_knowledge_api_id: str,
        permission: str = "all_team_members",
        external_retrieval_model: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """改一条 external dataset。

        **两个外部字段每次都带全。** ``_update_external_dataset`` 里
        ``external_knowledge_id`` / ``external_knowledge_api_id`` 缺任一都是
        ``ValueError``（不是"保持不变"），所以"只改权限"也必须回填它们。

        **``external_retrieval_model`` 每次也要带**（#1002 真机抓到）。Dify 检索 external
        dataset 时把 ``dataset.retrieval_model`` **原样**交给
        ``fetch_external_knowledge_retrieval``，而那里对它直接 ``.get(...)``：
        ``retrieval_model`` 是 SQL NULL 时，**每一次检索**都炸
        ``AttributeError: 'NoneType' object has no attribute 'get'``，Dify 把异常吞成
        "没有检索结果" —— 表现为智能体回答"暂时无法查询到"，而列表、命中测试、
        权限全都正常。``create_external_dataset`` **不写**这个字段，所以不补它就永远是
        NULL。UI 的 Settings 表单也会带这一块（``use-form-state.ts``），
        所以补上它与"人点过保存"是同一个状态。

        ``permission`` 显式给 ``all_team_members``：Dify 的 ``only_me`` 只对 maintainer
        可见，那正是编排页「未找到知识库」的成因。
        """
        body: dict[str, Any] = {
            "name": name,
            "permission": permission,
            "external_knowledge_id": external_knowledge_id,
            "external_knowledge_api_id": external_knowledge_api_id,
        }
        if external_retrieval_model is not None:
            body["external_retrieval_model"] = external_retrieval_model
        payload = self._call(
            workspace_id,
            f"/console/api/datasets/{dataset_id}",
            method="PATCH",
            body=body,
        )
        return payload if isinstance(payload, dict) else {}


__all__ = [
    "ConsoleApiError",
    "ConsoleApiSettings",
    "DifyConsoleAdmin",
    "IMPORT_MODE_YAML_CONTENT",
    "PLUGIN_INSTALL_PKG_PATH",
]
