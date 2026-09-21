"""有界 HTTP 传输骨架：请求构造、异常映射、退避与信封解析的单一来源。

所有出站 HTTP（introspection / UPMS / Diag API / kb-service / Gateway 客户端 /
TDengine 代理 / Responses 适配器）走的是同一条管线：构造请求 → 装配认证头 →
``urlopen(timeout)`` → 异常分类 → UTF-8/JSON 解码 → 信封解析。本模块把这条管线
定义为**纯函数与不可变小数据类**：只用标准库 ``urllib.request``，不引入第三方
依赖，不持有运行时状态，也不定义类层次。

各调用点以声明式参数注入自己的领域错误类与错误码（``ErrorMapping``）、认证方式、
信封形态与重试策略。差异因此显式可见，而「哪些异常必须被捕获」「状态码代表凭证
问题还是上游不可用」「退避怎么算」只在骨架内定义一次。

本模块不定义任何领域错误类型：领域错误的类型、code 与消息文案仍归各调用点，
骨架只按声明的方式构造它们，对外行为零变化。

端点自身的语义仍留在调用点，由上面的原语直接组合：媒体 404 谓词、响应大小上限
后的分支、流式转发。这些是 PRD 明确要求保留的显式差异，不进骨架。
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import json
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, NoReturn
from urllib.parse import quote, urlencode

from aiops_diagnostics.http_auth import build_internal_token_headers

DEFAULT_TIMEOUT_SECONDS = 10.0

JSON_CONTENT_TYPE = "application/json"
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
TEXT_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
TENANT_ID_HEADER = "tenant-id"

#: 从错误响应体里取人类可读细节时依次尝试的键。
DETAIL_KEYS = ("detail", "msg", "message")

#: HTTP 状态码 → 语义分类的唯一权威表：凭证被拒与其它状态分开。
AUTH_REJECTED_STATUSES = frozenset({401, 403})

#: 传输层故障的捕获集合。``OSError`` 显式列出：它是前三个成员的公共父类，
#: 也是 kb-service 客户端一直依赖的兜底，列出来以免"少了 OSError"这类漂移。
TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    urllib.error.URLError,
    TimeoutError,
    ConnectionError,
    OSError,
    http.client.IncompleteRead,
)
#: 解码层故障的捕获集合（非 UTF-8 响应体、非法 JSON）。
DECODE_EXCEPTIONS: tuple[type[BaseException], ...] = (UnicodeDecodeError, json.JSONDecodeError)
#: 一个上游故障不得逃逸成未映射异常：调用点需要覆盖的完整集合。
TRANSPORT_FAILURES: tuple[type[BaseException], ...] = TRANSPORT_EXCEPTIONS + DECODE_EXCEPTIONS

#: UPMS 与 Diag API 共用的信封成功码：``code in (0, 200)``。
ENVELOPE_SUCCESS_CODES = frozenset({0, 200})

FailureKind = Literal["auth_rejected", "http_error", "unavailable", "invalid_body", "invalid_envelope"]

AUTH_REJECTED: FailureKind = "auth_rejected"
HTTP_ERROR: FailureKind = "http_error"
UNAVAILABLE: FailureKind = "unavailable"
INVALID_BODY: FailureKind = "invalid_body"
INVALID_ENVELOPE: FailureKind = "invalid_envelope"


# --------------------------------------------------------------------------
# 请求构造
# --------------------------------------------------------------------------


def quote_segment(value: str) -> str:
    """Percent-encode 一个 URL 路径段——路径段引用的统一规则。"""
    return quote(value, safe="")


def join_url(base_url: str, path: str = "", *, query: Mapping[str, str] | None = None) -> str:
    """拼接 base 与 path：base 去掉尾部斜杠，path 补上缺失的前导斜杠。

    ``query`` 按键排序后编码，保证同一组参数得到同一个 URL（字节级稳定）。
    """
    if path and not path.startswith(("/", "?", "#")):
        path = f"/{path}"
    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{urlencode(sorted(query.items()))}"
    return url


def basic_auth_header(client_id: str, client_secret: str) -> str:
    """Basic 认证头（introspection、TDengine 代理）。

    凭据先按路径段规则 quote，再 base64——客户端 id/密钥含 ``:`` 或非 ASCII
    时不会被截断。
    """
    credentials = f"{quote(client_id, safe='')}:{quote(client_secret, safe='')}"
    return f"Basic {base64.b64encode(credentials.encode('utf-8')).decode('ascii')}"


def bearer_auth_header(token: str) -> str:
    """Bearer 认证头（UPMS、kb-service 服务令牌）。"""
    return f"Bearer {token}"


def tenant_id_header(tenant_id: str) -> dict[str, str]:
    """kb-service 的租户头（该服务端用租户头而非 Authorization 限定范围）。"""
    return {TENANT_ID_HEADER: tenant_id}


#: Diag API 的内部令牌头。实现复用 http_auth 的单一来源，这里作为骨架的命名
#: 入口，让五种认证方式在同一处可见。
internal_token_headers = build_internal_token_headers


def json_body(payload: object) -> bytes:
    """JSON 请求体：``ensure_ascii=False`` 保持中文可读，UTF-8 编码。"""
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def form_body(payload: Mapping[str, str]) -> bytes:
    """表单请求体（introspection 的 ``token=...``）。"""
    return urlencode(payload).encode("utf-8")


@dataclass(frozen=True, slots=True)
class RequestSpec:
    """一次有界出站请求，在打开 socket 之前完整描述。

    ``max_read_bytes`` 与 ``follow_redirects`` 是少数端点才需要的显式参数
    （TDengine 代理的响应上限、TDengine 代理与 Responses 适配器的禁重定向），
    默认值即"读完整响应体、跟随重定向"。
    """

    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    max_read_bytes: int | None = None
    follow_redirects: bool = True


def build_request(spec: RequestSpec) -> urllib.request.Request:
    """按 spec 组装 urllib 请求；头名大小写由 urllib 自己归一化。"""
    return urllib.request.Request(
        spec.url,
        data=spec.body,
        headers=dict(spec.headers),
        method=spec.method,
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向会绕过端点自身的策略，因此永不跟随。"""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def open_response(spec: RequestSpec) -> Any:
    """按 spec 的超时打开请求，返回可作上下文管理器使用的响应。

    返回的是 urllib 的响应对象（``read()`` / ``status`` / ``headers``），骨架
    不包装它：TDengine 代理与 Responses 适配器需要原样转发。
    """
    request = build_request(spec)
    if spec.follow_redirects:
        return urllib.request.urlopen(request, timeout=spec.timeout)
    return urllib.request.build_opener(_NoRedirectHandler()).open(request, timeout=spec.timeout)


