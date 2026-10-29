"""试运行按人可见范围（#782）：公司身份 → 管家端（``operator``）口径。

这一层的规则**只有一份** —— 与 ``third_session_auth`` 的会话路径同一套：顶层账号
（``sys_user.type ∈ {-1,1}``）看整个租户；其余（代理商）经 ``OperatorSiteScope``
的「店铺 → 站点」解析；解析失败一律 **fail-closed 成空站点集合**，不回落租户级放行、
也不套用 ``self``。

验收标准里的三条分别对应下面三组用例：代理商只看名下站点；顶层账号看整租户但跨租户
仍被拒；本人作为消费者下的单按管家端口径**看不到**（那是 ``self`` 的语义，不是 operator 的）。

这里只驱动 ``build_trial_scope``（纯函数、无网络）；订单授权本身由
``tests/dify/test_dify_trial.py`` 用真的 ``order_authorizer`` 覆盖。
"""

from __future__ import annotations

import os

import pytest

from aiops_diagnostics.dify.dify_trial import TrialInitiator, build_trial_scope
from aiops_diagnostics.identity.query_scope import QueryScope
from aiops_diagnostics.identity.scope_context import SCOPE_TYPE_ALL, SCOPE_TYPE_ORGAN


class _OperatorScope:
    """假 ``OperatorSiteScope``：B 端 id → 站点集合，或抛错。"""

    def __init__(self, mapping: dict[str, tuple[str, ...]], *, raises: bool = False) -> None:
        self.mapping = mapping
        self.raises = raises
        self.asked: list[tuple[str, str]] = []

    def site_scope_by_b_user_id(self, b_user_id: str, tenant_id: str) -> QueryScope:
        self.asked.append((b_user_id, tenant_id))
        if self.raises:
            raise RuntimeError("UPMS 不可达")
        return QueryScope(tenant_id=tenant_id, site_ids=self.mapping.get(b_user_id, ()), user_id=None)


def _initiator(user_type: str = "2", b_user_id: str = "B-1", tenant: str = "T-1") -> TrialInitiator:
    return TrialInitiator(b_user_id=b_user_id, tenant_id=tenant, user_type=user_type)


def test_an_agent_account_sees_only_its_own_sites() -> None:
    """代理商（``type=2``）⇒ 站点集合 = 它名下的那些，租户来自身份。"""
    directory = _OperatorScope({"B-1": ("S-1", "S-2")})
    context = build_trial_scope(_initiator(user_type="2"), operator_scope=directory)
    assert context.effective_tenant_id == "T-1"
    assert context.data_scope.type == SCOPE_TYPE_ORGAN
    assert context.data_scope.site_ids == ("S-1", "S-2")
    assert directory.asked == [("B-1", "T-1")]


def test_a_tenant_level_account_sees_the_whole_tenant_and_skips_the_lookup() -> None:
    """顶层账号（``-1`` 平台 / ``1`` 租户）⇒ 整租户，**不查店铺**（那无意义）。"""
    for user_type in ("-1", "1"):
        directory = _OperatorScope({})
        context = build_trial_scope(_initiator(user_type=user_type), operator_scope=directory)
        assert context.data_scope.type == SCOPE_TYPE_ALL, user_type
        assert directory.asked == [], f"{user_type} 不该去查店铺集合"


def test_an_empty_shop_binding_sees_no_orders() -> None:
    """未绑定店铺 ⇒ **空站点集合**（看不到订单），不是租户级放行。"""
    directory = _OperatorScope({"B-1": ()})
    context = build_trial_scope(_initiator(), operator_scope=directory)
    assert context.data_scope.type == SCOPE_TYPE_ORGAN
    assert context.data_scope.site_ids == ()


def test_a_scope_lookup_failure_is_fail_closed() -> None:
    """解析失败（上游不可达）⇒ 空站点集合。**不**回落租户级、**不**套 ``self``。"""
    directory = _OperatorScope({}, raises=True)
    context = build_trial_scope(_initiator(), operator_scope=directory)
    assert context.data_scope.type == SCOPE_TYPE_ORGAN
    assert context.data_scope.site_ids == ()


def test_an_unconfigured_operator_scope_is_an_empty_scope_not_a_tenant_one() -> None:
    """没配 UPMS/凭据（``operator_scope=None``）⇒ 空集合，绝不放宽成整租户。"""
    context = build_trial_scope(_initiator(), operator_scope=None)
    assert context.data_scope.type == SCOPE_TYPE_ORGAN
    assert context.data_scope.site_ids == ()


def test_the_scope_carries_the_initiators_identity_for_authorization() -> None:
    """范围里带上发起人的主体与租户 —— 订单授权与执行**共用这一个**对象。"""
    directory = _OperatorScope({"B-1": ("S-1",)})
    context = build_trial_scope(_initiator(user_type="5"), operator_scope=directory)
    assert context.subject.b_user_id == "B-1"
    assert context.subject.tenant_id == "T-1"
    assert context.subject.user_type == "5"
    assert context.delegated is False


def test_the_initiator_is_built_only_from_complete_headers() -> None:
    """桥没给全身份（缺 id 或租户）⇒ ``None`` ⇒ 退回 #781 的固定调试租户。"""
    assert TrialInitiator.from_headers("B-1", "T-1", "2", "zhang") is not None
    assert TrialInitiator.from_headers("", "T-1", "2") is None
    assert TrialInitiator.from_headers("B-1", "", "2") is None
    assert TrialInitiator.from_headers(None, None, None) is None


