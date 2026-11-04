"""「什么算凭据」—— 两种形状、一个答案（#787）。

`redaction.py` 是本仓脱敏的单一落点，但「什么算凭据」在它内部按**数据形状**分叉：
映射的键走 `SENSITIVE_KEY.search`，自由文本走 `CREDENTIAL_ASSIGNMENT`。后者的 `\\b`
在 `_` 处不成立，于是同一批字段名在两种形状下得到相反答案 —— 写进用户提问、
知识库正文或证据 payload 的 `inside_token=…` 原样留下。

这里把两种形状放在一起断言，并钉住 URL userinfo 只有一处定义。
"""

from __future__ import annotations

import pytest

from aiops_diagnostics.common.config import _redact_url_credentials
from aiops_diagnostics.common.redaction import (
    TRUNCATION_MARKER,
    redact_text,
    redact_url_credentials,
    sanitize_data,
    truncate,
)

#: #787 点名的七个字段名：两个形状必须给出**同一个**答案。
CREDENTIAL_NAMES = [
    "token",
    "api_key",
    "password",
    "inside_token",
    "service_token",
    "access_token",
    "client_secret",
    "integration_api_key",
]


@pytest.mark.parametrize("name", CREDENTIAL_NAMES)
def test_both_shapes_agree_that_this_name_is_a_credential(name: str) -> None:
    assert sanitize_data({name: "REALSECRET"})[name] == "REDACTED"
    assert redact_text(f"{name}=REALSECRET") == f"{name}=REDACTED"


def test_signature_key_is_a_credential_in_both_shapes() -> None:
    """`CompanyTokenSettings.signature_key` 是本地验签用的密钥，两条判据都曾漏掉它。"""
    assert sanitize_data({"signature_key": "REALSECRET"})["signature_key"] == "REDACTED"
    assert redact_text("signature_key=REALSECRET") == "signature_key=REDACTED"


def test_a_whole_identifier_is_matched_not_only_a_prefix() -> None:
    """`inside_token=…` 里的名字是**整段标识符**，不是 `token` 前面的一段。"""
    assert redact_text("inside_token=abc") == "inside_token=REDACTED"
    assert redact_text("x_token=y") == "x_token=REDACTED"


@pytest.mark.parametrize(
    "text",
    [
        "the secret sauce is patience",  # 普通英文里的 secret 不是赋值
        "token 过期了请重新登录",  # 没有分隔符
        "连接 redis 超时了",  # 没有赋值形状
    ],
)
def test_ordinary_prose_is_left_alone(text: str) -> None:
    assert redact_text(text) == text


@pytest.mark.parametrize("scheme", ["redis", "mysql", "https", "amqp"])
def test_url_userinfo_is_redacted_for_every_scheme(scheme: str) -> None:
    """`https?://` 只是调用点的参数，不是唯一合法的 scheme。"""
    text = redact_text(f"connect {scheme}://user:s3cr3t@db.internal:6379 failed")
    assert "s3cr3t" not in text
    assert "user" not in text
    assert f"{scheme}://REDACTED@db.internal:6379" in text


def test_both_url_paths_give_the_same_answer() -> None:
    """`redact_text`（持久化/日志）与 `Settings.redacted()`（--show-config）同一结果。"""
    url = "mysql://root:hunter2@db.internal:3306/app"
    assert redact_text(f"dsn={url}") == f"dsn={redact_url_credentials(url)}"
    assert _redact_url_credentials(url) == redact_url_credentials(url)


def test_a_query_string_at_sign_is_not_mistaken_for_userinfo() -> None:
    url = "https://h/x?next=a@b"
    assert redact_url_credentials(url) == url


def test_truncation_marker_has_one_definition() -> None:
    long = "x" * 1300
    assert truncate(long) == "x" * 1000 + TRUNCATION_MARKER
    assert truncate("short") == "short"
    longer = "y" * 2100  # 超过 sanitize_data 的默认上限 2048
    assert sanitize_data({"blob": longer})["blob"].endswith(TRUNCATION_MARKER)


def test_a_preserved_order_number_survives_redaction() -> None:
    """`preserve` 是既有策略，本轮不改（#606 的 Further Notes 记着它需要单独确认）。"""
    assert "2096164064667852801" in redact_text(
        "订单 2096164064667852801 充电异常", preserve={"2096164064667852801"}
    )
