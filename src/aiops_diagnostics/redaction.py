from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

SENSITIVE_KEY = re.compile(
    r"(?:^id$|password|passwd|secret|token|api[_-]?key|authorization|cookie|vin|"
    r"card[_-]?id|balance[_-]?card|pay[_-]?card|prepay[_-]?id|user[_-]?id|"
    r"created[_-]?by|updated[_-]?by|mobile|phone|plate|car[_-]?no|car[_-]?id|"
    r"group[_-]?id|out[_-]?trade[_-]?no|mac|raw)$",
    re.IGNORECASE,
)
CREDENTIAL_ASSIGNMENT = re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*([^\s,;]+)")
URL_CREDENTIALS = re.compile(r"(?P<scheme>https?://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE)
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


def redact_text(value: str, *, preserve: Iterable[str] = ()) -> str:
    preserved = {item for item in preserve if item}
    text = URL_CREDENTIALS.sub(r"\g<scheme>REDACTED@", value)
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
            return text[:max_string_length] + "...[TRUNCATED]"
        return text
    return value


def contains_secret(text: str, sensitive_values: Iterable[str]) -> bool:
    return any(value and len(value) >= 4 and value in text for value in sensitive_values)
