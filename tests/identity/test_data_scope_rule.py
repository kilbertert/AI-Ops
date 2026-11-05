"""「入口 + 主体属性 → 数据范围」的判定序列只有一份（#644）。

这段序列曾经在 ``third_session_auth`` 与 ``company_token_auth`` 里各写一遍，逐分支
同形 —— #628 修顶层账号可见性时必须在两处各加一遍同一个分支，就是这条序列收利息的
方式。本文件钉住三类性质：

* **行为**：入口 / 账号层级 / 主体可用性 / 站点集合四个判定步的矩阵，以及「解析失败
  失败关闭为空 ``organ``、合法空集不记为失败、未声明的异常类型不当失败处理」。
* **两链等价**：会话链与令牌链对同一组（入口, 账号类型, 站点集合）输入产出**同一个**
  ``DataScope`` —— 替身只替换 I/O，映射仍走真实实现，于是「其中一链少一个分支」
  这类退化会让用例直接转红。
* **没有第二份实现**：AST 守卫 —— 两条链的 ``_data_scope`` 委托给 ``entry_data_scope``
  且文件内不再构造 ``DataScope``；两条判据（``is_operator_entry`` /
  ``is_tenant_level_account``）的调用点台账覆盖全仓库；平台凭据路径显式
  ``del platform_entry``（它们的范围由令牌与目录断言，不读入口头）。
"""

from __future__ import annotations

import ast
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
    session_settings,
    upms_settings,
)

from aiops_diagnostics.data.sources import SourceError
from aiops_diagnostics.identity.caller_auth import (
    IntrospectionCallerResolver,
    IntrospectionSettings,
    UpmsCallerResolver,
)
from aiops_diagnostics.identity.company_token_auth import (
    CompanyTokenCallerResolver,
    CompanyTokenSettings,
)
from aiops_diagnostics.identity.data_scope_rule import entry_data_scope
from aiops_diagnostics.identity.query_scope import static_site_mapper
from aiops_diagnostics.identity.scope_context import (
    SCOPE_TYPE_ALL,
    SCOPE_TYPE_ORGAN,
    SCOPE_TYPE_SELF,
    DataScope,
    ScopeContext,
    ScopeError,
    SubjectRecord,
)
from aiops_diagnostics.identity.third_session_auth import RedisThirdSessionResolver

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "aiops_diagnostics"
SESSION_AUTH = "identity/third_session_auth.py"
TOKEN_AUTH = "identity/company_token_auth.py"
CALLER_AUTH = "identity/caller_auth.py"

SHOP_IDS = ("SHOP-1",)
SITES = {"SHOP-1": ("S-1", "S-2")}
ORGAN = DataScope(type=SCOPE_TYPE_ORGAN, site_ids=("S-1", "S-2"))
EMPTY_ORGAN = DataScope(type=SCOPE_TYPE_ORGAN, site_ids=())
SELF = DataScope(type=SCOPE_TYPE_SELF)
ALL = DataScope(type=SCOPE_TYPE_ALL)


def _rule(
    entry: str | None,
    user_type: str | None,
    *,
    subject_unavailable: bool = False,
    outcome: tuple[str, ...] | Exception = SHOP_IDS,
) -> tuple[DataScope, list[str], list[Exception]]:
    """调一次共享判定，记录「站点集合有没有被查」与「失败有没有被报」。"""
    calls: list[str] = []
    seen: list[Exception] = []

    def _site_ids() -> tuple[str, ...]:
        calls.append("site_ids")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    scope = entry_data_scope(
        entry,
        user_type,
        subject_unavailable=subject_unavailable,
        site_ids=_site_ids,
        on_unavailable=seen.append,
    )
    return scope, calls, seen


