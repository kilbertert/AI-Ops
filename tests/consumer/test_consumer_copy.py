"""租户文案包：措辞可发布，占位符与数字不可改，历史答复按发布时的版本复现。

契约见 `docs/acceptance/consumer-copy-bundle/acceptance.feature`，取舍见 ADR-0183。
"""

from __future__ import annotations

import json
import pathlib

import pytest
from execution.test_app_runtime_dify_entry import dify_chat
from execution.test_app_runtime_node_entry import ORDER_A

from aiops_diagnostics.consumer.consumer_billing import billing_answer
from aiops_diagnostics.consumer.consumer_copy import (
    CopyBundleInvalid,
    bundled,
    pack,
    resolve,
    validate,
)
from aiops_diagnostics.consumer.consumer_order_chat import status_answer
from aiops_diagnostics.gateway.gateway_store import GatewayStore

TENANT = "TENANT-A"
ISO = "2026-10-29T12:34:45.123456+00:00"
STATUS = {
    "order_no": "2113549588485718017",
    "status_code": "1",
    "status_role": "finished",
    "observed_at": ISO,
    "source": "cpms.chargingInfo",
}
BILLING = {
    **STATUS,
    "source": "cpms.orderDetail",
    "order_amount": "27.36",
    "recorded_payment_amount": "0.00",
    "refund_status": "0",
    "recorded_refund_amount": None,
}


@pytest.fixture()
def store(tmp_path: pathlib.Path) -> GatewayStore:
    return GatewayStore(tmp_path / "gateway.db")


def _text(answer: dict) -> str:
    return answer["blocks"][0]["text"]


# ── 校验：改字可以，改结构不行 ──────────────────────────────────────────────


def test_overriding_wording_is_accepted():
    validated = validate({"languages": {"zh": {"status_lede": "这单已经「{status}」了。"}}})
    assert validated["zh"]["status_lede"] == "这单已经「{status}」了。"


@pytest.mark.parametrize(
    "bundle,reason",
    [
        ({"languages": {"zh": {"status_lede": "这单「充电结束」。"}}}, "占位符"),
        ({"languages": {"zh": {"status_lede": "{status}{nope}"}}}, "占位符"),
        ({"languages": {"zh": {"status_ledee": "{status}"}}}, "文案键"),
        ({"languages": {"zh": {"status_lede": "   "}}}, "不能为空"),
        ({"languages": {"xx": {"status_lede": "{status}"}}}, "不能改文案"),
        # zh-Hant 也是整段模板：明确拒绝，而不是"发布了但悄悄不生效"。
        ({"languages": {"zh-Hant": {"status_lede": "「{status}」"}}}, "不能改文案"),
        ({"languages": {}}, "非空"),
        ({"language": {"zh": {}}}, "languages"),
    ],
)
def test_invalid_bundles_are_rejected_with_the_reason(bundle, reason):
    with pytest.raises(CopyBundleInvalid) as excinfo:
        validate(bundle)
    assert reason in str(excinfo.value)


def test_too_long_copy_is_rejected():
    with pytest.raises(CopyBundleInvalid) as excinfo:
        validate({"languages": {"zh": {"status_lede": "长" * 401 + "{status}"}}})
    assert "400" in str(excinfo.value)


def test_placeholder_check_covers_every_language():
    """德语模板的占位符与中文不同（`{order_no}` 位置不同），校验必须逐语种比。"""
    with pytest.raises(CopyBundleInvalid):
        validate({"languages": {"de": {"billing_lede": "Abrechnung: {order_amount}"}}})


# ── 合并：只影响被覆盖的语种 ────────────────────────────────────────────────


def test_pack_only_touches_the_languages_it_overrides():
    overrides = {"zh": {"status_lede": "这单已经「{status}」了。"}}
    zh = pack("zh", overrides)
    de = pack("de", overrides)
    assert zh["status_lede"] == "这单已经「{status}」了。"
    # 只发布了中文时，德语仍是内置德语 —— 不能因为缺配置就退回中文。
    assert de["status_lede"] == bundled("de")["status_lede"]
    assert zh["source_line"] == bundled("zh")["source_line"]


# ── 存储：只增不改、内容相同不推进版本 ──────────────────────────────────────


def test_publishing_creates_versions_and_identical_content_does_not(store):
    first = {"languages": {"zh": {"status_lede": "这单「{status}」。"}}}
    assert store.publish_consumer_copy(TENANT, first, published_by="ops") == 1
    assert store.publish_consumer_copy(TENANT, first, published_by="ops") == 1
    second = {"languages": {"zh": {"status_lede": "这单现在「{status}」。"}}}
    assert store.publish_consumer_copy(TENANT, second, published_by="ops") == 2
    assert store.consumer_copy(TENANT)[0] == 2
    assert store.consumer_copy(TENANT, 1)[1] == first


def test_unpublished_tenant_reads_as_version_zero(store):
    assert store.consumer_copy("NOBODY") == (0, {})
    assert store.publish_consumer_copy(TENANT, {"languages": {"zh": {}}}, published_by="x") == 1


