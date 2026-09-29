"""管家端公司 OAuth2 令牌的解析（#443，ADR-0009 第一段）。

断言的都是**对外可观察行为**：解析出的身份与站点范围、HTTP 状态与错误码、以及「哪条链被走到」。
不测内部调用顺序，不断言 SQL 文本。

两条纪律沿用既有先例：

- **形状替身**（``_Response``）照 ``tests/test_caller_auth.py``：公司 check_token 的成功体是
  **裸身份映射**（没有 ``code``/``data`` 信封）、失败体是 HTTP 200 + ``{"code":1,...}``，
  与 UPMS 的 ``{code:0,data:...}`` 信封不是一回事，因此不重用 ``FakeUpmsTransport``。
- **范围替身**照 ``tests/operator_support.py`` 的纪律：替身内部走**真实**的
  ``operator_site_scope_from_shops``，只替换 I/O（check_token 的 HTTP、充电库的站点归属映射），
  于是「把店铺 id 当站点 id」「空集合回落到不限」这类退化会让用例直接转红。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from operator_support import (
    B_USER_ID,
    C_USER_ID,
    ORDER_INSIDE,
    ORDER_OUTSIDE,
    SESSION_TOKEN,
    SITE_IN,
    Connection,
    assistant_app,
    java_session,
    mysql_settings,
    order_queries,
)

from aiops_diagnostics.caller_auth import (
    CALLER_AUTH_INVALID,
    CALLER_AUTH_UNAVAILABLE,
    CallerAuthError,
    ScopedOrderAuthorizer,
    SourceKeyCallerResolver,
)
from aiops_diagnostics.company_token_auth import (
    CompanyTokenCallerResolver,
    CompanyTokenSettings,
)
from aiops_diagnostics.config import Settings
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.query_scope import static_site_mapper
from aiops_diagnostics.scope_context import (
    SCOPE_TYPE_ORGAN,
    SCOPE_TYPE_SELF,
    ScopeError,
)

TENANT_ID = "T-1"
CHECK_TOKEN_URL = "https://check.example.test/oauth/check_token"
CLIENT_ID = "aiops"
CLIENT_SECRET = "check-token-secret"
TOKEN = "company-opaque-token"
#: ``Basic base64("aiops:check-token-secret")``，钉住「客户端凭据去哪了」。
EXPECTED_BASIC = "Basic YWlvcHM6Y2hlY2stdG9rZW4tc2VjcmV0"

SITES_BY_SHOP = {"SHOP-1": (SITE_IN,)}

#: 真实运营商令牌的公司 check_token 成功体：**框架组装的映射**——框架字段（``username``/
#: ``scope``/``exp``/``client_id``）与增强器字段（身份那一组）合并，后者覆盖同名键；**没有**
#: ``code``/``data`` 信封。因此身份判据只能落在增强器那一组（``id`` + ``tenant_id``）。
CLAIMS: dict[str, Any] = {
    # 框架字段（真实成功体里也存在，见 DefaultAccessTokenConverter.convertAccessToken）
    "scope": ["server"],
    "username": "operator-a",  # 增强器覆盖了框架的同名 username
    # 增强器字段（tokenEnhancer 逐字段写入 additionalInformation）
    "id": B_USER_ID,
    "user_id": C_USER_ID,
    "organ_id": "ORG-1",
    "type": "5",
    "tenant_id": TENANT_ID,
    "system_id": "1",
    "shop_id": "SHOP-HEAD",
    "license": "made-by-aiops",
    "tenant_ids": [TENANT_ID],
    "shop_ids": ["SHOP-1"],
    # 这三个也在真实成功体里，同样不是判据：``active``/``exp``/``client_id`` 与客户端凭据令牌
    # 无法区分（``exp`` 由公司判），只有增强器那组字段能区分。
    "active": True,
    "exp": 4102444800,
    "client_id": "admin",
}


class _Response:
    """最小响应对象：``bounded_http`` 只调用 ``read()``。"""

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class _RawResponse(_Response):
    """非 JSON 响应体（一层反代返回的 HTML 错误页）。"""

    def __init__(self, body: bytes) -> None:
        self._body = body


def _settings(**overrides: Any) -> CompanyTokenSettings:
    fields: dict[str, Any] = {
        "url": CHECK_TOKEN_URL,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    fields.update(overrides)
    return CompanyTokenSettings(**fields)


class _Mapper:
    """站点归属映射替身：只替换 I/O，映射规则仍走真实实现。"""

    def __init__(self, sites: dict[str, tuple[str, ...]] | None = None, *, error: Exception | None = None):
        self.sites = SITES_BY_SHOP if sites is None else sites
        self.error = error
        self.opened = 0

    def __call__(self, _settings: Settings) -> _Mapper:
        self.opened += 1
        return self

    def __enter__(self) -> Any:
        if self.error is not None:
            raise self.error
        return static_site_mapper(sites_by_shop=self.sites)

    def __exit__(self, *_args: Any) -> bool:
        return False


def _resolver(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: object = None,
    mapper: _Mapper | None = None,
    transport: Any = None,
) -> CompanyTokenCallerResolver:
    """构造解析器，并把 check_token 的 HTTP 换成替身。"""
    body = CLAIMS if payload is None else payload

    def respond(_request: Any, **_kwargs: Any) -> _Response:
        return _Response(body)

    monkeypatch.setattr("aiops_diagnostics.bounded_http.urllib.request.urlopen", transport or respond)
    return CompanyTokenCallerResolver(
        _settings(),
        mysql_settings(),
        scope_mapper_factory=mapper or _Mapper(),
    )


def _resolve(monkeypatch: pytest.MonkeyPatch, **kwargs: Any):
    entry = kwargs.pop("entry", "operator")
    token = kwargs.pop("token", TOKEN)
    resolver = _resolver(monkeypatch, **kwargs)
    return resolver.resolve(token, required_scope="aiops:orders:read", platform_entry=entry)


# --- 1. 形状：令牌 → 身份与站点范围 -----------------------------------------


def test_a_valid_token_yields_the_operator_identity_and_site_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有效令牌 ⇒ B 端 ``id``、C 端 ``user_id``、租户与站点集合（经归属映射）。"""
    sent: list[Any] = []

    def transport(request: Any, **_kwargs: Any) -> _Response:
        sent.append(request)
        return _Response(CLAIMS)

    context = _resolve(monkeypatch, transport=transport)

    assert context.subject.b_user_id == B_USER_ID
    assert context.subject.c_user_id == C_USER_ID
    assert context.caller.b_user_id == B_USER_ID
    assert context.effective_tenant_id == TENANT_ID
    assert context.delegated is False
    assert context.data_scope.type == SCOPE_TYPE_ORGAN
    assert context.data_scope.site_ids == (SITE_IN,)
    assert context.scope_fingerprint

    request = sent[0]
    assert request.full_url == CHECK_TOKEN_URL
    assert request.method == "POST"
    assert request.get_header("Authorization") == EXPECTED_BASIC
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert request.data == b"token=" + TOKEN.encode()


