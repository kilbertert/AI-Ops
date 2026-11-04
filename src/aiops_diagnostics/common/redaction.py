from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

SENSITIVE_KEY = re.compile(
    r"(?:^id$|password|passwd|secret|token|api[_-]?key|signature[_-]?key|authorization|cookie|vin|"
    r"card[_-]?id|balance[_-]?card|pay[_-]?card|prepay[_-]?id|user[_-]?id|"
    r"created[_-]?by|updated[_-]?by|mobile|phone|plate|car[_-]?no|car[_-]?id|"
    r"group[_-]?id|out[_-]?trade[_-]?no|mac|raw)$",
    re.IGNORECASE,
)
#: 自由文本里「名字=值」的赋值。名字用**整段标识符**匹配，不靠 `\b` —— `_` 是单词
#: 字符，`\b(inside_token)\b` 在 `inside_token=` 上不成立，于是同一批字段名在映射键与
#: 自由文本里给出相反答案（#787）。
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"(password|passwd|secret|token|api[_-]?key|[A-Za-z0-9_]*_(?:secret|token|key))"
    r"\s*[:=]\s*([^\s,;]+)"
)
#: URL 里的 userinfo，**任何 scheme** 都算（`redis://u:p@h` 与 `https://u:p@h` 同一件事）。
#: 走 `urlsplit` 而不是正则：只有 userinfo 部分被替换，查询串里的 `@` 不会被误伤。
URL_CREDENTIALS = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/@\s]*@")
BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{12,}")
OPENAI_STYLE_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
LABELED_PII = re.compile(
    r"(?i)(手机号|手机|电话|phone|mobile|VIN|车架号|卡号|card[_ -]?id|车牌|plate)\s*[:：=]?\s*"
    r"([A-Za-z0-9\u4e00-\u9fff._-]{5,32})"
)
PHONE_NUMBER = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
VIN_VALUE = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])", re.IGNORECASE)
LONG_ACCOUNT = re.compile(r"(?<!\d)\d{12,20}(?!\d)")
PLATE_VALUE = re.compile(
    r"(?<![\u4e00-\u9fffA-Z0-9])[\u4e00-\u9fff][A-Z][A-Z0-9]{5,6}"
    r"(?![\u4e00-\u9fffA-Z0-9])",
    re.IGNORECASE,
)


#: 「一段文本被截断」的标记与上限，**只在这里定义**（#787）。
TRUNCATION_MARKER = "...[TRUNCATED]"
ERROR_TEXT_LIMIT = 1000
ERROR_TEXT_SHORT_LIMIT = 500


def truncate(text: str, limit: int = ERROR_TEXT_LIMIT) -> str:
    """截断并标记。调用点不再各自写 `[:1000]` / `[:500]`。"""
    if len(text) <= limit:
        return text
    return text[:limit] + TRUNCATION_MARKER


def redact_url_credentials(url: str) -> str:
    """把 URL 里的 userinfo 换成 `REDACTED`，**任何 scheme**。URL userinfo 的唯一定义。

    `urlsplit` 无法解析时退回整串 `REDACTED` —— 一条看起来像 URL 的文本里带着
    解析不了的 userinfo，宁可整串抹掉。
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "REDACTED"
    if parsed.username is None and parsed.password is None:
        return url
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, "REDACTED@" + hostname, parsed.path, parsed.query, parsed.fragment))


def _redact_url_userinfo_in_text(text: str) -> str:
    """自由文本里的 URL：先粗筛出含 userinfo 的片段，再逐个走 `urlsplit`。

    粗筛只是找出「值得解析一下」的片段；规则本身只有 `redact_url_credentials` 一份。
    """
    return URL_CREDENTIALS.sub(lambda match: redact_url_credentials(match.group(0)), text)


def redact_text(value: str, *, preserve: Iterable[str] = ()) -> str:
    preserved = {item for item in preserve if item}
    text = _redact_url_userinfo_in_text(value)
    text = BEARER_TOKEN.sub("Bearer REDACTED", text)
    text = OPENAI_STYLE_KEY.sub("REDACTED", text)
    text = CREDENTIAL_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=REDACTED", text)

    def replace_labeled(match: re.Match[str]) -> str:
        candidate = match.group(2)
        if candidate in preserved:
            return match.group(0)
        return f"{match.group(1)}=REDACTED"

    text = LABELED_PII.sub(replace_labeled, text)

    def replace_unlabeled(match: re.Match[str]) -> str:
        return match.group(0) if match.group(0) in preserved else "REDACTED"

    for pattern in (PHONE_NUMBER, VIN_VALUE, LONG_ACCOUNT, PLATE_VALUE):
        text = pattern.sub(replace_unlabeled, text)
    return text


def sanitize_data(
    value: Any,
    *,
    preserve: Iterable[str] = (),
    max_string_length: int = 2048,
) -> Any:
    preserved = tuple(item for item in preserve if item)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if SENSITIVE_KEY.search(text_key):
                result[text_key] = "REDACTED"
            else:
                child_preserved = preserved
                if text_key in {
                    "order_no",
                    "tenant_id",
                    "txSerialNo",
                    "transaction_id",
                    "device_id",
                    "device_code",
                    "child_device_id",
                    "child_device_code",
                } and item not in (None, ""):
                    child_preserved = (*preserved, str(item))
                result[text_key] = sanitize_data(
                    item,
                    preserve=child_preserved,
                    max_string_length=max_string_length,
                )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            sanitize_data(item, preserve=preserved, max_string_length=max_string_length) for item in value
        ]
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, str):
        text = redact_text(value, preserve=preserved)
        if len(text) > max_string_length:
            return text[:max_string_length] + TRUNCATION_MARKER
        return text
    return value


def contains_secret(text: str, sensitive_values: Iterable[str]) -> bool:
    return any(value and len(value) >= 4 and value in text for value in sensitive_values)