# ── 渲染：租户措辞生效，数字不动 ────────────────────────────────────────────


def test_published_wording_reaches_the_answer_but_numbers_do_not_move(store):
    default_text = _text(billing_answer(BILLING, "zh"))
    store.publish_consumer_copy(
        TENANT,
        {"languages": {"zh": {"billing_lede": "这单账单{parts}，就这样。"}}},
        published_by="ops",
    )
    tenant_pack = resolve(store, TENANT, version=1, language="zh")
    text = _text(billing_answer(BILLING, "zh", pack=tenant_pack))
    assert text.startswith("这单账单合计 27.36，系统记录的支付金额是 0.00，没有退款记录，就这样。")
    # 数字、来源人话、免责句都还在：文案只改词，不改事实。
    assert "27.36" in text and "0.00" in text
    assert "不等于银行已经扣款" in text and "订单账务记录" in text
    assert text != default_text


def test_english_copy_is_composable_too(store):
    """`en` 与 `zh` 走同一条组合式渲染，所以它也能改；这同时证明不是只有中文特判。"""
    store.publish_consumer_copy(
        TENANT,
        {"languages": {"en": {"status_lede": "Order {status} right now."}}},
        published_by="ops",
    )
    tenant_pack = resolve(store, TENANT, version=1, language="en")
    text = _text(status_answer(STATUS, "en", localized=True, pack=tenant_pack))
    assert text.startswith("Order finished right now.")
    assert STATUS["order_no"] in text


# ── 复现：钉住的版本不受后续发布影响 ────────────────────────────────────────


def test_resolve_follows_the_pinned_version_not_the_latest(store):
    store.publish_consumer_copy(
        TENANT, {"languages": {"zh": {"status_lede": "第一版「{status}」。"}}}, published_by="ops"
    )
    store.publish_consumer_copy(
        TENANT, {"languages": {"zh": {"status_lede": "第二版「{status}」。"}}}, published_by="ops"
    )
    assert resolve(store, TENANT, version=1, language="zh")["status_lede"] == "第一版「{status}」。"
    assert resolve(store, TENANT, version=2, language="zh")["status_lede"] == "第二版「{status}」。"
    # 没有钉版本的历史答复按内置文案渲染 —— 与引入文案包之前逐字相同。
    assert resolve(store, TENANT, version=None, language="zh") == bundled("zh")
    assert resolve(store, TENANT, version=0, language="zh") == bundled("zh")


# ── 端到端：发布新文案后，在途答复的回读仍是旧措辞 ──────────────────────────


def test_readback_after_a_new_publish_keeps_the_original_wording(tmp_path, monkeypatch):
    from consumer.test_consumer_live_context import Flow, ask, done, wire

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = wire(env, monkeypatch, tmp_path)
        runtime.store.publish_consumer_copy(
            TENANT,
            {"languages": {"zh": {"status_lede": "第一版：「{status}」。"}}},
            published_by="ops",
        )
        Flow.skip = False
        first = ask(env).json()
        assert done(env, first["qa_id"]).json()["status"] == "completed"
        original = done(env, first["qa_id"]).json()["result"]
        assert original["blocks"][0]["text"].startswith("第一版：「")
        assert ORDER_A in original["blocks"][0]["text"]

        # 运营改了措辞：新答复用新版，**旧那条回读仍是旧措辞**（否则就是 503）。
        runtime.store.publish_consumer_copy(
            TENANT,
            {"languages": {"zh": {"status_lede": "第二版：「{status}」。"}}},
            published_by="ops",
        )
        again = done(env, first["qa_id"])
        assert again.status_code == 200, again.text
        assert again.json()["result"] == original

        second = ask(env).json()
        assert done(env, second["qa_id"]).json()["status"] == "completed"
        latest = done(env, second["qa_id"]).json()["result"]
        assert latest["blocks"][0]["text"].startswith("第二版：「")


def test_tampered_readback_is_still_refused(tmp_path, monkeypatch):
    """按版本复现不能变成"什么都放行"：正文被改过仍要拒绝。"""
    import sqlite3

    from consumer.test_consumer_live_context import Flow, ask, done, wire

    with dify_chat(tmp_path, monkeypatch, stub_base="http://127.0.0.1:1") as env:
        runtime, _ = wire(env, monkeypatch, tmp_path)
        Flow.skip = False
        first = ask(env).json()
        assert done(env, first["qa_id"]).json()["status"] == "completed"
        with sqlite3.connect(env["db"]) as db:
            db.execute(
                "UPDATE assistant_questions SET result_json=? WHERE qa_id=?",
                (
                    json.dumps(
                        {"blocks": [{"kind": "text", "text": "改过的正文"}], "retrieval_status": "not_found"}
                    ),
                    first["qa_id"],
                ),
            )
        refused = done(env, first["qa_id"])
        assert refused.status_code != 200
        assert "改过的正文" not in refused.text