def test_the_client_secret_never_appears_in_settings_repr() -> None:
    assert CLIENT_SECRET not in repr(_settings())


def test_shop_ids_are_mapped_through_the_ownership_table_not_used_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """店铺 id ≠ 站点 id：必须经 ``ch_site.shop_id → ch_site.id`` 映射。"""
    mapper = _Mapper({"SHOP-1": ("SITE-A", "SITE-B")})

    context = _resolve(monkeypatch, mapper=mapper)

    assert context.data_scope.site_ids == ("SITE-A", "SITE-B")
    assert mapper.opened == 1


def test_the_tenant_comes_from_tenant_id_not_the_switchable_tenant_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``tenant_ids`` 是平台管理员可**切换**的租户列表：拿它当范围只会放宽。"""
    context = _resolve(
        monkeypatch,
        payload={**CLAIMS, "tenant_id": TENANT_ID, "tenant_ids": [TENANT_ID, "T-2"]},
    )

    assert context.effective_tenant_id == TENANT_ID


# --- 2. 入口分流（镜像 #436）--------------------------------------------------


@pytest.mark.parametrize("entry", ["consumer", None, "", "unknown", "CONSUMER"])
def test_only_the_operator_entry_gets_the_operator_site_scope(
    monkeypatch: pytest.MonkeyPatch, entry: str | None
) -> None:
    """非管家端入口一律保持最窄的 ``self``，且**不发起任何范围 I/O**。"""
    mapper = _Mapper()

    context = _resolve(monkeypatch, entry=entry, mapper=mapper)

    assert context.data_scope.type == SCOPE_TYPE_SELF
    assert context.data_scope.site_ids == ()
    assert mapper.opened == 0
    # 范围收窄不等于身份不可用：C 端 id 仍在（self 范围靠它过滤）。
    assert context.subject.c_user_id == C_USER_ID


@pytest.mark.parametrize("entry", ["OPERATOR", " operator ", "Operator"])
def test_the_operator_entry_is_normalized_like_the_platform_resolver(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """规范化与 ``PlatformIdentityResolver`` 同一套：上游认的写法这里也必须认，否则范围被收紧。"""
    context = _resolve(monkeypatch, entry=entry)

    assert context.data_scope.type == SCOPE_TYPE_ORGAN
    assert context.data_scope.site_ids == (SITE_IN,)


# --- 3. fail closed 矩阵 ------------------------------------------------------


def _without(key: str) -> dict[str, Any]:
    return {k: v for k, v in CLAIMS.items() if k != key}


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        # 失败体：公司对非法令牌返回 HTTP 200 + code 非 0，且 **data 为 null**
        # （R.failed(Integer, String) 走 restResult(null, code, msg)）。数字与字符串两种写法。
        ({"code": 1, "msg": "token无效", "data": None}, CALLER_AUTH_INVALID),
        ({"code": "1", "msg": "token无效", "data": None}, CALLER_AUTH_INVALID),
        ({"code": 401, "msg": "unauthorized", "data": None}, CALLER_AUTH_INVALID),
        # 只有 active、没有身份：客户端凭据令牌的形状 —— ``active`` 不是判据，
        # 而且它同样带 client_id/scope/exp/authorities，所以形状判据必须窄到增强器那组字段。
        (
            {
                "active": True,
                "client_id": "aiops",
                "scope": ["server"],
                "exp": 4102444800,
                "authorities": ["ROLE_USER"],
                "shop_ids": [],
            },
            CALLER_AUTH_INVALID,
        ),
        # 身份字段缺失/空白：不猜、不回落成 C 端身份。
        ({**_without("id"), "shop_ids": []}, CALLER_AUTH_INVALID),
        ({**CLAIMS, "id": ""}, CALLER_AUTH_INVALID),
        ({**CLAIMS, "id": None}, CALLER_AUTH_INVALID),
        ({**_without("tenant_id"), "shop_ids": []}, CALLER_AUTH_INVALID),
        ({**CLAIMS, "tenant_id": "  "}, CALLER_AUTH_INVALID),
        ({**CLAIMS, "id": {"nested": 1}}, CALLER_AUTH_INVALID),
        # 是合法 JSON 但不是身份映射（数组）：无可读的身份声明。
        (["not", "a", "map"], CALLER_AUTH_INVALID),
        # 标量载荷连信封都不成立 ⇒ 无法判断令牌是否被拒，按上游不可用（可重试）。
        ("not-a-map", CALLER_AUTH_UNAVAILABLE),
    ],
)
def test_the_resolver_fails_closed(
    monkeypatch: pytest.MonkeyPatch, payload: object, expected_code: str
) -> None:
    with pytest.raises(CallerAuthError) as excinfo:
        _resolve(monkeypatch, payload=payload)

    assert excinfo.value.code == expected_code
    # 令牌与密钥都不得进入错误文本。
    assert TOKEN not in str(excinfo.value)
    assert CLIENT_SECRET not in str(excinfo.value)


def test_the_company_refusal_is_judged_by_the_envelope_not_by_its_identity_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公司的拒绝体里**带得下完整身份字段**，仍必须被拒。

    这条是「判定写在信封上」的**变异覆盖**：只改 ``_envelope_refused`` 的判据（例如直接
    ``return False``）而身份字段齐全时，按身份解读的代码会把它当成功 —— 那样一条被公司明确
    拒绝的令牌就能拿到完整的运营商站点范围。用例特意让所有身份字段合法，只让 ``code`` 说「不」。
    """
    refused = {**CLAIMS, "code": 1, "msg": "token无效", "data": None}
    mapper = _Mapper()
    sent: list[Any] = []

    def transport(request: Any, **_kwargs: Any) -> _Response:
        sent.append(request)
        return _Response(refused)

    with pytest.raises(CallerAuthError) as excinfo:
        _resolve(monkeypatch, payload=refused, mapper=mapper, transport=transport)

    assert excinfo.value.code == CALLER_AUTH_INVALID
    assert mapper.opened == 0  # 拒绝发生在任何范围解析之前
    assert len(sent) == 1