def read_body(response: Any, *, max_read_bytes: int | None = None) -> bytes:
    """读取响应体；设上限时多读一个字节，让调用方无需缓冲即可发现超限。"""
    if max_read_bytes is None:
        return response.read()
    return response.read(max_read_bytes + 1)


# --------------------------------------------------------------------------
# 异常映射
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HttpFailure:
    """一次已分类的传输故障，附带构造领域错误所需的信息。"""

    kind: FailureKind
    status: int | None = None
    detail: str = ""
    cause: BaseException | None = None


@dataclass(frozen=True, slots=True)
class ErrorMapping:
    """一个客户端声明：五类结果如何变成它自己的领域错误。

    字段按管线段落排列：HTTP 层（``auth_rejected`` / ``http_error``）、传输层
    （``unavailable``）、解码层（``invalid_body``）、信封层
    （``invalid_envelope``）。每个字段构造该客户端自己的异常类型、code 与消息，
    因此错误码词汇表与消息文案仍归各客户端；而"哪些故障必须被覆盖"由骨架单一定义。

    同一个客户端常把几类结果映射到同一条消息（例如 kb-service 不区分 HTTP 状态），
    此时传同一个 callable 即可——差异仍然显式。
    """

    auth_rejected: Callable[[HttpFailure], Exception]
    http_error: Callable[[HttpFailure], Exception]
    unavailable: Callable[[HttpFailure], Exception]
    invalid_body: Callable[[HttpFailure], Exception]
    invalid_envelope: Callable[[HttpFailure], Exception]
    auth_rejected_statuses: frozenset[int] = AUTH_REJECTED_STATUSES
    detail_keys: tuple[str, ...] = DETAIL_KEYS


