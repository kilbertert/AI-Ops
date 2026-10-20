"""构造身份启动器的判据（不联网、不连库）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "tools/m2_close/m3live/identity_launcher.py"


def _load(monkeypatch, *, token="t0ken", tenant="TENANT-A", c_user="USER-A"):
    monkeypatch.setenv("AIOPS_M2_LOCAL_TOKEN", token)
    monkeypatch.setenv("AIOPS_M2_TENANT", tenant)
    monkeypatch.setenv("AIOPS_M2_C_USER", c_user)
    spec = importlib.util.spec_from_file_location("identity_launcher_under_test", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_token_is_the_only_way_in(monkeypatch):
    """错令牌直接拒绝，且用的是既有的错误码。"""
    module = _load(monkeypatch)
    resolver = module.ConstructedIdentity()
    from aiops_diagnostics.caller_auth import CallerAuthError

    with pytest.raises(CallerAuthError):
        resolver.resolve("wrong", required_scope="aiops:orders:read")


def test_constructed_scope_is_self_and_carries_the_declared_user(monkeypatch):
    """构造的 `ScopeContext` 必须是 ``self`` 范围 + 声明的租户/C 端用户。

    真实 `ScopedOrderAuthorizer` 会拿这个范围去业务库核对订单归属 —— 范围写错的话，
    真实授权器会给出 404，链路根本不会往下走。所以这一条是「构造身份」能被接受的前提。
    """
    module = _load(monkeypatch, tenant="TENANT-A", c_user="USER-A")
    scope = module.ConstructedIdentity().resolve("t0ken", required_scope="aiops:orders:read")

    assert scope.data_scope.type == "self"
    assert scope.effective_tenant_id == "TENANT-A"
    assert scope.subject.c_user_id == "USER-A"
    assert scope.delegated is False
    assert scope.permissions == {"aiops:orders:read"}, "只该有读取 scope，不该照给"


@pytest.mark.parametrize("scope", ["aiops:agents:manage", "aiops:shortcuts:manage", "aiops:diag:read"])
def test_management_scopes_are_refused(monkeypatch, scope):
    """握手令牌**不得**开管理面。

    早先按 `required_scope` 原样授予，于是「只读验证」的令牌能进 `/v1/agents` 与
    `/v1/shortcuts` 的管理路由。现在用白名单：不在集合里的 scope 直接拒绝，
    而且是拒绝**整个身份**，不是少给一个权限（那会变成 403 而不是 401，语义不同）。
    """
    module = _load(monkeypatch)
    from aiops_diagnostics.caller_auth import CallerAuthError

    with pytest.raises(CallerAuthError):
        module.ConstructedIdentity().resolve("t0ken", required_scope=scope)


def test_launcher_never_installs_a_synthetic_authorizer():
    """源码级断言：启动器**不得**传入 `order_authorizer`。

    这条不是风格检查。`create_gateway_app(order_authorizer=…)` 一旦被传入，真实授权器
    就被替身顶掉，而整条链路仍然「跑通」—— 那是本文件存在的意义被悄悄抹掉。
    """
    source = LAUNCHER.read_text(encoding="utf-8")
    call = source.split("create_gateway_app(", 1)[1].split(")", 1)[0]
    assert "order_authorizer" not in call, "启动器替换了订单授权器，真实判定会失效"
    # 反面对照：合成验收的启动器**确实**替换它 —— 两者不该被混为一谈。
    synthetic = (REPO / "tools/m2_close/gateway_launcher.py").read_text(encoding="utf-8")
    assert "order_authorizer=SyntheticOrderAuthorizer()" in synthetic


def test_isolation_guard_runs_before_the_app_is_built():
    """隔离门禁必须在 `create_gateway_app` **之前**：后者一打开库就写状态。"""
    source = LAUNCHER.read_text(encoding="utf-8")
    # 只看代码行：docstring 与注释里也会提到这两个名字，按全文比会得到假的顺序。
    code = [
        line
        for line in source.split("def main()", 1)[1].splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    text = "\n".join(code)
    assert text.index("enforce_from_env()") < text.index("create_gateway_app(")