@pytest.mark.parametrize(
    "shop_ids",
    [
        {"SHOP-1": True},  # 对象
        123,  # 标量数字
        ["SHOP-1", {"nested": 1}],  # 元素不是标量
        ["SHOP-1", None],
        ["SHOP-1", ""],
    ],
)
def test_invalid_shop_id_shapes_fail_closed(monkeypatch: pytest.MonkeyPatch, shop_ids: object) -> None:
    with pytest.raises(CallerAuthError) as excinfo:
        _resolve(monkeypatch, payload={**CLAIMS, "shop_ids": shop_ids})

    assert excinfo.value.code == CALLER_AUTH_INVALID


def test_a_missing_shop_ids_key_is_rejected_while_an_empty_value_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """口径有意不对称：**键缺失**说明通道没给答案（拒）；**值为空**说明账号确实没绑店铺（Ø）。

    两者下游都是拒绝，但成因不同：后者是「数据待补登记」，前者是「这次应答不可信」。
    """
    with pytest.raises(CallerAuthError) as excinfo:
        _resolve(monkeypatch, payload=_without("shop_ids"))
    assert excinfo.value.code == CALLER_AUTH_INVALID

    for empty in ([], None):
        context = _resolve(monkeypatch, payload={**CLAIMS, "shop_ids": empty})
        assert context.data_scope.type == SCOPE_TYPE_ORGAN
        assert context.data_scope.site_ids == ()


