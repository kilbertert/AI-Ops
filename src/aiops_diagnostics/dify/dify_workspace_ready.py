"""`aiops-admin ensure-ready`：让建出来的 workspace **不再是空壳**（#881）。

`ensure-workspace`（#880）把 workspace 建出来，状态停在 ``created``。这一步把它推到
``ready`` —— 三步，全部成功才算：

1. **装插件**：新 workspace 只有 6 行插件升级策略，一个插件都没有 —— 没有模型插件，
   里面建得出 app 也跑不起来。
2. **写这个 workspace 自己的 provider 凭据**：Dify 的数据模型里**没有共享层**
   （``providers.tenant_id`` 非空、凭据用每租户一把 RSA 密钥加解密），所以复制行到
   别的 workspace 也解不开。只能每个 workspace 各写一份。
3. **导模板 DSL**：模板在仓库里，**只含结构与工具白名单，`model_config` 留空**。

## 第 3 步失败**不回滚**

设计（ADR-0059 §5.1）明确：模板导入失败**只记状态，不删 workspace、不回滚**。理由是
template 缺了不影响登录 —— 人进得去、能改；而重建 workspace 会造永久孤儿（Dify 没有删
workspace 的 API）。所以它的失败写在 ``last_error`` 里，状态停在 ``created``，重跑可补齐。

## 凭据从**部署配置**读，代码里没有密钥

``AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_API_KEY`` 由运维在主机上填。缺它 ⇒ **拒绝并说明
去哪里填**，不猜、不硬编码、不回落任何"示例 key"。Dify 会拿这把 key 去做真实校验
（2026-10-21 实测：假 key 回 400 ``Credentials validation failed``），所以假 key 走不通 ——
这正是它必须来自受控位置的原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aiops_diagnostics.dify.dify_console_admin import ConsoleApiError, DifyConsoleAdmin
from aiops_diagnostics.dify.dify_ensure_workspace import EnsureError
from aiops_diagnostics.dify.dify_workspaces import DifyWorkspaceError, DifyWorkspaces

#: 每个新 workspace 都要装的插件（``provider/name:version@sha``）。
#:
#: 取的是 36 上**admin workspace 实际装着的两个**（2026-10-21 实测）—— 不是猜的清单：
#: 新 workspace 要能跑起 app，就得有和能用的那个一样的两家 provider 插件。
DEFAULT_PLUGIN_IDENTIFIERS = (
    "langgenius/deepseek:0.0.24@9e12eef09973667f0eaf4f082af883167e1de88740b22e71e71488d020c3778a",
    "langgenius/openai:1.0.7@f36c3761167b3af0bfb4975b728fe3cd126342d076e1ec5359e6d4f92557d3d4",
)

#: 模板 DSL 里 ``model.provider`` 与我们的凭据绑定：Dify 的 provider 名是
#: ``langgenius/<plugin>/<provider>``，而 DSL 里写的是 provider 的短名。
TEMPLATE_PROVIDER = "langgenius/deepseek/deepseek"
TEMPLATE_PROVIDER_SHORT = "deepseek"

#: 模板 DSL 随包发布（**不是** ``ops/``）：41 上的产物只含 ``src/``（部署脚本 `tar` 的
#: 就是那一个目录），模板放 ``ops/`` 会在 41 上"文件不存在"，而它恰恰是 41 才要跑的步骤。
#: 与 ``faq_catalog.json`` 同一条规矩：`importlib.resources` 读，跟着 wheel 走。
TEMPLATE_RESOURCE = "dify_workspace_template.dsl.yml"

#: 导进去之后那个 app 的名字 —— 也是"导过了就跳过"的判据。
DEFAULT_APP_NAME = "aiops-workspace-template"

#: 凭据在 workspace 里的名字。固定值，好让"已存在"成为一个可判定的状态。
CREDENTIAL_NAME = "aiops-workspace"


def bundled_template() -> str:
    """模板 DSL 的正文（随包发布的那一份）。"""
    from importlib.resources import files

    return files("aiops_diagnostics.dify").joinpath(TEMPLATE_RESOURCE).read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class ReadySettings:
    """让一个 workspace 变可用所需的一切。**没有一样是密钥本身**（钥匙在部署配置里）。"""

    console_base_url: str
    console_api_key: str = field(repr=False, default="")
    #: 写进每个新 workspace 的 provider 凭据（来自 ``AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_API_KEY``）。
    model_api_key: str = field(repr=False, default="")
    #: 这个 provider 的 endpoint；deepseek 官方端点是默认值。
    model_endpoint: str = "https://api.deepseek.com/v1"
    plugin_identifiers: tuple[str, ...] = DEFAULT_PLUGIN_IDENTIFIERS
    timeout: float = 30.0

    def missing(self) -> list[str]:
        """缺哪些键。空列表 = 可以跑。

        分开列出而不是一句"配置不全"：运维要知道是**哪一行**没填，尤其是那把
        model key —— 它不是控制台钥匙，容易漏。
        """
        gaps: list[str] = []
        if not self.console_base_url:
            gaps.append("AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL")
        if not self.console_api_key:
            gaps.append("AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY")
        if not self.model_api_key:
            gaps.append("AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_API_KEY")
        return gaps


@dataclass(frozen=True, slots=True)
class ReadyOutcome:
    tenant_id: str
    environment: str
    workspace_id: str
    state: str
    plugins_installed: tuple[str, ...] = ()
    credential_written: bool = False
    #: 当前生效凭据是否**被我们切到了** `aiops-workspace`（已经是它则为 False）。
    credential_activated: bool = False
    app_id: str = ""
    note: str = ""
    #: 模板导入时 Dify 给的告警（``completed-with-warnings``）。**不是失败**，但要说出来 ——
    #: 它意味着"Dify 丢掉了 DSL 里它还原不了的部分"，而 ``ready`` 会被静默推上去。
    template_warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "environment": self.environment,
            "workspace_id": self.workspace_id,
            "state": self.state,
            "plugins_installed": list(self.plugins_installed),
            "credential_written": self.credential_written,
            "credential_activated": self.credential_activated,
            "app_id": self.app_id,
            "note": self.note,
            "template_warnings": list(self.template_warnings),
        }


def ensure_ready(
    *,
    tenant_id: str,
    environment: str,
    workspaces: DifyWorkspaces,
    console: DifyConsoleAdmin,
    settings: ReadySettings,
    template_yaml: str,
    app_name: str,
    log: Any = None,
) -> ReadyOutcome:
    """把 ``created`` 推到 ``ready``。**幂等**：每一步都先看"是不是已经有了"。

    顺序不能换：插件 → 凭据 → 模板。模板 DSL 会引用 provider，provider 没凭据时导入
    虽然能成功，但那个 app 一跑就报 ``ProviderTokenDoesNotExist`` —— 把失败推到了
    "运营点了一下才发现"的位置。
    """
    gaps = settings.missing()
    if gaps:
        raise EnsureError(
            "WORKSPACE_READY_CONFIG_MISSING",
            "让 workspace 可用需要这些配置：" + "、".join(gaps) + "。"
            "其中 AIOPS_GATEWAY_DIFY_WORKSPACE_MODEL_API_KEY 要由运维在主机上填写 —— "
            "它是写进每个 workspace 的模型凭据，代码里没有、也不该有默认值。",
        )

    try:
        workspace_id = workspaces.resolve(tenant_id, environment)
    except DifyWorkspaceError as exc:
        raise EnsureError(exc.code, exc.message) from exc

    try:
        installed = tuple(console.install_plugins(workspace_id, list(settings.plugin_identifiers)))
    except ConsoleApiError as exc:
        raise EnsureError(exc.code, f"装插件失败：{exc.message}") from exc
    if log is not None and installed:
        log(f"plugins installed into {workspace_id}: {', '.join(installed)}")

    # 凭据：已有的跳过。Dify 会拿它做真实校验，所以这里的失败是"这把 key 不能用"。
    try:
        wrote_credential = console.write_provider_credential(
            workspace_id,
            TEMPLATE_PROVIDER,
            credentials={"api_key": settings.model_api_key, "endpoint_url": settings.model_endpoint},
            name=CREDENTIAL_NAME,
        )
        # **写完还要让它生效。** Dify 按 ``created_at DESC`` 选当前凭据，所以在**迁移过的**
        # workspace 里，复制过来的旧凭据仍排在前面 —— 2026-10-22 在 41 上实测：凭据写成功、
        # 状态推到 ready，对话却 401（生效的是 key 已失效的 ``deepseek-official``）。
        activated = console.activate_provider_credential(workspace_id, TEMPLATE_PROVIDER, CREDENTIAL_NAME)
    except ConsoleApiError as exc:
        raise EnsureError(exc.code, f"写 provider 凭据失败：{exc.message}") from exc

    # 模板：失败**只记状态**（ADR-0059 §5.1）—— 不回滚、不删 workspace，重跑可补齐。
    app_id = ""
    warnings: tuple[str, ...] = ()
    try:
        if app_name not in console.app_names(workspace_id):
            app_id = console.import_app_dsl(workspace_id, yaml_content=template_yaml, name=app_name)
            warnings = tuple(getattr(console, "last_import_warnings", ()))
    except ConsoleApiError as exc:
        workspaces.mark_template_failed(tenant_id, environment, error=f"{exc.code}: {exc.message}")
        raise EnsureError(
            exc.code,
            f"模板 DSL 导入失败：{exc.message}。workspace 与凭据都已就位，重跑本命令可补齐这一步"
            "（这就是设计里「导入失败只记状态、不阻塞登录」的那一半）。",
        ) from exc

    workspaces.mark_ready(tenant_id, environment)
    return ReadyOutcome(
        tenant_id=tenant_id,
        environment=environment,
        workspace_id=workspace_id,
        state="ready",
        plugins_installed=installed,
        credential_written=wrote_credential,
        credential_activated=activated,
        app_id=app_id,
        note=(
            "ready（模板已存在，未重复导入）"
            if not app_id
            else (
                "ready，但 Dify 导入时报了告警（模板有它还原不了的部分）：" + "；".join(warnings)
                if warnings
                else "ready"
            )
        ),
        template_warnings=warnings,
    )


__all__ = [
    "CREDENTIAL_NAME",
    "DEFAULT_APP_NAME",
    "DEFAULT_PLUGIN_IDENTIFIERS",
    "ReadyOutcome",
    "ReadySettings",
    "TEMPLATE_PROVIDER",
    "TEMPLATE_PROVIDER_SHORT",
    "bundled_template",
    "ensure_ready",
]