def error_detail(exc: urllib.error.HTTPError, *, keys: tuple[str, ...] = DETAIL_KEYS) -> str:
    """尽力从错误响应体里取人类可读细节，取不到返回空串。"""
    with contextlib.suppress(Exception):
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        if isinstance(payload, Mapping):
            for key in keys:
                value = payload.get(key)
                if value:
                    return str(value)
    return ""


def classify_http_error(exc: urllib.error.HTTPError, *, mapping: ErrorMapping) -> HttpFailure:
    """按唯一权威表把一个 HTTPError 状态码归入语义分类。"""
    kind = AUTH_REJECTED if exc.code in mapping.auth_rejected_statuses else HTTP_ERROR
    detail = error_detail(exc, keys=mapping.detail_keys)
    return HttpFailure(kind=kind, status=exc.code, detail=detail, cause=exc)


def classify_transport_error(exc: BaseException) -> HttpFailure:
    """连接/超时/读中断故障在任何边界都是同一类：上游不可用。"""
    return HttpFailure(kind=UNAVAILABLE, detail=exc.__class__.__name__, cause=exc)


def classify_failure(exc: BaseException, *, mapping: ErrorMapping) -> HttpFailure:
    """把共享捕获集合里的任一故障归入语义分类。

    ``HTTPError`` 是 ``URLError`` 的子类，因此也在集合内，并优先按状态码分类；
    解码故障单独归为一类，不并入"上游不可用"。
    """
    if isinstance(exc, urllib.error.HTTPError):
        return classify_http_error(exc, mapping=mapping)
    if isinstance(exc, DECODE_EXCEPTIONS):
        return HttpFailure(INVALID_BODY, detail=exc.__class__.__name__, cause=exc)
    return classify_transport_error(exc)


def domain_error(failure: HttpFailure, mapping: ErrorMapping) -> Exception:
    """按分类取出该客户端声明的领域错误。"""
    if failure.kind == AUTH_REJECTED:
        return mapping.auth_rejected(failure)
    if failure.kind == UNAVAILABLE:
        return mapping.unavailable(failure)
    if failure.kind == INVALID_BODY:
        return mapping.invalid_body(failure)
    if failure.kind == INVALID_ENVELOPE:
        return mapping.invalid_envelope(failure)
    return mapping.http_error(failure)


def raise_transport_failure(exc: BaseException, *, mapping: ErrorMapping) -> NoReturn:
    """把共享捕获集合里的一个故障分类并抛成声明的领域错误。"""
    raise domain_error(classify_failure(exc, mapping=mapping), mapping) from exc


def decode_json(body: bytes, *, mapping: ErrorMapping) -> Any:
    """UTF-8 解码并解析 JSON；坏响应体是领域错误，不是裸异常。"""
    try:
        return json.loads(body.decode("utf-8"))
    except DECODE_EXCEPTIONS as exc:
        failure = HttpFailure(INVALID_BODY, detail=exc.__class__.__name__, cause=exc)
        raise mapping.invalid_body(failure) from exc


# --------------------------------------------------------------------------
# 信封解析
# --------------------------------------------------------------------------


def parse_raw_envelope(payload: Any, mapping: ErrorMapping) -> Any:
    """无信封：解码后的 JSON 原样返回（TDengine REST、UPMS ds 兜底）。

    对象与数组都接受——两者分别是不同调用点的真实形态；标量载荷没有任何调用点
    能消费，按信封不符处理。
    """
    if isinstance(payload, (Mapping, list)):
        return payload
    raise mapping.invalid_envelope(HttpFailure(INVALID_ENVELOPE, detail="invalid response"))


def parse_data_key_envelope(payload: Any, mapping: ErrorMapping) -> Any:
    """kb-service 信封：有 ``data`` 键取 ``data``，否则整个 payload。"""
    if isinstance(payload, Mapping):
        return payload.get("data", payload)
    if isinstance(payload, list):
        return payload
    raise mapping.invalid_envelope(HttpFailure(INVALID_ENVELOPE, detail="invalid response"))