def test_a_shop_ids_string_is_read_as_one_id_not_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """公司契约是数组；字符串只当「一个店铺 id」，不做逗号切分的猜测。"""
    context = _resolve(monkeypatch, payload={**CLAIMS, "shop_ids": "SHOP-UNMAPPED"})

    # 该店铺没有站点归属 ⇒ 空集合（拒绝），不是把整串当站点 id。
    assert context.data_scope.site_ids == ()


@pytest.mark.parametrize("token", ["", "aops_device_token"])
def test_aiops_own_tokens_are_never_sent_to_the_company(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    """本仓自己的设备令牌不是公司令牌：先拒，避免把它送到权威服务上去试。"""
    sent: list[Any] = []

    def transport(request: Any, **_kwargs: Any) -> _Response:
        sent.append(request)
        return _Response(CLAIMS)

    with pytest.raises(CallerAuthError) as excinfo:
        _resolve(monkeypatch, token=token, transport=transport)

    assert excinfo.value.code == CALLER_AUTH_INVALID
    assert sent == []


# --- 4. 上游故障：可重试、不逃成未映射异常 -------------------------------------


def test_a_transport_failure_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: Any, **_kwargs: Any) -> _Response:
        raise urllib.error.URLError("offline")

    with pytest.raises(CallerAuthError) as excinfo:
        _resolve(monkeypatch, transport=unavailable)

    assert excinfo.value.code == CALLER_AUTH_UNAVAILABLE
    assert excinfo.value.retryable


def test_http_401_says_the_caller_credentials_are_wrong_not_the_user_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公司 ``checkTokenAccess("isAuthenticated()")`` 拒的是 **AI-Ops 的客户端凭据**。

    报成 401 会把运维问题说成用户问题（让客户端去重新登录），因此归到可重试的 503。
    """

    def reject(*_args: Any, **_kwargs: Any) -> _Response:
        raise urllib.error.HTTPError(CHECK_TOKEN_URL, 401, "Unauthorized", {}, None)

    with pytest.raises(CallerAuthError) as excinfo:
        _resolve(monkeypatch, transport=reject)

    assert excinfo.value.code == CALLER_AUTH_UNAVAILABLE
    assert excinfo.value.retryable


def test_a_non_json_body_is_a_retryable_upstream_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    """反代返回的 HTML 错误页是**传输**故障，不是「令牌被拒」。"""

    def html(*_args: Any, **_kwargs: Any) -> _RawResponse:
        return _RawResponse(b"<html><body>502 Bad Gateway</body></html>")

    with pytest.raises(CallerAuthError) as excinfo:
        _resolve(monkeypatch, transport=html)

    assert excinfo.value.code == CALLER_AUTH_UNAVAILABLE
    assert excinfo.value.retryable


# --- 5. 站点范围解析失败：拒绝，且不是 500 -------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ScopeError("站点归属查询失败", code="scope.upms_unavailable"),
        ValueError("范围 ID 包含不允许的字符"),
    ],
)
def test_every_scope_failure_denies_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="aiops_diagnostics.company_token_auth"):
        context = _resolve(monkeypatch, mapper=_Mapper(error=error))

    assert context.data_scope.type == SCOPE_TYPE_ORGAN
    assert context.data_scope.site_ids == ()
    assert "company_token scope_unavailable" in caplog.text
    for identifier in (B_USER_ID, C_USER_ID, TENANT_ID, CLIENT_SECRET, TOKEN):
        assert identifier not in caplog.text


def test_the_two_empty_scope_causes_are_still_logged_apart(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """空集合的成因沿用共享解析的那两组原因：账号漏登记 vs 已登记店铺无站点。"""
    with caplog.at_level(logging.INFO, logger="aiops_diagnostics.query_scope"):
        _resolve(monkeypatch, payload={**CLAIMS, "shop_ids": []})
        caplog.clear()
        _resolve(monkeypatch, mapper=_Mapper({}))

    assert "reason=shop_without_site" in caplog.text


# --- 6. 范围真的下推到查询（不回落成不限）--------------------------------------


def test_a_token_without_shop_binding_denies_every_order_without_issuing_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shop_ids`` 为空 ⇒ 站点集合 Ø ⇒ 订单查询被拒**且不发起 SQL**（不是「不限制」）。"""
    context = _resolve(monkeypatch, payload={**CLAIMS, "shop_ids": []})
    connection = Connection()
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **_: connection)

    authorizer = ScopedOrderAuthorizer(mysql_settings())

    assert authorizer.can_access(context, ORDER_INSIDE) is False
    assert authorizer.can_access(context, ORDER_OUTSIDE) is False
    assert order_queries(connection) == []


def test_an_order_outside_the_operators_sites_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """站点集合内的单放行、集合外的拒：证明拒绝不是「全部拒掉」。"""
    context = _resolve(monkeypatch, mapper=_Mapper({"SHOP-1": (SITE_IN,)}))
    connection = Connection()
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **_: connection)

    authorizer = ScopedOrderAuthorizer(mysql_settings())

    assert context.data_scope.site_ids == (SITE_IN,)
    assert authorizer.can_access(context, ORDER_INSIDE) is True
    assert authorizer.can_access(context, ORDER_OUTSIDE) is False


