"""「调用者能做什么」的单一权威（#1040）。

能力（capability）规则曾有四种渲染：两条生产身份链把路由请求的
``required_scope`` **原样写回** ``context.permissions`` —— 于是连凭空拼出的
scope 也被「授予」；唯一在查的 UPMS 链只判「有没有任意权限」而不判「是不是这个」；
写进审计的 ``permissions`` 没有任何人验证过。现在每条链有显式档位：

* ``ASSERTED`` —— 凭据自身断言 scope 集合（自省链）；成员判定走
  ``capability_verdict`` 一张表，缺 ``required_scope`` 即拒绝（唯一的 403 成因）。
* ``DERIVED`` —— 能力由目录推导（角色/权限码），scope 不参与；``permissions`` 记空集。
* ``UNASSERTED`` —— 链不判能力；``permissions`` 记空集，管理面由角色门兜底。

本文件钉住：判定表全矩阵、每条链的档位与可观察行为、``scope_json`` 往返
（含早于本字段的历史行）、以及「回声不得再长出来」的语法级守卫。
"""

from __future__ import annotations

import ast
import base64
import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from operator_support import (
    B_USER_ID,
    C_USER_ID,
    TENANT,
    OperatorScope,
    b_subject,
    mysql_settings,
    resolve_session,
    upms_settings,
)

from aiops_diagnostics.execution.agent_runs import scope_from_json, scope_to_json
from aiops_diagnostics.identity.caller_auth import (
    CALLER_AUTH_FORBIDDEN,
    CallerAuthError,
    IntrospectionCallerResolver,
    IntrospectionSettings,
    UpmsCallerResolver,
)
from aiops_diagnostics.identity.capability import (
    CAPABILITY_ASSERTED,
    CAPABILITY_DERIVED,
    CAPABILITY_UNASSERTED,
    VERDICT_GRANT,
    VERDICT_NOT_ASSERTED,
    VERDICT_REFUSE,
    capability_verdict,
)
from aiops_diagnostics.identity.company_token_auth import (
    CompanyTokenCallerResolver,
    CompanyTokenSettings,
)
from aiops_diagnostics.identity.scope_context import (
    DataScope,
    ScopeContext,
    SubjectRecord,
)

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "aiops_diagnostics"
TEST_ROOT = Path(__file__).parents[1]

#: 断言集合恰含 ``aiops:orders:read`` 的简写——表行因此能放进一行。
ASSERTED_ONE = frozenset({"aiops:orders:read"})

# ── 判定表 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "required", "asserted", "expected"),
    [
        # ASSERTED：唯一做成员判定的档 —— 在 ⇒ GRANT，不在 ⇒ REFUSE。
        (CAPABILITY_ASSERTED, "aiops:orders:read", frozenset({"aiops:orders:read"}), VERDICT_GRANT),
        (CAPABILITY_ASSERTED, "aiops:agents:manage", frozenset({"aiops:orders:read"}), VERDICT_REFUSE),
        (CAPABILITY_ASSERTED, "aiops:orders:read", frozenset(), VERDICT_REFUSE),
        (CAPABILITY_ASSERTED, "", frozenset({"aiops:orders:read"}), VERDICT_REFUSE),
        # 其余两档不回答能力问题 —— 与输入无关，恒定 NOT_ASSERTED。
        (CAPABILITY_DERIVED, "aiops:orders:read", frozenset({"aiops:orders:read"}), VERDICT_NOT_ASSERTED),
        (CAPABILITY_DERIVED, "totally:invented:scope", frozenset(), VERDICT_NOT_ASSERTED),
        (CAPABILITY_UNASSERTED, "aiops:orders:read", frozenset(), VERDICT_NOT_ASSERTED),
        (CAPABILITY_UNASSERTED, "aiops:agents:manage", ASSERTED_ONE, VERDICT_NOT_ASSERTED),
    ],
)
def test_the_verdict_table(source: str, required: str, asserted: frozenset, expected: str) -> None:
    assert capability_verdict(source, required, asserted) == expected


def test_an_unknown_source_fails_closed() -> None:
    with pytest.raises(ValueError):
        capability_verdict("sometimes", "aiops:orders:read", frozenset())


# ── 每条链的档位与可观察行为 ────────────────────────────────────────────────


def _jwt(claims: dict[str, Any]) -> str:
    """本地验签模式（trust_company_payload）下形状合法即可的三段令牌。"""

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = b64(json.dumps({"typ": "JWT", "alg": "HS256"}).encode())
    payload = b64(json.dumps(claims).encode())
    return f"{header}.{payload}.{b64(b'sig')}"