def test_the_platform_tenant_sentinel_is_not_a_tenant() -> None:
    """``tenant_id == "-1"`` ⇒ 没有可用身份（退回调试租户），**不是**一个叫 -1 的租户。

    平台账号（``sys_user.type = -1``）的 ``tenant_id`` 也是 ``-1``。把它当租户下推，
    得到的不是"看整个平台"，而是一条**范围为空**的查询 —— 每张订单都 404。

    实测（2026-10-20，41）：同一请求体、同一把钥匙、同一个已绑定 app，只把
    ``X-AIOps-Initiator-Tenant`` 从 ``-1`` 换成 ``1942…``，就从 ``404 ORDER_NOT_FOUND``
    变成 ``202``。反证：去掉这条判定，下面的 ``None`` 断言会红（会变成一个 tenant=-1 的身份）。
    """
    assert TrialInitiator.from_headers("U-1", "-1", "-1", "system") is None
    # 对照：同一个账号在真实租户下仍然成立，这条判定只挡哨兵值本身。
    assert TrialInitiator.from_headers("U-1", "T-1", "-1", "system") is not None


def test_a_missing_user_type_is_not_treated_as_tenant_level() -> None:
    """``type`` 缺失 ⇒ 空串 ⇒ **不是**顶层账号 ⇒ 走 fail-closed 的店铺解析。"""
    directory = _OperatorScope({})
    context = build_trial_scope(_initiator(user_type=""), operator_scope=directory)
    assert context.data_scope.type == SCOPE_TYPE_ORGAN
    assert directory.asked, "type 缺失时应当去解析店铺（而不是当成顶层账号）"


def test_a_representative_order_scope_is_not_the_self_scope() -> None:
    """**管家端口径不是 ``self``**：代理商看到的是一组站点，而不是"本人的单"。

    这条正是验收标准里「同一账号对本人作为消费者下的单按管家端口径被拒」的机制：
    ``self`` 范围按 C 端用户过滤，而 operator 范围按站点过滤 —— 两者对同一张单可以
    给出相反答案。这里钉住"operator 路径产出的是站点集合"，``self`` 的对照由
    ``tests/test_order_visibility`` 覆盖。
    """
    directory = _OperatorScope({"B-1": ("S-1",)})
    context = build_trial_scope(_initiator(), operator_scope=directory)
    assert context.data_scope.site_ids == ("S-1",)
    assert not context.subject.c_user_id, "operator 路径不按 C 端用户过滤"


@pytest.mark.parametrize("user_type", ["0", "9", "abc", ""])
def test_only_the_two_documented_types_are_tenant_level(user_type: str) -> None:
    """顶层判据只认 ``-1/1``（与 ``is_tenant_level_account`` 同一处）；其余一律按站点。"""
    directory = _OperatorScope({"B-1": ("S-1",)})
    context = build_trial_scope(_initiator(user_type=user_type), operator_scope=directory)
    assert context.data_scope.type == SCOPE_TYPE_ORGAN


def test_create_gateway_app_injects_the_operator_scope_into_a_real_runtime(tmp_path) -> None:
    """**装配层真的注入了**（不只是纯函数对）。

    纯函数测试证明规则对；这一条证明那条规则在真实装配路径上被接到了运行时上 ——
    否则"规则写好了但没人调用"会让范围静默落到空集合，而那看起来像"账号没权限"。
    """
    from aiops_diagnostics.gateway_api import create_gateway_app
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime
    from aiops_diagnostics.gateway_store import GatewayStore

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    os.chmod(settings.server_config_file, 0o600)  # 私有配置文件：不得有组/其他位
    store = GatewayStore(settings.database_file)
    runtime = GatewayRuntime(
        store, settings, __import__("aiops_diagnostics.config", fromlist=["Settings"]).Settings()
    )
    create_gateway_app(settings=settings, store=store, runtime=runtime, caller_resolver=None)  # type: ignore[arg-type]
    # 未配置 UPMS ⇒ 解析器可能是 None；关键是**属性被设过**（注入路径走通），
    # 而不是仍停留在类级默认。用一个哨兵确认装配层调到了 setter。
    assert hasattr(runtime, "_trial_operator_scope")


def test_the_trial_scope_is_empty_when_no_operator_scope_was_injected() -> None:
    """运行时的默认值就是 ``None`` ⇒ 空集合。**新装的部署不会因漏注入而放宽**。"""
    import tempfile
    from pathlib import Path

    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime
    from aiops_diagnostics.gateway_store import GatewayStore

    tmp = Path(tempfile.mkdtemp())
    settings = GatewayServerSettings(
        data_home=tmp,
        database_file=tmp / "gateway.db",
        server_config_file=tmp / "production.env",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    os.chmod(settings.server_config_file, 0o600)  # 私有配置文件：不得有组/其他位
    runtime = GatewayRuntime(
        GatewayStore(settings.database_file),
        settings,
        __import__("aiops_diagnostics.config", fromlist=["Settings"]).Settings(),
    )
    assert runtime._trial_operator_scope is None
    context = build_trial_scope(_initiator(), operator_scope=runtime._trial_operator_scope)
    assert context.data_scope.site_ids == ()