# --- 7. 部署接缝：配置三态 -----------------------------------------------------


def _gateway_settings(tmp_path: Path, config_text: str = "# test\n", **overrides: Any):
    config_file = tmp_path / "production.env"
    config_file.write_text(config_text, encoding="utf-8")
    os.chmod(config_file, 0o600)  # the config is a private file
    fields: dict[str, Any] = {
        "data_home": tmp_path,
        "database_file": tmp_path / "gateway.db",
        "server_config_file": config_file,
    }
    fields.update(overrides)
    return GatewayServerSettings(**fields)


COMPANY_KEYS: dict[str, Any] = {
    "company_check_token_url": CHECK_TOKEN_URL,
    "company_token_client_id": CLIENT_ID,
    "company_token_client_secret": CLIENT_SECRET,
}

#: #444 的入站来源密钥。长度必须 ≥ ``MIN_SOURCE_KEY_LENGTH``（16），否则启动失败。
SOURCE_KEY = "source-key-0123456789abcdef"


def test_the_company_path_is_not_built_while_all_keys_are_empty(tmp_path: Path) -> None:
    """全空 ⇒ 与启用前同型（UPMS 兜底），这是「先合不启用」的机制保证（#443/#444 同一条）。"""
    from aiops_diagnostics.caller_auth import UpmsCallerResolver
    from aiops_diagnostics.gateway_api import _caller_resolver

    settings = _gateway_settings(tmp_path, "AIOPS_UPMS_BASE_URL=https://upms.example.test\n")

    assert isinstance(_caller_resolver(settings), UpmsCallerResolver)


def test_the_company_path_is_not_built_without_the_source_key(tmp_path: Path) -> None:
    """⭐ #444 的核心：**没有来源密钥，新链路整体不参与**，即使校验入口与凭据都配好了。

    这条同时也是「门为什么必须在会话之前」的证据：``third_session_service_token`` 在这里
    **没有**配置（41 之外的环境），而一旦它配置上，落在下面的会话那一级就会直接 return ——
    把这条断言与下一条放在一起读，才是本票真正的形状。
    """
    from aiops_diagnostics.caller_auth import DisabledCallerResolver
    from aiops_diagnostics.gateway_api import _caller_resolver

    resolver = _caller_resolver(_gateway_settings(tmp_path, **COMPANY_KEYS))

    assert not isinstance(resolver, CompanyTokenCallerResolver)
    # 没有 introspection、没有 UPMS 地址 ⇒ 落到 fail-closed 的禁用解析器（拒绝，不是放行）。
    assert isinstance(resolver, DisabledCallerResolver)


def test_the_source_key_gate_is_built_once_key_and_company_path_are_configured(tmp_path: Path) -> None:
    from aiops_diagnostics.caller_auth import SourceKeyCallerResolver
    from aiops_diagnostics.gateway_api import _caller_resolver

    resolver = _caller_resolver(_gateway_settings(tmp_path, company_source_key=SOURCE_KEY, **COMPANY_KEYS))

    assert isinstance(resolver, SourceKeyCallerResolver)


def test_the_gate_sits_in_front_of_the_session_level(tmp_path: Path) -> None:
    """⭐ 41 现网形状：服务令牌有值（会话那一级会直接 return）**且**密钥已配置。

    这是本票最容易被写错的一处。若门排在会话那一级之后，带公司令牌、不带 ``third-session``
    的请求会在第一级就被 401 —— 密钥配了也白配。因此断言两层：
    ``_fallback`` 仍是会话解析器（既有链没被动过），而**外层**是门。
    """
    from aiops_diagnostics.caller_auth import SourceKeyCallerResolver
    from aiops_diagnostics.gateway_api import _caller_resolver
    from aiops_diagnostics.third_session_auth import RedisThirdSessionResolver

    resolver = _caller_resolver(
        _gateway_settings(
            tmp_path,
            "AIOPS_REDIS_PASSWORD=redis-secret\n",
            third_session_service_token="svc",
            company_source_key=SOURCE_KEY,
            **COMPANY_KEYS,
        )
    )

    assert isinstance(resolver, SourceKeyCallerResolver)
    assert isinstance(resolver._fallback, RedisThirdSessionResolver)
    assert isinstance(resolver._company, CompanyTokenCallerResolver)


