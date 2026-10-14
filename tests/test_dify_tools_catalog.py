"""The 3A tool catalog: the note endpoint, the OpenAPI artifact, and the pull rule (#774).

Three properties, each one a way this could look done while being useless:

1. **The endpoint is worth nothing to a leaked key.** Dify's custom-API-tool
   credential is workspace-shared, so the test is not "it needs a key" but "with
   the key it still cannot read anything" — driven by a data source that fails
   the moment it is touched.
2. **The artifact an operator pastes is the artifact the code defines.** A
   checked-in OpenAPI file drifts silently; the test reads it back and compares
   operation ids with ``ALL_TOOL_NAMES``.
3. **The pull rule keys on the provider's UUID, not on a name it cannot see.**
   Dify's tool entry carries no provider name (``AgentToolEntity``), and the UUID
   is the ``ApiToolProvider`` row id, so the rule only works if it is resolved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from aiops_diagnostics.agent_contracts import ALL_TOOL_NAMES
from aiops_diagnostics.dify_tools_catalog import (
    AI_OPS_PROVIDER_NAME,
    CATALOG_NOTE,
    DeclaredTool,
    openapi_document,
    partition_aiops_tools,
)
from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver, PlatformRoleRecord
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore

TOOLS_KEY = "dify-tools-key-0123456789"
KNOWLEDGE_KEY = "dify-knowledge-key-0123456789"
PUBLISH_KEY = "dify-publish-key-0123456789"
CONSOLE_KEY = "dify-console-key-0123456789"

ROOT = Path(__file__).parents[1]
OPENAPI_PATH = ROOT / "ops" / "dify-tools" / "aiops-tools.openapi.json"

AI_OPS_UUID = "3f1b0c8e-6a1d-4f0e-9c2b-7d5a1e6f8b03"


class _Runtime:
    """Only what the adapter reads on this route. Nothing here can fetch data."""

    kb_search_client = None
    media_signer = None

    def shutdown(self) -> None:  # pragma: no cover - not exercised
        pass


class _Directory:
    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        return (PlatformRoleRecord("B-1", "C-1", "T-1", "admin"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        return ()


def _client(tmp_path: Path, **overrides: Any) -> TestClient:
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        **overrides,
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=_Runtime(),  # type: ignore[arg-type]
        caller_resolver=None,  # type: ignore[arg-type]
        order_authorizer=None,  # type: ignore[arg-type]
        platform_resolver=PlatformIdentityResolver(_Directory()),
        faq_catalog=FAQCatalog.bundled(),
    )
    return TestClient(app)


def test_the_note_route_needs_its_own_key_and_returns_one_fixed_string(tmp_path) -> None:
    """200 + 同一段固定文本；缺失 401、错误 403、**别的** Dify 钥匙都不通用。"""
    client = _client(
        tmp_path,
        dify_tools_api_key=TOOLS_KEY,
        dify_knowledge_api_key=KNOWLEDGE_KEY,
        dify_knowledge_bindings="kb-1:T-1:KB-A",
        dify_publish_api_key=PUBLISH_KEY,
        dify_console_base_url="http://dify.invalid:10008",
        dify_console_api_key=CONSOLE_KEY,
    )
    ok = client.post("/v1/dify/tools/order_snapshot", headers={"Authorization": f"Bearer {TOOLS_KEY}"})
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"tool": "order_snapshot", "note": CATALOG_NOTE}

    # 七个工具回的是**同一段**文本：它说的是"本工具只在 AI-Ops 运行时执行"，与具体哪个
    # 工具无关；一旦它开始按工具分叉，就意味着这里有了"每个工具能干什么"的知识。
    notes = {
        client.post(f"/v1/dify/tools/{tool}", headers={"Authorization": f"Bearer {TOOLS_KEY}"}).json()["note"]
        for tool in ALL_TOOL_NAMES
    }
    assert notes == {CATALOG_NOTE}

    assert client.post("/v1/dify/tools/order_snapshot").status_code == 401
    assert (
        client.post(
            "/v1/dify/tools/order_snapshot", headers={"Authorization": "Bearer wrong-key"}
        ).status_code
        == 403
    )
    for other in (KNOWLEDGE_KEY, PUBLISH_KEY, CONSOLE_KEY):
        assert (
            client.post(
                "/v1/dify/tools/order_snapshot", headers={"Authorization": f"Bearer {other}"}
            ).status_code
            == 403
        ), "工具目录的钥匙必须是独立的一把 —— 与其它面同值等于把两件事绑成一个权限"


def test_an_unknown_tool_is_not_found(tmp_path) -> None:
    """七个之外的名字是 404 —— 固定回一段文本的路由，路径本身就是唯一可观察的差异。"""
    client = _client(tmp_path, dify_tools_api_key=TOOLS_KEY)
    assert (
        client.post("/v1/dify/tools/drop_table", headers={"Authorization": f"Bearer {TOOLS_KEY}"}).status_code
        == 404
    )


def test_an_unconfigured_key_means_the_route_is_not_there(tmp_path) -> None:
    """未配置 ⇒ 404（不是"不需要钥匙"）—— 与另两条 Dify 路由同一条机制。"""
    client = _client(tmp_path)
    assert client.post("/v1/dify/tools/order_snapshot").status_code == 404
    assert (
        client.post(
            "/v1/dify/tools/order_snapshot", headers={"Authorization": f"Bearer {TOOLS_KEY}"}
        ).status_code
        == 404
    )


def test_the_tools_key_cannot_reuse_another_dify_key(tmp_path) -> None:
    """同值是**启动错误**，不是运行时某个先到先得的匹配。"""
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        dify_tools_api_key=KNOWLEDGE_KEY,
        dify_knowledge_api_key=KNOWLEDGE_KEY,
        dify_knowledge_bindings="kb-1:T-1:KB-A",
    )
    with pytest.raises(ValueError, match="AIOPS_GATEWAY_DIFY_TOOLS_API_KEY must differ"):
        settings.validate()


def test_the_checked_in_openapi_document_is_the_one_the_code_defines() -> None:
    """粘贴进 Dify 的那份文档 == 代码定义的那份，且 operationId 集合恰是七个工具名。"""
    on_disk = json.loads(OPENAPI_PATH.read_text("utf-8"))
    assert on_disk == openapi_document(), (
        f"{OPENAPI_PATH.relative_to(ROOT)} 与 openapi_document() 不一致 —— "
        '重新生成它（`uv run python -c "..."` 见文件头注释）而不是手改，手改必然漂'
    )

    operations = {method["operationId"] for path in on_disk["paths"].values() for method in path.values()}
    assert operations == set(ALL_TOOL_NAMES)


def test_the_openapi_operations_declare_no_parameters() -> None:
    """不声明任何参数 —— 这条是结构性的，不是风格。

    我们的工具输入全部来自运行上下文（ADR-0019），从不来自模型。OpenAPI 里留空是让
    "运营无法通过工具参数钉住一个订单号"成为**结构事实**：Dify 没有可提示模型去填的东西。
    """
    document = openapi_document()
    for path, item in document["paths"].items():
        for method in item.values():
            assert "parameters" not in method, f"{path} 声明了参数 —— 工具输入不来自模型"
            assert "requestBody" not in method, f"{path} 声明了请求体"


#: 一个 aiops 提供方下的条目 —— 形状照 Dify 1.17.1 的 ``AgentToolEntity``（无 provider_name）。
def _entry(
    tool: str, *, provider: str = AI_OPS_UUID, kind: str = "api", enabled: bool = True
) -> DeclaredTool:
    return DeclaredTool(provider_type=kind, provider_id=provider, tool_name=tool, enabled=enabled)


def test_the_aiops_provider_maps_every_tool_without_a_stored_uuid() -> None:
    """七个工具按 provider 的 UUID 匹配，直接映射到同名 ToolName。

    **没有任何一处存过那个 UUID** —— 它是参数，是每次拉取当场从控制台解析出来的。
    """
    declared = tuple(_entry(tool) for tool in ALL_TOOL_NAMES)
    allowed, unmapped = partition_aiops_tools(declared, aiops_provider_id=AI_OPS_UUID, bindings={})
    assert allowed == ALL_TOOL_NAMES
    assert unmapped == ()


def test_a_disabled_tool_is_dropped_exactly_like_a_removed_one() -> None:
    """``enabled=false`` 不进白名单（#772 E：「暂时关掉」与「删除」效果一致）。"""
    declared = (
        _entry("order_snapshot"),
        _entry("known_runbook", enabled=False),
        _entry("redis_sync"),
    )
    allowed, unmapped = partition_aiops_tools(declared, aiops_provider_id=AI_OPS_UUID, bindings={})
    assert allowed == ("order_snapshot", "redis_sync")
    assert unmapped == (), "被关掉的工具不是'翻不出来'，它只是没被选中"