def _token_context(entry: str | None = None, **claims_overrides: Any) -> ScopeContext:
    claims: dict[str, Any] = {
        "scope": ["server"],
        "id": B_USER_ID,
        "user_id": C_USER_ID,
        "tenant_id": TENANT,
        "type": "5",
        "exp": int((datetime.now(UTC) + timedelta(minutes=10)).timestamp()),
    }
    claims.update(claims_overrides)
    resolver = CompanyTokenCallerResolver(
        CompanyTokenSettings(signature_key="k" * 16, trust_company_payload=True),
        mysql_settings(),
        scope_mapper_factory=lambda _s: contextlib.nullcontext(None),
    )
    return resolver.resolve(_jwt(claims), required_scope="totally:invented:scope", platform_entry=entry)


def test_the_session_chain_asserts_no_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    """会话链是 DERIVED 档：``required_scope`` 不再被抄进上下文 —— 这正是
    被回填消灭的形状，问什么曾经被原样「授予」什么。"""
    context = resolve_session(
        monkeypatch,
        records=(b_subject(),),
        operator_scope=OperatorScope(("SHOP-1",), {"SHOP-1": ("S-1",)}),
    )
    assert context.capability_source == CAPABILITY_DERIVED
    assert context.permissions == frozenset()


def test_the_company_token_chain_asserts_no_capability() -> None:
    context = _token_context()  # required_scope="totally:invented:scope"
    assert context.capability_source == CAPABILITY_DERIVED
    assert context.permissions == frozenset()


@pytest.mark.parametrize(
    "inner_permissions",
    [
        frozenset(),  # 空权限集：旧实现在这里直接 403
        frozenset({"upms:user:list"}),  # 目录给了集合：本链也不抄进上下文
    ],
)
def test_the_upms_chain_is_explicitly_unasserted(inner_permissions: frozenset) -> None:
    """UPMS 链是 UNASSERTED 档：cloud-auth 给的是**平台权限**（能点哪些菜单），
    不是资源 scope。「有任意权限就放行」的错配检查已删除 —— 空集不再在本层
    403，目录给的非空集也不被当成能力断言抄回去；能力由角色门另行拒绝。"""
    subject = SubjectRecord(b_user_id=B_USER_ID, tenant_id=TENANT)

    class _FixedResolver:
        def resolve(self, _request: Any) -> ScopeContext:
            return ScopeContext.build(
                caller=subject,
                subject=subject,
                delegated=False,
                effective_tenant_id=TENANT,
                data_scope=DataScope(type="self"),
                roles=frozenset(),
                permissions=inner_permissions,
            )

    resolver = UpmsCallerResolver(upms_settings())
    resolver.resolver = _FixedResolver()  # type: ignore[assignment]
    context = resolver.resolve("token", required_scope="aiops:agents:manage")
    assert context.capability_source == CAPABILITY_UNASSERTED
    assert context.permissions == frozenset()


def _introspection_resolver(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> IntrospectionCallerResolver:
    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(payload).encode()

    monkeypatch.setattr(
        "aiops_diagnostics.common.bounded_http.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(),
    )
    return IntrospectionCallerResolver(
        IntrospectionSettings(
            url="https://auth.example.test/oauth2/introspect",
            client_id="aiops",
            client_secret="secret",
            audience="aiops-api",
        )
    )


def _introspection_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "active": True,
        "sub": B_USER_ID,
        "tenant_id": TENANT,
        "aud": ["aiops-api"],
        "scope": "aiops:orders:read",
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        "data_scope": {"type": "self"},
    }
    payload.update(overrides)
    return payload


def test_the_introspection_chain_is_asserted_and_grants_only_what_it_asserts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _introspection_resolver(monkeypatch, _introspection_payload())
    context = resolver.resolve("opaque-token", required_scope="aiops:orders:read")
    assert context.capability_source == CAPABILITY_ASSERTED
    assert context.permissions == frozenset({"aiops:orders:read"})


def test_the_introspection_chain_still_refuses_an_unasserted_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成员判定换了位置（唯一的那张表），403 的对外形状一字不变。"""
    resolver = _introspection_resolver(monkeypatch, _introspection_payload())
    with pytest.raises(CallerAuthError) as excinfo:
        resolver.resolve("opaque-token", required_scope="aiops:agents:manage")
    assert excinfo.value.code == CALLER_AUTH_FORBIDDEN


def test_platform_chains_read_the_same_verdict_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一份（身份, 权限集, 路由）在两平台链上的答案由同一张表驱动：
    ASSERTED 精确成员判定；UNASSERTED 恒不判。"""
    cases = [
        (CAPABILITY_ASSERTED, "aiops:agents:manage", ASSERTED_ONE, VERDICT_REFUSE),
        (CAPABILITY_ASSERTED, "aiops:orders:read", frozenset({"aiops:orders:read"}), VERDICT_GRANT),
        (CAPABILITY_UNASSERTED, "aiops:agents:manage", ASSERTED_ONE, VERDICT_NOT_ASSERTED),
        (CAPABILITY_UNASSERTED, "aiops:agents:manage", frozenset(), VERDICT_NOT_ASSERTED),
    ]
    for source, required, asserted, expected in cases:
        assert capability_verdict(source, required, asserted) == expected