def parse_code_data_envelope(payload: Any, mapping: ErrorMapping) -> Any:
    """UPMS / Diag API 信封：``code`` 必须是 0 或 200，然后取 ``data``。

    信封层的 401/403 与 HTTP 层同一张表：凭证被拒；其余拒绝码归上游不可用。
    客户端按 ``failure.kind`` 选择自己的错误码（UPMS 与 Diag API 的消息与
    HTTP 层不同，但错误码分流规则相同）。
    """
    if not isinstance(payload, Mapping):
        raise mapping.invalid_envelope(HttpFailure(INVALID_ENVELOPE, detail="invalid response"))
    code = payload.get("code")
    if code in ENVELOPE_SUCCESS_CODES:
        return payload.get("data")
    message = payload.get("msg")
    failure = HttpFailure(
        kind=AUTH_REJECTED if code in mapping.auth_rejected_statuses else UNAVAILABLE,
        detail="invalid response" if message is None else str(message),
    )
    raise domain_error(failure, mapping)


EnvelopeParser = Callable[[Any, ErrorMapping], Any]


# --------------------------------------------------------------------------
# 退避
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """显式声明的重试策略；默认一次尝试，不重试。

    ``methods`` 限定哪些方法是幂等可重试的（默认只有 GET：POST enroll /
    create_run 在服务端先生效后响应，重试可能重复兑换一次性注册码或重复创建
    诊断运行）。
    """

    max_retries: int = 0
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 8.0
    jitter_seconds: float = 1.0
    methods: frozenset[str] = frozenset({"GET"})

    @property
    def attempts(self) -> int:
        """总尝试次数；负数预算按"一次尝试、不重试"处理。"""
        return max(0, self.max_retries) + 1


#: 不需要重试的调用点显式声明这一份（也是默认值）。
NO_RETRY = RetryPolicy()


def retry_delay(attempt: int, *, policy: RetryPolicy) -> float:
    """指数退避加抖动——全仓唯一一份实现。

    ``min(base * 2**attempt, max_delay) + uniform(0, jitter)``：上限约束单次
    睡眠，抖动让共享上游重启后的并发客户端不会步调一致地重试。
    """
    delay = min(policy.base_delay_seconds * 2**attempt, policy.max_delay_seconds)
    return delay + random.uniform(0, policy.jitter_seconds)


def should_retry(
    attempt: int,
    *,
    policy: RetryPolicy,
    method: str = "GET",
    deadline: float | None = None,
    now: float | None = None,
) -> bool:
    """这次失败之后还能不能再试一次。

    ``deadline`` 是 ``time.monotonic()`` 时刻：轮询类调用方把自己的超时传下来，
    慢连接不会一路重试到远超超时。``now`` 仅用于测试注入时钟。
    """
    if attempt + 1 >= policy.attempts:
        return False
    if method not in policy.methods:
        return False
    if deadline is None:
        return True
    return (time.monotonic() if now is None else now) < deadline


# --------------------------------------------------------------------------
# 管线
# --------------------------------------------------------------------------


def request_json(
    spec: RequestSpec,
    *,
    mapping: ErrorMapping,
    envelope: EnvelopeParser = parse_raw_envelope,
    retry: RetryPolicy = NO_RETRY,
    deadline: float | None = None,
) -> Any:
    """执行一次有界请求并返回按信封解析后的 payload。

    管线在这里只定义一次：构造 → 打开 → 读取 → 解码 → 信封。只有瞬时传输故障
    会重试，且只限策略声明为幂等的方法、不越过 ``deadline``；HTTPError 是上游
    的明确响应，立即按声明映射，不重试。
    """
    attempt = 0
    while True:
        try:
            with open_response(spec) as response:
                body = read_body(response, max_read_bytes=spec.max_read_bytes)
            return envelope(decode_json(body, mapping=mapping), mapping=mapping)
        except TRANSPORT_EXCEPTIONS as exc:
            deliberate = isinstance(exc, urllib.error.HTTPError)
            if deliberate or not should_retry(attempt, policy=retry, method=spec.method, deadline=deadline):
                raise_transport_failure(exc, mapping=mapping)
            time.sleep(retry_delay(attempt, policy=retry))
            attempt += 1