# ── 判定矩阵 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("entry", ["operator", "OPERATOR", " operator ", "Operator"])
def test_operator_entries_take_the_operator_branch(entry: str | None) -> None:
    """入口判据与 ``PlatformIdentityResolver`` 同一套规范化（strip + lower）。"""
    scope, calls, _ = _rule(entry, "5", outcome=("S-1",))
    assert scope == DataScope(type=SCOPE_TYPE_ORGAN, site_ids=("S-1",))
    assert calls == ["site_ids"]


@pytest.mark.parametrize("entry", [None, "", "consumer", "operatorx", "OPERATORX", "admin"])
def test_non_operator_entries_never_leave_self(entry: str | None) -> None:
    """未知/缺失/非法入口一律最窄，且**不打**站点归属查询。"""
    scope, calls, seen = _rule(entry, "5", outcome=("S-1",))
    assert scope == SELF
    assert calls == [] and seen == []


def test_entry_is_judged_before_the_account_tier() -> None:
    """非管家端入口上的顶层账号仍得 ``self`` —— 入口分流先于层级判定。"""
    scope, calls, _ = _rule("consumer", "-1", outcome=("S-1",))
    assert scope == SELF
    assert calls == []


@pytest.mark.parametrize("user_type", ["-1", "1", " 1 ", "\t-1\n"])
def test_tenant_level_accounts_get_the_whole_tenant(user_type: str | None) -> None:
    """顶层账号的可见范围是整个租户 —— 且不花一次店铺/站点归属查询。"""
    scope, calls, _ = _rule("operator", user_type, outcome=("S-1",))
    assert scope == ALL
    assert calls == []


@pytest.mark.parametrize("user_type", ["5", "", None, "0", "-1x", "2"])
def test_every_other_account_resolves_its_site_set(user_type: str | None) -> None:
    scope, calls, _ = _rule("operator", user_type, outcome=("S-1",))
    assert scope == DataScope(type=SCOPE_TYPE_ORGAN, site_ids=("S-1",))
    assert calls == ["site_ids"]


def test_an_unavailable_subject_stays_self_without_a_site_lookup() -> None:
    """会话链独有的第三判定步：拿不到同租户内唯一 B 端主体 ⇒ 最窄。"""
    scope, calls, _ = _rule("operator", "5", subject_unavailable=True, outcome=("S-1",))
    assert scope == SELF
    assert calls == []


def test_a_legally_empty_site_set_is_not_reported_as_unavailable() -> None:
    """合法空集（漏登记/数据缺口，#425）与解析失败是两种成因，不能混记。"""
    scope, _, seen = _rule("operator", "5", outcome=())
    assert scope == EMPTY_ORGAN
    assert seen == []


@pytest.mark.parametrize(
    "error",
    [
        ScopeError("upms unreachable", code="UPMS_UNAVAILABLE"),
        SourceError("site mapper down"),
        ValueError("site id unusable"),
    ],
)
def test_every_declared_unavailable_fails_closed_to_an_empty_organ(error: Exception) -> None:
    """失败关闭为**空集合** —— 不回落 ``all``，也绝不套用 ``self``。"""
    scope, _, seen = _rule("operator", "5", outcome=error)
    assert scope == EMPTY_ORGAN
    assert seen == [error]


def test_an_undeclared_failure_propagates_instead_of_becoming_a_scope() -> None:
    """声明集之外的异常是缺陷而不是「上游不可用」—— 放它出去，不吞成范围。"""
    seen: list[Exception] = []

    def _site_ids() -> tuple[str, ...]:
        raise RuntimeError("a bug, not an outage")

    with pytest.raises(RuntimeError):
        entry_data_scope(
            "operator",
            "5",
            subject_unavailable=False,
            site_ids=_site_ids,
            on_unavailable=seen.append,
        )
    assert seen == []


# ── 两链等价 ──────────────────────────────────────────────────────────────


def _session_scope(
    subject: SubjectRecord, entry: str | None, operator_scope: OperatorScope | None
) -> DataScope:
    resolver = RedisThirdSessionResolver(session_settings(), operator_scope=operator_scope)
    return resolver._data_scope(subject, subject.tenant_id, entry)