def test_an_unknown_name_under_the_aiops_provider_is_refused_not_mapped() -> None:
    """aiops 下的未知 tool_name ⇒ 报出该条目（绝不按字面映射进生产）。"""
    declared = (_entry("order_snapshot"), _entry("drop_everything"))
    allowed, unmapped = partition_aiops_tools(declared, aiops_provider_id=AI_OPS_UUID, bindings={})
    assert allowed == ("order_snapshot",)
    assert [entry.tool_name for entry in unmapped] == ["drop_everything"]


def test_another_provider_still_goes_through_the_explicit_binding_table() -> None:
    """其他提供方**只能**经显式登记表 —— 名字相等不代表语义相同这条不变。"""
    declared = (
        _entry("timezone_conversion", provider="time", kind="builtin"),
        _entry("order_snapshot", provider="time", kind="builtin"),
    )
    allowed, unmapped = partition_aiops_tools(
        declared,
        aiops_provider_id=AI_OPS_UUID,
        bindings={"time/timezone_conversion": "known_runbook"},
    )
    assert allowed == ("known_runbook",)
    # 注意第二条：它是**别的提供方**下的 `order_snapshot`，与我们的同名工具不是同一个东西。
    assert [entry.identity for entry in unmapped] == ["time/order_snapshot"]