def test_the_gate_wraps_the_existing_chain_without_changing_its_choice(tmp_path: Path) -> None:
    """既有链的选择结果零变化：门只是**包在外面**，未命中密钥时原样委派。"""
    from aiops_diagnostics.caller_auth import SourceKeyCallerResolver, UpmsCallerResolver
    from aiops_diagnostics.gateway_api import _caller_resolver

    settings = _gateway_settings(
        tmp_path,
        "AIOPS_UPMS_BASE_URL=https://upms.example.test\n",
        company_source_key=SOURCE_KEY,
        **COMPANY_KEYS,
    )

    resolver = _caller_resolver(settings)

    assert isinstance(resolver, SourceKeyCallerResolver)
    # UPMS 兜底原本会被 company_check_token_url 挡掉（#443 那一级条件）；#444 起那条路径
    # 不再参与既有链，兜底照旧 —— 这正是「门未命中 ⇒ 走既有链」的字面含义。
    assert isinstance(resolver._fallback, UpmsCallerResolver)


def test_a_half_configured_company_path_is_a_startup_error(tmp_path: Path) -> None:
    """只设 URL 不是静默禁用，而是启动失败：否则部署看起来正常、实际一直 401。"""
    with pytest.raises(ValueError, match="COMPANY_TOKEN_CLIENT"):
        _gateway_settings(tmp_path, company_check_token_url=CHECK_TOKEN_URL).validate()


def test_a_source_key_without_the_check_token_url_is_a_startup_error(tmp_path: Path) -> None:
    """#444：只配密钥等于「装了门但没有可路由的目标」—— 每条带密钥的请求都静默落回既有链。"""
    with pytest.raises(ValueError, match="COMPANY_CHECK_TOKEN_URL"):
        _gateway_settings(tmp_path, company_source_key=SOURCE_KEY).validate()


def test_a_short_source_key_is_a_startup_error(tmp_path: Path) -> None:
    """门对着可达的入口：短到可枚举的密钥等于没有这道门，因此在启动时就拒。"""
    with pytest.raises(ValueError, match="at least 16"):
        _gateway_settings(tmp_path, company_source_key="short-key", **COMPANY_KEYS).validate()


# --- 8. 端到端：真实应用层 -----------------------------------------------------


def _company_token_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mapper: _Mapper):
    connection = Connection()
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **_: connection)
    monkeypatch.setattr(
        "aiops_diagnostics.bounded_http.urllib.request.urlopen",
        lambda *_a, **_k: _Response(CLAIMS),
    )
    resolver = CompanyTokenCallerResolver(_settings(), mysql_settings(), scope_mapper_factory=mapper)
    # 真实解析器经既有的 caller_resolver 接缝注入：驱动的是真实的授权判定与范围下推。
    return assistant_app(tmp_path, monkeypatch, resolver, connection)  # type: ignore[arg-type]


def test_a_company_token_reaches_the_operator_actions_and_order_diagnosis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """令牌 → ``operator`` 动作列表 → 订单检测：站点内 ``202``、站点外 ``404``。"""
    client, runtime = _company_token_client(tmp_path, monkeypatch, _Mapper({"SHOP-1": (SITE_IN,)}))
    headers = {"Authorization": "Bearer company-token", "X-Business-Entry": "operator"}
    question = "帮我检测这个订单的充电异常"

    listed = client.get("/v1/shortcuts", headers=headers)
    inside = client.post(
        "/v1/assistant/questions",
        json={"question": question, "order_no": ORDER_INSIDE},
        headers=headers,
    )
    outside = client.post(
        "/v1/assistant/questions",
        json={"question": question, "order_no": ORDER_OUTSIDE},
        headers=headers,
    )

    assert listed.status_code == 200
    assert [item["code"] for item in listed.json()["shortcuts"]] == [
        "case_exploration",
        "smart_diagnosis",
    ]
    assert inside.status_code == 202
    assert runtime.diagnoses == [(ORDER_INSIDE, question)]
    assert outside.status_code == 404
    assert outside.json()["error"]["code"] == "ORDER_NOT_FOUND"