def _token_scope(
    subject: SubjectRecord,
    entry: str | None,
    *,
    shop_ids: tuple[str, ...] = SHOP_IDS,
    error: Exception | None = None,
) -> DataScope:
    if error is not None:

        def factory(_settings: Any) -> Any:
            return contextlib.nullcontext(_RaisingMapper(error))

    else:

        def factory(_settings: Any) -> Any:
            return contextlib.nullcontext(static_site_mapper(sites_by_shop=SITES))

    settings = CompanyTokenSettings(
        url="https://check.example.test/oauth/check_token",
        client_id="aiops",
        client_secret="check-token-secret",
    )
    resolver = CompanyTokenCallerResolver(
        settings, mysql_settings(), scope_mapper_factory=factory
    )
    return resolver._data_scope(subject, shop_ids, entry)


class _RaisingMapper:
    """``site_ids_by_shops`` 直接抛错的站点映射（模拟充电库一跳不可用）。"""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def site_ids_by_shops(self, _shop_ids: tuple[str, ...], _tenant_id: str) -> tuple[str, ...]:
        raise self.error


@pytest.mark.parametrize("entry", ["operator", "consumer", None, " OPERATOR "])
@pytest.mark.parametrize("user_type", ["5", "-1"])
def test_both_credential_chains_answer_the_same_scope(entry: str | None, user_type: str) -> None:
    """同一请求在两条链上必须得到**同一个** ``DataScope`` —— 否则同一张订单的
    授权答案取决于「用户拿的是会话还是令牌」，那正是 #644 收敛要消灭的歧义。"""
    subject = b_subject(user_type=user_type)
    session = _session_scope(subject, entry, OperatorScope(SHOP_IDS, SITES))
    token = _token_scope(subject, entry)
    assert session == token


def test_both_chains_fail_closed_to_the_same_empty_organ() -> None:
    """失败落点也一致：空 ``organ``，各记各的日志行（词汇不同、分支相同）。"""
    subject = b_subject()
    session = _session_scope(
        subject, "operator", OperatorScope(SHOP_IDS, SITES, error=ScopeError("upms"))
    )
    token = _token_scope(subject, "operator", error=SourceError("mysql"))
    assert session == token == EMPTY_ORGAN


def test_both_chains_treat_a_legally_empty_site_set_the_same_way() -> None:
    subject = b_subject()
    session = _session_scope(subject, "operator", OperatorScope((), SITES))
    token = _token_scope(subject, "operator", shop_ids=())
    assert session == token == EMPTY_ORGAN


def test_only_the_session_chain_has_the_subject_unavailable_branch() -> None:
    """主体可用性是会话链的第三判定步（C→B 映射可能不唯一）；令牌链主体来自
    令牌声明，缺身份在更早处已被拒 —— 它在调用点显式传 ``False``。"""
    operator_scope = OperatorScope(SHOP_IDS, SITES)
    assert (
        _session_scope(b_subject(b_subject_reason="c_mapping_not_found"), "operator", operator_scope)
        == SELF
    )
    assert _session_scope(b_subject(), "operator", None) == SELF
    # 令牌链对同样的「可用主体 + operator 入口」给 organ，证明它没有该分支。
    assert _token_scope(b_subject(), "operator") == ORGAN


def test_each_chain_logs_its_own_line_on_unavailable(caplog: pytest.LogCaptureFixture) -> None:
    """两条链失败时记各自的日志行 —— 成因要去不同的上游看，混用一个词汇
    就无法从日志判断是哪条链坏了。"""
    subject = b_subject()
    with caplog.at_level("INFO"):
        _session_scope(
            subject, "operator", OperatorScope(SHOP_IDS, SITES, error=ScopeError("upms"))
        )
        _token_scope(subject, "operator", error=SourceError("mysql"))
    messages = [record.getMessage() for record in caplog.records]
    assert any("third_session operator_scope_unavailable" in m for m in messages)
    assert any("company_token scope_unavailable" in m for m in messages)