# ── 审计与持久化 ──────────────────────────────────────────────────────────


def _context(**kwargs: Any) -> ScopeContext:
    fields: dict[str, Any] = {
        "caller": SubjectRecord(b_user_id=B_USER_ID, tenant_id=TENANT),
        "subject": SubjectRecord(b_user_id=B_USER_ID, tenant_id=TENANT),
        "delegated": False,
        "effective_tenant_id": TENANT,
        "data_scope": DataScope(type="self"),
        "roles": frozenset(),
        "permissions": frozenset(),
    }
    fields.update(kwargs)
    return ScopeContext.build(**fields)


def test_capability_source_is_not_part_of_the_fingerprint() -> None:
    """档位不改变任何可见性判定，因此不进指纹 —— 同语义上下文换档不换指纹。"""
    assert (
        _context(capability_source=CAPABILITY_DERIVED).scope_fingerprint
        == _context(capability_source=CAPABILITY_UNASSERTED).scope_fingerprint
    )


def test_an_unknown_capability_source_is_rejected() -> None:
    with pytest.raises(ValueError):
        _context(capability_source="sometimes")


def test_scope_json_round_trips_the_capability_source() -> None:
    context = _context(permissions=frozenset(), capability_source=CAPABILITY_DERIVED)
    rebuilt = scope_from_json(scope_to_json(context))
    assert rebuilt.capability_source == CAPABILITY_DERIVED
    assert rebuilt.permissions == frozenset()
    assert rebuilt.scope_fingerprint == context.scope_fingerprint


def test_scope_json_without_the_field_reads_as_unasserted() -> None:
    """早于 #1040 的行没有这一键：读回 UNASSERTED 只表示「当时没有档位
    记录」，字段内容（哪怕是回声形状）原样保留 —— 历史行不改写。"""
    context = _context(
        permissions=frozenset({"aiops:diagnoses:write"}),
        capability_source=CAPABILITY_ASSERTED,
    )
    legacy = json.loads(scope_to_json(context))
    del legacy["capability_source"]
    rebuilt = scope_from_json(json.dumps(legacy))
    assert rebuilt.capability_source == CAPABILITY_UNASSERTED
    assert rebuilt.permissions == frozenset({"aiops:diagnoses:write"})


def test_audit_summary_carries_the_source() -> None:
    assert _context(capability_source=CAPABILITY_DERIVED).audit_summary()["capability_source"] == (
        CAPABILITY_DERIVED
    )


# ── 没有第二份实现（AST 守卫）─────────────────────────────────────────────

#: 每条链声明的档位台账。新的解析器必须在这里登记 —— 这是「每条链一个
#: 显式档位」的可执行形式。
_CHAIN_TIERS = {
    "identity/third_session_auth.py": "CAPABILITY_DERIVED",
    "identity/company_token_auth.py": "CAPABILITY_DERIVED",
    "identity/caller_auth.py": {"CAPABILITY_ASSERTED", "CAPABILITY_UNASSERTED"},
}


def _trees() -> dict[str, ast.Module]:
    return {
        str(path.relative_to(SOURCE_ROOT)): ast.parse(path.read_text(encoding="utf-8"))
        for path in SOURCE_ROOT.rglob("*.py")
    }


def _is_echo_call(node: ast.AST) -> bool:
    """``frozenset({required_scope})`` 或 ``frozenset([required_scope])`` 的调用形状。"""
    if not isinstance(node, ast.Call) or getattr(node.func, "id", "") != "frozenset":
        return False
    if len(node.args) != 1 or not isinstance(node.args[0], (ast.Set, ast.List)):
        return False
    elements = node.args[0].elts
    return len(elements) == 1 and isinstance(elements[0], ast.Name) and elements[0].id == "required_scope"


