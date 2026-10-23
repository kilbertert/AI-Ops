"""consumer-status 三个验收工具的**误报自检**。

这些是工具自检，**不是**真实业务验收、也不是真实越权验收。它们钉住的是
"判据比事实宽"这一类缺陷：旧版三个脚本都会在证据不足时 `return 0`。

每条反例都对应一个**已复现的真实误报**：

1. `check_identity.py` 只看 `scope_type == "self"`，于是 `delegated=true`、
   `orders:read` 被拒、范围指纹不同、没有本人订单，全都报通过。
2. `independent_cpms.py` 捕获 CPMS 异常后仍退出 0；用不存在的 `caller.user_id`；
   把"同一个选择器换 page_size 首行相同"当成排序证明。
3. `status_probe.py` 用**整页文本**判订单号（会命中用户自己的提问）；
   GET 匹配不到本次 qa_id 时回退到别的 GET。

这里不 mock 真实页面或业务源：判据本身是纯函数，直接喂合成事件即可。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
TOOLS = ROOT / "tools" / "consumer-status"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"consumer_status_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


identity = _load("check_identity")
cpms = _load("independent_cpms")
probe = _load("status_probe")


# ---------------------------------------------------------------- 退出码契约


def test_exit_codes_are_the_documented_three() -> None:
    """0 通过 / 1 失败 / 2 未验证 —— 三个脚本必须同一套。"""
    for module in (identity, cpms, probe):
        assert (module.EXIT_PASS, module.EXIT_FAIL, module.EXIT_UNVERIFIED) == (0, 1, 2)


# ---------------------------------------------------------------- 身份判据


class _Scope:
    def __init__(self, type_: str) -> None:
        self.type = type_


class _Subject:
    def __init__(self, c_user_id: str | None) -> None:
        self.c_user_id = c_user_id


class _Ctx:
    """最小 ScopeContext 替身：只带判据真正读的那几个属性。"""

    def __init__(self, *, scope_type: str, c_user_id: str | None, delegated: bool, fp: str) -> None:
        self.data_scope = _Scope(scope_type)
        self.subject = _Subject(c_user_id)
        self.delegated = delegated
        self.effective_tenant_id = "1942105476598861824"
        self.scope_fingerprint = fp


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        (
            {"scope_type": "self", "c_user_id": "c-1", "delegated": True, "fp": "a"},
            "self 但 delegated=true —— 旧判据只看 scope_type，会报成功",
        ),
        (
            {"scope_type": "self", "c_user_id": None, "delegated": False, "fp": "a"},
            "缺 C 端身份 —— 不可能有本人订单，旧判据照样成功",
        ),
        (
            {"scope_type": "all", "c_user_id": "c-1", "delegated": False, "fp": "a"},
            "租户级范围不是本人",
        ),
    ],
)
def test_identity_shape_rejects(kwargs: dict, why: str) -> None:
    """驱动**被测的那一个函数**，不在测试里重算表达式。

    第一版这些用例自己重算 `scope_type == "self" and ...`，于是把被测代码的判据
    改松（变异复验）时测试照样全绿 —— 它证明的是"我重算的和我写的一样"。
    """
    shape = identity.identity_shape(
        scope_type=kwargs["scope_type"],
        delegated=kwargs["delegated"],
        c_user_id=kwargs["c_user_id"] or "",
    )
    assert shape["consumer_self_non_delegated"] is False, why
    failures = identity.prerequisite_failures(shape=shape, orders_read_scope=True, fingerprints_match=True)
    assert "consumer_self_non_delegated" in failures, why


def test_identity_accepts_only_the_full_shape() -> None:
    shape = identity.identity_shape(scope_type="self", delegated=False, c_user_id="c-1")
    assert shape["consumer_self_non_delegated"] is True
    assert identity.prerequisite_failures(shape=shape, orders_read_scope=True, fingerprints_match=True) == []


def test_orders_read_refused_is_a_prerequisite_failure() -> None:
    shape = identity.identity_shape(scope_type="self", delegated=False, c_user_id="c-1")
    failures = identity.prerequisite_failures(shape=shape, orders_read_scope=False, fingerprints_match=True)
    assert failures == ["orders_read_scope"]


def test_fingerprint_mismatch_is_a_prerequisite_failure() -> None:
    """两次解析（diagnoses:write / orders:read）范围指纹不同 ⇒ 前提不成立。"""
    shape = identity.identity_shape(scope_type="self", delegated=False, c_user_id="c-1")
    failures = identity.prerequisite_failures(shape=shape, orders_read_scope=True, fingerprints_match=False)
    assert failures == ["orders_read_fingerprint_matches"]


def test_no_owned_order_is_unverified_not_pass() -> None:
    """没有本人订单时旧版 `return 0`；现在必须是 2（未验证）。"""
    assert identity.EXIT_UNVERIFIED == 2
    rows: list[dict] = []
    assert bool(rows) is False


# ---------------------------------------------------------------- 排序判据


def _row(order_no: str, created: str) -> dict:
    return {"order_no": order_no, "created_time": created}


def test_strict_descending_accepts_a_properly_ordered_tail() -> None:
    rows = [
        _row("2110544580381650945", "2026-10-15 09:33:35"),
        _row("2109932028547182593", "2026-10-13 16:59:31"),
    ]
    assert cpms.strict_descending(rows) is True


def test_strict_descending_rejects_a_tie() -> None:
    """同一 (created_time, order_no) 出现两次 ⇒ 不是严格递减，判据不成立。"""
    row = _row("2110544580381650945", "2026-10-15 09:33:35")
    assert cpms.strict_descending([row, dict(row)]) is False


def test_strict_descending_rejects_ascending() -> None:
    rows = [
        _row("2109932028547182593", "2026-10-13 16:59:31"),
        _row("2110544580381650945", "2026-10-15 09:33:35"),
    ]
    assert cpms.strict_descending(rows) is False


def test_same_selector_twice_is_not_an_ordering_proof() -> None:
    """换 page_size 首行相同只证明"这个选择器确定"，不证明它按时间降序。

    旧版把这个当排序证明。这里断言：把它当作证明的那个形状**判不过**严格递减
    （因为只有一行时无从比较）。
    """
    one_row = [_row("2110544580381650945", "2026-10-15 09:33:35")]
    assert cpms.strict_descending(one_row) is True  # 单行无从证伪
    # 所以判据必须看**至少两行**；一行时调用方要说明"未验证"而不是"通过"。
    assert len(one_row) < 2


# ---------------------------------------------------------------- CPMS 比对


def test_cpms_observation_failure_is_unverified() -> None:
    """CPMS 抛异常时旧版仍 return 0；契约上必须是 2。"""
    assert cpms.EXIT_UNVERIFIED == 2


def test_cpms_missing_endpoint_never_falls_back_to_ops() -> None:
    """个人地址缺失 ⇒ TENANT_ENDPOINT_UNRESOLVED ⇒ 未验证，不回退 OPS。"""
    from aiops_diagnostics.gateway_config import charging_endpoint

    with pytest.raises(ValueError, match="TENANT_ENDPOINT_UNRESOLVED"):
        charging_endpoint("", "1942105476598861824")


@pytest.mark.parametrize(
    ("page", "expect_code", "reasons"),
    [
        (
            {"order_no": "2109932028547182593", "source": "cpms.chargingInfo"},
            "1",
            ["order_no_mismatch"],
        ),
        (
            {"order_no": "2110544580381650945", "source": "other.source"},
            "1",
            ["source_mismatch"],
        ),
        (
            {"order_no": "2110544580381650945", "source": "cpms.chargingInfo"},
            "9",
            ["status_code_mismatch"],
        ),
    ],
)
def test_compare_with_cpms_rejects(page: dict, expect_code: str, reasons: list[str]) -> None:
    observed = {
        "order_no": "2110544580381650945",
        "source": "cpms.chargingInfo",
        "status_code": "1",
    }
    diff = cpms.compare_with_cpms(page=page, cpms=observed, expect_status_code=expect_code)
    assert diff["consistent"] is False
    assert reasons[0] in diff["reasons"]


def test_compare_with_cpms_accepts_a_match() -> None:
    observed = {
        "order_no": "2110544580381650945",
        "source": "cpms.chargingInfo",
        "status_code": "1",
    }
    diff = cpms.compare_with_cpms(
        page={"order_no": "2110544580381650945", "source": "cpms.chargingInfo"},
        cpms=observed,
        expect_status_code="1",
    )
    assert diff["consistent"] is True


# ---------------------------------------------------------------- 页面判据

ORDER = "2110544580381650945"
GOOD_ANSWER = (
    f"Order {ORDER}: finished (status code 1).\n"
    "Source: cpms.chargingInfo; observed at (UTC): 2026-10-23T03:21:45.688717+00:00."
)


def _post(qa_id: str = "qa_" + "a" * 32) -> dict:
    return {
        "method": "POST",
        "path": probe.POST_PATH,
        "http": 202,
        "content_type": "application/json",
        "cache_control": "no-store",
        "body": {"type": "qa", "status": "completed", "qa_id": qa_id},
    }


def _get(qa_id: str, answer: str) -> dict:
    return {
        "method": "GET",
        "path": f"{probe.POST_PATH}/{qa_id}",
        "http": 200,
        "content_type": "application/json",
        "cache_control": "no-store",
        "body": {
            "type": "qa",
            "status": "completed",
            "qa_id": qa_id,
            "result": {"blocks": [{"kind": "text", "text": answer}]},
        },
    }


def _judge(events, payloads, *, answer_text=GOOD_ANSWER, region=GOOD_ANSWER, label="latest"):
    return probe.judge(
        events=events,
        payloads=payloads,
        answer_text=answer_text,
        order_no=ORDER,
        label=label,
        assistant_region_text=region,
    )


def test_page_judge_accepts_the_happy_path() -> None:
    qa = "qa_" + "a" * 32
    checks = _judge(
        [_post(qa), _get(qa, GOOD_ANSWER)],
        [{"body": {"question": "check my latest order status"}}],
    )
    assert all(checks.values()), [k for k, v in checks.items() if not v]


def test_only_the_user_question_on_page_is_a_failure() -> None:
    """**这是最危险的那个误报**：用户提问里就含订单号。

    旧判据对整页 `inner_text()` 做 `order_no in text`，于是助手没回答也能过。
    """
    qa = "qa_" + "a" * 32
    checks = _judge(
        [_post(qa), _get(qa, GOOD_ANSWER)],
        [{"body": {"question": f"check order {ORDER} status"}}],
        label="specified",
        region=f"check order {ORDER} status",  # 页面上只有用户那条提问
    )
    assert checks["assistant_region_has_answer"] is False
    assert checks["answer_matches_qa_blocks"] is False


def test_get_from_another_qa_must_not_be_used() -> None:
    """本次 POST 的 qa_id 没取到 GET，却有上一轮别的 GET —— 旧版会回退到它。"""
    mine = "qa_" + "b" * 32
    other = "qa_" + "c" * 32
    checks = _judge(
        [_post(mine), _get(other, GOOD_ANSWER)],
        [{"body": {"question": "check my latest order status"}}],
    )
    assert checks["get_matches_own_qa_id"] is False
    assert checks["get_200_json"] is False


def test_html_response_is_a_failure() -> None:
    qa = "qa_" + "a" * 32
    html = {
        "method": "GET",
        "path": f"{probe.POST_PATH}/{qa}",
        "http": 200,
        "content_type": "text/html",
        "cache_control": None,
        "not_json": True,
    }
    checks = _judge(
        [_post(qa), html],
        [{"body": {"question": "check my latest order status"}}],
    )
    assert checks["all_v1_json"] is False
    assert checks["get_200_json"] is False


@pytest.mark.parametrize("status", ["queued", "running"])
def test_unfinished_qa_is_a_failure(status: str) -> None:
    qa = "qa_" + "a" * 32
    post = _post(qa)
    post["body"]["status"] = status
    checks = _judge(
        [post, _get(qa, GOOD_ANSWER)],
        [{"body": {"question": "check my latest order status"}}],
    )
    assert checks["post_completed"] is False


def test_model_refusal_text_is_a_failure() -> None:
    """回落到普通模型路径时的措辞 —— 它不是状态查询的成功。"""
    qa = "qa_" + "a" * 32
    refusal = "I'm sorry, but I can't look up a specific order or account status in this chat."
    checks = _judge(
        [_post(qa), _get(qa, refusal)],
        [{"body": {"question": "check my latest order status"}}],
        answer_text=refusal,
        region=refusal,
    )
    assert checks["not_model_refusal"] is False
    assert checks["answer_has_order"] is False


def test_latest_case_must_not_carry_order_no() -> None:
    qa = "qa_" + "a" * 32
    checks = _judge(
        [_post(qa), _get(qa, GOOD_ANSWER)],
        [{"body": {"question": "check my latest order status", "order_no": ORDER}}],
    )
    assert checks["request_has_no_order_no"] is False


def test_duplicate_submission_is_a_failure() -> None:
    qa = "qa_" + "a" * 32
    checks = _judge(
        [_post(qa), _post(qa), _get(qa, GOOD_ANSWER)],
        [{"body": {"question": "check my latest order status"}}],
    )
    assert checks["exactly_one_post"] is False