def test_the_consumer_entry_keeps_the_self_scope_for_a_company_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一个令牌、consumer 入口 ⇒ ``self``：不因入口换了就放宽到运营商站点集合。"""
    client, _ = _company_token_client(tmp_path, monkeypatch, _Mapper({"SHOP-1": (SITE_IN,)}))
    headers = {"Authorization": "Bearer company-token", "X-Business-Entry": "consumer"}

    # 集合内、但挂在**别人**名下的那单：consumer 入口必须看不到（否则就是 #436 的回归）。
    other = client.post(
        "/v1/assistant/questions",
        json={"question": "帮我检测这个订单的充电异常", "order_no": ORDER_INSIDE},
        headers=headers,
    )

    assert other.status_code == 404
    assert other.json()["error"]["code"] == "ORDER_NOT_FOUND"


# --- 9. #444：来源密钥门（分派 + fail closed）---------------------------------
#
# 断言的都是对外可观察行为：**哪条链被走到**（由替身记录）与 HTTP 结果。门本身只是一次
# 比较，真正的风险在「比较错了会怎样」，所以每条错误方向都有一条用例。


class _Recording:
    """记录被调用时收到的参数的解析器替身（两条链各一份，用来证明分派对不对）。"""

    def __init__(self, name: str, context: Any) -> None:
        self.name = name
        self.context = context
        self.calls: list[dict[str, Any]] = []

    def resolve(
        self,
        token: str,
        *,
        required_scope: str,
        third_session: str | None = None,
        platform_entry: str | None = None,
        source_key: str | None = None,
    ) -> Any:
        self.calls.append(
            {
                "token": token,
                "required_scope": required_scope,
                # 密钥**不入记录**：替身也不该把明文秘密留在内存断言里。
                "third_session": third_session,
                "platform_entry": platform_entry,
            }
        )
        return self.context


def _gate(source_key: str = SOURCE_KEY) -> tuple[SourceKeyCallerResolver, _Recording, _Recording]:
    company = _Recording("company", "company-context")
    fallback = _Recording("fallback", "fallback-context")
    return SourceKeyCallerResolver(source_key, company, fallback), company, fallback


def _resolve_through(
    gate: SourceKeyCallerResolver, *, source_key: str | None, third_session: str | None = "sess-1"
) -> Any:
    return gate.resolve(
        "opaque-token",
        required_scope="aiops:orders:read",
        third_session=third_session,
        platform_entry="operator",
        source_key=source_key,
    )


def test_the_correct_source_key_routes_to_the_company_path() -> None:
    """带且验过 ⇒ 走公司令牌路径，且**不把会话值递给它**（两种凭据不是一回事）。"""
    gate, company, fallback = _gate()

    assert _resolve_through(gate, source_key=SOURCE_KEY) == "company-context"
    assert len(company.calls) == 1
    assert fallback.calls == []
    assert company.calls[0]["third_session"] is None
    assert company.calls[0]["platform_entry"] == "operator"


@pytest.mark.parametrize("presented", [None, "", "wrong-key-0123456789abcde", SOURCE_KEY + "x"])
def test_a_missing_or_wrong_source_key_falls_back_to_the_existing_chain(presented: str | None) -> None:
    """不带 / 带错 ⇒ 走既有链，且入口与会话值**原样透传**（既有链的结果零变化）。"""
    gate, company, fallback = _gate()

    assert _resolve_through(gate, source_key=presented) == "fallback-context"
    assert company.calls == []
    assert len(fallback.calls) == 1
    assert fallback.calls[0]["third_session"] == "sess-1"
    assert fallback.calls[0]["platform_entry"] == "operator"


def test_a_near_miss_source_key_is_not_accepted() -> None:
    """前缀/大小写/空白都不算命中：显式钉住「比较不做任何规范化」。

    这四种写法都是「运维手抄时最可能的错法」，若比较宽容其中任意一种，有效密钥空间就缩了。
    """
    gate, company, _ = _gate()

    for near_miss in (SOURCE_KEY[:-1], SOURCE_KEY.upper(), f" {SOURCE_KEY}", f"{SOURCE_KEY} "):
        assert _resolve_through(gate, source_key=near_miss) != "company-context", near_miss
    assert company.calls == []


def test_an_empty_configured_key_never_opens_the_gate() -> None:
    """⭐ #444 的 fail closed：**未配置密钥 ⇒ 新链路整体不启用**（不是「没密钥就放行」）。"""
    with pytest.raises(ValueError, match="source key is required"):
        SourceKeyCallerResolver("", _Recording("company", None), _Recording("fallback", None))


def test_the_gate_is_inert_when_no_header_is_presented() -> None:
    """整条既有链的既有客户端**从来不带**这个头 —— 他们的行为必须一个字都不变。"""
    gate, company, fallback = _gate()

    assert _resolve_through(gate, source_key=None, third_session=None) == "fallback-context"
    assert company.calls == []
    assert fallback.calls[0]["third_session"] is None