def test_the_required_scope_echo_cannot_reappear_in_src() -> None:
    """``src/`` 里 ``permissions=frozenset({required_scope})`` 即红 —— 回声
    只可能从这个形状再长出来。集合与列表字面量都算。"""
    offenders: list[str] = []
    for relative, tree in _trees().items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_echo_call(node):
                continue
            offenders.append(f"{relative}:{node.lineno}")
    assert offenders == [], "; ".join(offenders)


def test_test_doubles_declare_the_tier_they_impersonate() -> None:
    """``tests/`` 的回声形状只允许在**声明了档位**的替身里出现：同一个
    ``ScopeContext.build`` 调用必须同时带 ``capability_source=`` —— 否则
    替身冒充的是「问什么有什么」的旧回声。"""
    offenders: list[str] = []
    for path in sorted(TEST_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            keywords = {kw.arg for kw in node.keywords if kw.arg}
            permission_values = [kw.value for kw in node.keywords if kw.arg == "permissions"]
            if any(_is_echo_call(value) for value in permission_values) and (
                "capability_source" not in keywords
            ):
                offenders.append(f"{path.relative_to(TEST_ROOT)}:{node.lineno}")
    assert offenders == [], "; ".join(offenders)


def test_every_chain_declares_its_tier() -> None:
    """四条链的 ``resolve`` 各自把档位写进上下文 —— 删了 ``capability_source=``
    本测试红，那是「这条链不再声明自己是什么」。"""
    trees = _trees()
    found: dict[str, set[str]] = {relative: set() for relative in _CHAIN_TIERS}
    for relative, expected in _CHAIN_TIERS.items():
        tree = trees[relative]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "capability_source" and isinstance(kw.value, ast.Name):
                    found[relative].add(kw.value.id)
        assert found[relative] == (expected if isinstance(expected, set) else {expected}), (
            f"{relative} declares {sorted(found[relative])}"
        )


#: 非 ASSERTED 链的 resolve 必须 ``del required_scope`` —— 请求的 scope 不被
#: 读取是结构性事实（读了直接 NameError），不是约定。台账按 文件 → 类 登记。
_NON_ASSERTED_RESOLVERS = {
    "identity/third_session_auth.py": "RedisThirdSessionResolver",
    "identity/company_token_auth.py": "CompanyTokenCallerResolver",
    "identity/caller_auth.py": "UpmsCallerResolver",
}


def _resolve_function(relative: str, class_name: str) -> ast.FunctionDef:
    tree = _trees()[relative]
    klass = next(
        node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return next(node for node in klass.body if isinstance(node, ast.FunctionDef) and node.name == "resolve")


def _deleted_names(function: ast.FunctionDef) -> set[str]:
    return {
        target.id
        for node in ast.walk(function)
        if isinstance(node, ast.Delete)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def test_non_asserted_chains_never_read_required_scope() -> None:
    """三条不判能力的链都把 ``required_scope`` ``del`` 掉 —— 「不读」由
    NameError 保证，不靠自觉。ASSERTED 链相反：它必须读（成员判定）。"""
    for relative, class_name in _NON_ASSERTED_RESOLVERS.items():
        resolve = _resolve_function(relative, class_name)
        assert "required_scope" in _deleted_names(resolve), f"{class_name}.resolve reads required_scope"


def test_the_upms_chain_cannot_refuse_on_permissions() -> None:
    """「有任意权限就放行」不得回来：``UpmsCallerResolver.resolve`` 内既不得
    引用 ``required_scope``（已 ``del``），也不得再抛 FORBIDDEN。"""
    resolve = _resolve_function("identity/caller_auth.py", "UpmsCallerResolver")
    assert "required_scope" in _deleted_names(resolve)
    forbidden_refs = [
        node.lineno
        for node in ast.walk(resolve)
        if isinstance(node, ast.Name) and node.id == "CALLER_AUTH_FORBIDDEN"
    ]
    assert forbidden_refs == [], f"FORBIDDEN references at {forbidden_refs}"


def test_the_asserted_chain_routes_through_the_shared_table() -> None:
    """自省链的成员判定必须走 ``capability_verdict`` —— 再写一次
    ``required_scope not in scopes`` 就是第二张表。"""
    resolve = _resolve_function("identity/caller_auth.py", "IntrospectionCallerResolver")
    verdict_calls = [
        node
        for node in ast.walk(resolve)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "capability_verdict"
    ]
    assert len(verdict_calls) == 1
    membership_checks = [
        node.lineno
        for node in ast.walk(resolve)
        if isinstance(node, ast.Compare)
        and isinstance(node.ops[0], (ast.NotIn, ast.In))
        and isinstance(node.left, ast.Name)
        and node.left.id == "required_scope"
    ]
    assert membership_checks == [], f"second membership table at {membership_checks}"