# ── 平台凭据路径不读入口头 ────────────────────────────────────────────────


def _platform_context() -> ScopeContext:
    subject = SubjectRecord(b_user_id=B_USER_ID, c_user_id=C_USER_ID, tenant_id=TENANT)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=TENANT,
        data_scope=DataScope(type=SCOPE_TYPE_SELF),
        roles=frozenset(),
        permissions=frozenset({"aiops:orders:read"}),
    )


def test_the_upms_path_returns_the_same_context_for_every_entry() -> None:
    """UPMS 平台凭据路径：范围由目录按令牌断言，入口头不参与。"""
    context = _platform_context()

    class _FixedResolver:
        def resolve(self, _request: Any) -> ScopeContext:
            return context

    resolver = UpmsCallerResolver(upms_settings())
    resolver.resolver = _FixedResolver()  # type: ignore[assignment]
    for entry in ("operator", "consumer", None):
        assert (
            resolver.resolve("token", required_scope="aiops:orders:read", platform_entry=entry)
            is context
        )


def test_the_introspection_path_returns_the_same_context_for_every_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自省平台凭据路径：范围由令牌 ``data_scope`` 声明断言，入口头不参与。"""
    payload = {
        "active": True,
        "sub": B_USER_ID,
        "tenant_id": TENANT,
        "aud": ["aiops-api"],
        "scope": "aiops:orders:read",
        "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        "data_scope": {"type": "organ", "site_ids": ["S-9"]},
    }

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
    resolver = IntrospectionCallerResolver(
        IntrospectionSettings(
            url="https://auth.example.test/oauth2/introspect",
            client_id="aiops",
            client_secret="secret",
            audience="aiops-api",
        )
    )
    contexts = [
        resolver.resolve("opaque-token", required_scope="aiops:orders:read", platform_entry=entry)
        for entry in ("operator", "consumer", None)
    ]
    # ``resolved_at`` 每次解析都新取，不进范围语义 —— 等价比对落在指纹与
    # 语义字段上，不比整个对象。
    semantic = [
        (c.caller, c.subject, c.delegated, c.effective_tenant_id, c.data_scope, c.roles, c.permissions)
        for c in contexts
    ]
    assert {context.scope_fingerprint for context in contexts} == {contexts[0].scope_fingerprint}
    assert all(item == semantic[0] for item in semantic)


# ── 没有第二份实现（AST 守卫）───────────────────────────────────────────────

#: 「谁允许调用这两条安全判据」的台账：判据是安全边界，多一份调用点就多一处
#: 可能漂移的判定。新增调用点必须在这里登记理由；已有调用点的理由：
#: - ``entry_data_scope``：判定序列的唯一实现（本 PRD）；
#: - ``CompanyTokenCallerResolver.resolve``：入口才解析店铺集合的按需取值短路
#:   （非管家端入口/顶层账号不打 ``/shopuser/getShops``，与规则里同一判据）；
#: - ``operator_publish``：发布面是**另一条**规则（入口必须显式为 operator 才能发），
#:   判「能不能发」而非「数据范围取哪种口径」；
#: - ``build_trial_scope`` / ``operator_scope``：试用与管理面各自的「顶层账号不受
#:   站点维度约束」判定，语义同样是「层级」而非「范围分派」。
_CRITERION_LEDGER: dict[str, set[tuple[str, str]]] = {
    "is_operator_entry": {
        ("identity/data_scope_rule.py", "entry_data_scope"),
        ("identity/company_token_auth.py", "resolve"),
        ("gateway/gateway_api.py", "operator_publish"),
    },
    "is_tenant_level_account": {
        ("identity/data_scope_rule.py", "entry_data_scope"),
        ("identity/company_token_auth.py", "resolve"),
        ("dify/dify_trial.py", "build_trial_scope"),
        ("operator/operator_cpms.py", "operator_scope"),
    },
}
_CRITERIA = frozenset(_CRITERION_LEDGER)


def _tree(relative: str) -> ast.Module:
    return ast.parse((SOURCE_ROOT / relative).read_text(encoding="utf-8"))


def _innermost_function(tree: ast.Module) -> dict[ast.AST, str]:
    """AST 节点 → 它所在的最内层函数名（含类方法归属；模块级为 ``"<module>"``）。"""
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    owners: dict[ast.AST, str] = {}

    def owner_of(node: ast.AST) -> str:
        current = node
        while current in parents:
            current = parents[current]
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current.name
        return "<module>"

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            owners[node] = owner_of(node)
    return owners


def _criterion_call_sites() -> dict[str, set[tuple[str, str]]]:
    """全仓库 ``is_operator_entry`` / ``is_tenant_level_account`` 调用点（从源码枚举）。"""
    found: dict[str, set[tuple[str, str]]] = {name: set() for name in _CRITERIA}
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = _tree(str(path.relative_to(SOURCE_ROOT)))
        owners = _innermost_function(tree)
        for node, owner in owners.items():
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") in _CRITERIA:
                found[node.func.id].add((str(path.relative_to(SOURCE_ROOT)), owner))  # type: ignore[union-attr]
    return found


def test_the_criterion_call_sites_match_the_ledger() -> None:
    assert _criterion_call_sites() == _CRITERION_LEDGER


def test_neither_chain_constructs_a_datascope_outside_the_rule() -> None:
    """``DataScope(...)`` 在两链文件里一个都不许出现 —— 出现一个就是第二份实现。"""
    for relative in (SESSION_AUTH, TOKEN_AUTH):
        offenders = [
            f"{relative}:{node.lineno}"
            for node in ast.walk(_tree(relative))
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "DataScope"
        ]
        assert offenders == [], "; ".join(offenders)


def test_both_chains_delegate_to_the_shared_rule() -> None:
    """两链的 ``_data_scope`` 各自恰好调一次 ``entry_data_scope``。"""
    for relative in (SESSION_AUTH, TOKEN_AUTH):
        tree = _tree(relative)
        owners = _innermost_function(tree)
        hits = sorted(
            node.lineno
            for node, owner in owners.items()
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "entry_data_scope"
            and owner == "_data_scope"
        )
        assert len(hits) == 1, f"{relative} delegates at {hits}"


def test_the_token_chain_declares_no_subject_unavailable_branch() -> None:
    """「主体可用性」分支的存在性写在调用点上：令牌链显式 ``False``，
    会话链传真实表达式 —— 谁把令牌链改成真表达式，就是悄悄给它加了一个
    这条链结构上不可能成立的分支。"""
    for relative, expect_literal_false in ((TOKEN_AUTH, True), (SESSION_AUTH, False)):
        tree = _tree(relative)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "entry_data_scope"
        ]
        assert len(calls) == 1
        keyword = next((kw for kw in calls[0].keywords if kw.arg == "subject_unavailable"), None)
        assert keyword is not None, f"{relative} does not pass subject_unavailable"
        literal_false = isinstance(keyword.value, ast.Constant) and keyword.value.value is False
        assert literal_false == expect_literal_false, (
            f"{relative} subject_unavailable literal={literal_false}"
        )


def test_platform_paths_delete_the_entry_argument() -> None:
    """``del platform_entry`` 是「平台凭据路径不读入口头」的可执行形式：
    删掉它，入口头就重新成为一个有沉默语义的参数；有人真读它才该显式决定。"""
    tree = _tree(CALLER_AUTH)
    for class_name in ("UpmsCallerResolver", "IntrospectionCallerResolver"):
        klass = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        resolve = next(
            node
            for node in klass.body
            if isinstance(node, ast.FunctionDef) and node.name == "resolve"
        )
        deleted = {
            target.id
            for node in ast.walk(resolve)
            if isinstance(node, ast.Delete)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        assert "platform_entry" in deleted, f"{class_name}.resolve keeps platform_entry"