def test_the_source_key_gate_decides_the_chain_through_the_real_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端，**且解析器由配置构造**：同一请求只换来源密钥这个头，就走两条真实链。

    这里刻意**不用** ``caller_resolver=`` 替身注入：门是在 ``_caller_resolver`` 里装上的，
    注入替身会绕过整条配置链，「接线把 header 传到了门」这件事就没人证明（改掉 wrapper 的
    ``source_key=`` 传参，注入式用例照样全绿 —— 试着改坏过一次，就是这样漏的）。
    因此应用走真实的 ``assistant_app`` 默认路径（内部调 ``_caller_resolver(settings)``），
    只有两处**外部 I/O** 换成替身：公司 check_token 的 HTTP 与既有链的 Redis 会话值。

    既有链一侧必须是真实的 ``RedisThirdSessionResolver``，否则「带错密钥 ⇒ 走既有链」会被
    「无论怎样都是同一个替身」蒙过去；服务令牌与 Redis 会话都配好，于是两条链各自成立。
    """
    connection = Connection()
    monkeypatch.setattr("aiops_diagnostics.sources.pymysql.connect", lambda **_: connection)
    monkeypatch.setattr(
        "aiops_diagnostics.bounded_http.urllib.request.urlopen", lambda *_a, **_k: _Response(CLAIMS)
    )
    # 唯一被替身顶掉的**内部**依赖：站点归属映射要连充电库。这条用例的对象是「header → 门 →
    # 哪条链」，不是映射本身（映射规则由 1–6 节覆盖）。顶在模块级而不是构造参数上，是因为
    # 解析器由真实配置链构造，没有注入点。
    monkeypatch.setattr("aiops_diagnostics.company_token_auth.mysql_site_mapper", _Mapper())

    class _Redis:
        """既有链要的会话值（与 ``operator_support._Redis`` 同一形状）。"""

        def get(self, _key: str) -> bytes:
            return java_session({"userId": C_USER_ID, "tenantId": TENANT_ID})

    monkeypatch.setattr("aiops_diagnostics.third_session_auth.redis.Redis", lambda **_: _Redis())
    settings = _gateway_settings(
        tmp_path,
        "AIOPS_REDIS_PASSWORD=redis-secret\n",
        third_session_service_token="svc",
        company_source_key=SOURCE_KEY,
        **COMPANY_KEYS,
    )
    client, runtime = assistant_app(tmp_path, monkeypatch, None, connection, settings=settings)
    question = "帮我检测这个订单的充电异常"

    # 带密钥 ⇒ 公司链：B 端主体名下站点集合 ⇒ 站点内的单可查。
    with_key = client.post(
        "/v1/assistant/questions",
        json={"question": question, "order_no": ORDER_INSIDE},
        headers={
            "Authorization": "Bearer company-token",
            "X-Business-Entry": "operator",
            "X-AIOps-Source-Key": SOURCE_KEY,
        },
    )
    # 不带密钥 ⇒ 既有链，且**走到会话解析器**：公司令牌不等于服务令牌 ⇒ 401（不是放行）。
    # 若门接错（比如门把 header 丢了、或门排在会话之后），这一条会变成别的东西：
    # 前者会拿到 202（公司链被误用），后者会拿到「公司令牌被当成会话值」的 401 —— 后者与本例
    # 结果同为 401，所以**另用一条**断言把两条 401 区分开（见下）。
    without_key = client.post(
        "/v1/assistant/questions",
        json={"question": question, "order_no": ORDER_INSIDE},
        headers={"Authorization": "Bearer company-token", "X-Business-Entry": "operator"},
    )
    # 既有链真正被走到：服务令牌 + 会话值 ⇒ 200 的动作列表（没有 202/404 的订单分支）。
    # 头名是 ``x-third-session``（下划线会变成连字符）—— ``third-session`` 与
    # ``x-third-session`` 是两个不同的头，用错了就落到「无会话」分支被拒，与本题无关却会让
    # 这条断言失去意义。
    session_chain = client.get(
        "/v1/shortcuts",
        headers={
            "Authorization": "Bearer svc",
            "X-Business-Entry": "operator",
            "x-third-session": SESSION_TOKEN,
        },
    )
    # **第二个入口**：``/v1/shortcuts`` 走的是 ``_authenticate_caller`` 那条共用依赖，而
    # ``/v1/assistant/questions`` 走的是内联的那条。两条各自把 ``source_key`` 传下去，因此
    # 两条都要有带密钥的请求打到：只测其中一条时，另一条把参数丢掉会**静默通过**（试过：
    # 把 ``_authenticate_caller`` 里的 ``source_key=`` 删掉，只测助手入口的用例照样全绿）。
    keyed_shortcuts = client.get(
        "/v1/shortcuts",
        headers={
            "Authorization": "Bearer company-token",
            "X-Business-Entry": "operator",
            "X-AIOps-Source-Key": SOURCE_KEY,
        },
    )
    # **第三个入口**：``/v1/orders/{order_no}/access`` 走 ``authenticated_caller``（内联的另一
    # 条）。它同样要有一条带密钥的请求：仓库里共有**三个**入口依赖各自把 ``source_key`` 传下去
    # （``authenticated_diagnosis_caller`` 内联、``_authenticate_caller`` 共用、
    # ``authenticated_caller`` 内联），只驱动其中两个时，第三个把参数丢掉仍会**静默通过**。
    keyed_order_access = client.get(
        f"/v1/orders/{ORDER_INSIDE}/access",
        headers={
            "Authorization": "Bearer company-token",
            "X-Business-Entry": "operator",
            "X-AIOps-Source-Key": SOURCE_KEY,
        },
    )

    assert with_key.status_code == 202
    assert runtime.diagnoses == [(ORDER_INSIDE, question)]
    assert without_key.status_code == 401
    assert without_key.json()["error"]["code"] == "INVALID_ACCESS_TOKEN"
    assert session_chain.status_code == 200
    assert keyed_shortcuts.status_code == 200
    assert keyed_order_access.status_code == 200
    assert keyed_order_access.json()["accessible"] is True
    assert SESSION_TOKEN not in without_key.text