def test_the_same_dsl_under_a_different_provider_uuid_resolves_the_same_way() -> None:
    """换一个 provider UUID（重建 Dify 实例/重建提供方）⇒ 结果不变。

    这条就是"映射不依赖 UUID"的可执行形式：UUID 是输入，不是被记住的东西，所以重建之后
    重新解析出来的新 UUID 一样能用。
    """
    declared = (_entry("order_snapshot"), _entry("redis_sync"))
    first, _ = partition_aiops_tools(declared, aiops_provider_id=AI_OPS_UUID, bindings={})
    second, _ = partition_aiops_tools(
        (
            _entry("order_snapshot", provider="b2c3d4e5-0000-1111-2222-333344445555"),
            _entry("redis_sync", provider="b2c3d4e5-0000-1111-2222-333344445555"),
        ),
        aiops_provider_id="b2c3d4e5-0000-1111-2222-333344445555",
        bindings={},
    )
    assert first == second


def test_a_missing_aiops_provider_refuses_rather_than_mapping() -> None:
    """工作区里没有 aiops 提供方 ⇒ 一条都映射不出来（不是"跳过"）。"""
    allowed, unmapped = partition_aiops_tools((_entry("order_snapshot"),), aiops_provider_id="", bindings={})
    assert allowed == ()
    assert [entry.tool_name for entry in unmapped] == ["order_snapshot"]


def test_no_declared_tools_keeps_the_documented_default() -> None:
    """什么都不声明 ⇒ 空结果，由调用方沿用 ADR-0019 默认（全部七个）。"""
    allowed, unmapped = partition_aiops_tools((), aiops_provider_id=AI_OPS_UUID, bindings={})
    assert allowed == ()
    assert unmapped == ()


def test_the_catalog_name_and_the_declared_tools_meet_in_the_pull() -> None:
    """端到端：一份真 DSL 声明 aiops 的七个工具 → 白名单就是那七个。

    走 ``map_dsl_to_config`` 真路径（不是只测 ``partition_aiops_tools``），因为真正会出
    问题的是"两边各自都对、接起来不对"。
    """
    from aiops_diagnostics.dify_dsl_pull import _resolve_tools, map_dsl_to_config

    dsl = yaml.safe_load(_MINIMAL_DSL)
    dsl["model_config"]["agent_mode"] = {
        "enabled": True,
        "strategy": "router",
        "tools": [
            {
                "provider_type": "api",
                "provider_id": AI_OPS_UUID,
                "tool_name": tool,
                "tool_parameters": {},
            }
            for tool in ("order_snapshot", "known_runbook")
        ],
    }
    # ``_resolve_tools`` 是桥本体：AI_OPS_PROVIDER_NAME 由**调用方**（拉取路径）解析成 UUID，
    # 这里直接用 UUID 驱动，检查的是"拿到真实解析结果之后"的行为。
    from aiops_diagnostics.dify_dsl_pull import _declared_tools

    entries = _declared_tools(dsl["model_config"])
    assert _resolve_tools(entries, aiops_provider_id=AI_OPS_UUID, registry={}) == (
        "order_snapshot",
        "known_runbook",
    )
    assert AI_OPS_PROVIDER_NAME == "aiops"
    # 并且 map 也只认这两条
    config = map_dsl_to_config(
        yaml.safe_dump(dsl, allow_unicode=True),
        agent_type="customer",
        tool_resolver=lambda names: _resolve_tools(names, aiops_provider_id=AI_OPS_UUID, registry={}),
    )
    assert config.allowed_tools == ("order_snapshot", "known_runbook")


_MINIMAL_DSL = """\
version: 0.7.0
kind: app
app:
  name: catalog-fixture
  mode: chat
model_config:
  model:
    provider: openai
    name: gpt-4o
    mode: chat
    completion_params: {}
  pre_prompt: 你是运维助手，只依据证据回答。
  prompt_template:
    - role: system
      text: 你是运维助手。
  agent_mode:
    enabled: false
    prompt: null
    strategy: null
    tools: []
"""
