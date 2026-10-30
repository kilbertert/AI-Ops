"""consumer-status 验收工具的**执行路径**测试。

上一轮（#1004）的教训：那些测试只检查常量和自造变量，于是 Codex 把
"无本人订单"分支和"CPMS 异常"分支**分别改成 EXIT_PASS**，27 条测试照样全绿。
判据对了，但它没有被执行路径驱动 —— 等于没测。

这一轮每个用例都调用**真实的 `main(...)`**，外部身份、SQL、CPMS 全部换成隔离替身，
并断言三件事：退出码、报告里的结论、**业务调用次数**。

这些是工具自检，**不是**真实业务验收，也不是真实越权验收。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
TOOLS = ROOT / "tools" / "verification" / "consumer-status"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"cs_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


identity = _load("check_identity")
cpms = _load("independent_cpms")
probe = _load("status_probe")


# ---------------------------------------------------------------- 替身


class _Scope:
    def __init__(self, type_: str) -> None:
        self.type = type_


class _Subject:
    def __init__(self, c_user_id: str | None) -> None:
        self.c_user_id = c_user_id


class Caller:
    def __init__(
        self,
        *,
        scope_type: str = "self",
        c_user_id: str | None = "c-1",
        delegated: bool = False,
        fingerprint: str = "fp-1",
        tenant: str = "T-1",
    ) -> None:
        self.data_scope = _Scope(scope_type)
        self.subject = _Subject(c_user_id)
        self.delegated = delegated
        self.scope_fingerprint = fingerprint
        self.effective_tenant_id = tenant


class Resolver:
    """按 scope 返回预置的 caller；scope 缺失即拒绝（与生产同语义）。"""

    def __init__(self, by_scope: dict[str, object]) -> None:
        self.by_scope = by_scope
        self.calls: list[str] = []

    def resolve(self, *, required_scope: str, third_session: str, platform_entry: str):
        self.calls.append(required_scope)
        value = self.by_scope.get(required_scope)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RuntimeError("rejected")
        return value


def _identity_deps(*, diagnosing, reading, rows, counter: list[int] | None = None):
    """构造 check_identity 的 Deps 替身；`counter` 用来数业务查询次数。"""
    counter = counter if counter is not None else [0]
    resolver = Resolver({identity.SCOPE_DIAGNOSES: diagnosing, identity.SCOPE_ORDERS_READ: reading})

    def make_resolver():
        return resolver

    def make_source(_caller):
        counter[0] += 1
        return rows

    return (
        identity.Deps(
            make_resolver=make_resolver,
            make_source=make_source,
            session="sess",
            service_token="tok",
        ),
        counter,
    )


@pytest.fixture(autouse=True)
def _session_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOPS_CHECK_SESSION", "sess")


def _run_identity(deps) -> int:
    return identity.main([], deps=deps)


# ---------------------------------------------------------------- check_identity


def test_identity_happy_path_exits_zero_and_queries_once() -> None:
    good = Caller()
    deps, counter = _identity_deps(diagnosing=good, reading=good, rows=[{"order_no": "A", "id": 1}])
    assert _run_identity(deps) == identity.EXIT_PASS
    assert counter[0] == 1


def test_no_owned_order_is_unverified_and_does_not_pass() -> None:
    """Codex 把这一分支改成 EXIT_PASS 后旧测试仍全绿 —— 这里必须抓住。"""
    good = Caller()
    deps, counter = _identity_deps(diagnosing=good, reading=good, rows=[])
    code = _run_identity(deps)
    assert code == identity.EXIT_UNVERIFIED, "没有本人订单不能报通过"
    assert code != identity.EXIT_PASS
    # 业务查询确实尝试过（要看得到"没有订单"，就必须查过）
    assert counter[0] == 1


@pytest.mark.parametrize(
    "caller_kwargs",
    [
        {"delegated": True},
        {"c_user_id": None},
        {"scope_type": "all"},
    ],
)
def test_identity_prerequisites_block_the_business_query(caller_kwargs: dict) -> None:
    """身份不满足时，**业务查询次数必须为零**。"""
    bad = Caller(**caller_kwargs)
    deps, counter = _identity_deps(diagnosing=bad, reading=bad, rows=[{"order_no": "A", "id": 1}])
    assert _run_identity(deps) == identity.EXIT_FAIL
    assert counter[0] == 0, "前提不成立却发起了业务查询"


def test_orders_read_rejected_blocks_the_business_query() -> None:
    good = Caller()
    deps, counter = _identity_deps(
        diagnosing=good,
        reading=RuntimeError("denied"),
        rows=[{"order_no": "A", "id": 1}],
    )
    assert _run_identity(deps) == identity.EXIT_FAIL
    assert counter[0] == 0


def test_fingerprint_mismatch_blocks_the_business_query() -> None:
    deps, counter = _identity_deps(
        diagnosing=Caller(fingerprint="fp-1"),
        reading=Caller(fingerprint="fp-2"),
        rows=[{"order_no": "A", "id": 1}],
    )
    assert _run_identity(deps) == identity.EXIT_FAIL
    assert counter[0] == 0


# ---------------------------------------------------------------- independent_cpms


class CpmsDeps:
    def __init__(
        self,
        *,
        caller=None,
        rows=None,
        by_order=None,
        read=None,
        endpoint_error: bool = False,
        endpoint: str = "https://cpms.example/charging-pile",
    ) -> None:
        self.caller = caller if caller is not None else Caller()
        self.rows = rows if rows is not None else [{"order_no": "A", "id": 2, "created_time": "t2"}]
        self.by_order = by_order or {}
        self._read = read
        self._endpoint = endpoint
        self._endpoint_error = endpoint_error
        self.cpms_calls = 0

    def resolve_caller(self):
        return self.caller

    def list_orders(self, _caller, *, page_size: int):
        assert page_size >= 1
        return self.rows

    def get_orders(self, _caller, order_no: str):
        return self.by_order.get(order_no, [])

    def read_status(self, endpoint, order_no, *, tenant_id, user_id):
        if self._read is not None:
            return self._read(endpoint, order_no, tenant_id=tenant_id, user_id=user_id)
        return {
            "order_no": order_no,
            "status_code": "1",
            "status_role": "finished",
            "observed_at": "2026-10-23T00:00:00+00:00",
            "source": "cpms.chargingInfo",
        }

    def endpoint(self, tenant_id: str) -> str:
        if self._endpoint_error:
            raise ValueError("TENANT_ENDPOINT_UNRESOLVED")
        return self._endpoint


def _run_cpms(deps, argv: list[str] | None = None) -> int:
    return cpms.main(argv or [], deps=deps)


def test_cpms_happy_path_exits_zero() -> None:
    deps = CpmsDeps(
        rows=[
            {"order_no": "A", "id": 2, "created_time": "2026-10-23 10:00:00"},
            {"order_no": "Z", "id": 1, "created_time": "2026-10-23 09:00:00"},
        ]
    )
    assert _run_cpms(deps) == cpms.EXIT_PASS


def test_cpms_failure_is_unverified_not_pass() -> None:
    """Codex 把这一分支改成 EXIT_PASS 后旧测试仍全绿 —— 这里必须抓住。"""

    def boom(*_a, **_k):
        raise RuntimeError("upstream down")

    deps = CpmsDeps(
        rows=[
            {"order_no": "A", "id": 2, "created_time": "t2"},
            {"order_no": "Z", "id": 1, "created_time": "t1"},
        ],
        read=boom,
    )
    code = _run_cpms(deps)
    assert code == cpms.EXIT_UNVERIFIED
    assert code != cpms.EXIT_PASS
    assert deps.cpms_calls == 1


def test_cpms_endpoint_unregistered_is_unverified_and_calls_no_cpms() -> None:
    deps = CpmsDeps(
        rows=[
            {"order_no": "A", "id": 2, "created_time": "t2"},
            {"order_no": "Z", "id": 1, "created_time": "t1"},
        ],
        endpoint_error=True,
    )
    assert _run_cpms(deps) == cpms.EXIT_UNVERIFIED
    assert deps.cpms_calls == 0, "端点未解析却调用了 CPMS"


def test_cpms_inconsistent_result_fails(capsys: pytest.CaptureFixture[str]) -> None:
    deps = CpmsDeps(
        rows=[
            {"order_no": "A", "id": 2, "created_time": "t2"},
            {"order_no": "Z", "id": 1, "created_time": "t1"},
        ]
    )
    code = _run_cpms(deps, ["--expect-source", "other.source", "--expect-status-code", "1"])
    assert code == cpms.EXIT_FAIL
    out = json.loads(capsys.readouterr().out)
    assert out["comparison"]["state"] == "compared"
    assert out["comparison"]["consistent"] is False


def test_fixed_expectation_is_not_recorded_as_consistency(capsys: pytest.CaptureFixture[str]):
    """读取成功 ≠ 一致性通过。不给 `--expect-*` 时必须是 not_compared。"""
    deps = CpmsDeps(
        rows=[
            {"order_no": "A", "id": 2, "created_time": "t2"},
            {"order_no": "Z", "id": 1, "created_time": "t1"},
        ]
    )
    assert _run_cpms(deps) == cpms.EXIT_PASS
    out = json.loads(capsys.readouterr().out)
    assert out["comparison"]["state"] == "not_compared"
    assert "consistent" not in out["comparison"]


# ---------------------------------------------------------------- 排序判据（真实 id）


def test_same_second_higher_id_first_is_correct() -> None:
    """同一秒：id=2/order_no=A 在 id=1/order_no=Z 前面是**正确**排序。

    旧判据拿 order_no 当第二键，按字符串比较会把它判反。这是 Codex 给的反例。
    """
    rows = [
        {"id": 2, "order_no": "A", "created_time": "2026-10-23 10:00:00"},
        {"id": 1, "order_no": "Z", "created_time": "2026-10-23 10:00:00"},
    ]
    assert cpms.sort_verdict(rows)["verdict"] == "pass"


def test_same_second_lower_id_first_is_rejected() -> None:
    rows = [
        {"id": 1, "order_no": "Z", "created_time": "2026-10-23 10:00:00"},
        {"id": 2, "order_no": "A", "created_time": "2026-10-23 10:00:00"},
    ]
    assert cpms.sort_verdict(rows)["verdict"] == "fail"


@pytest.mark.parametrize("rows", [[], [{"id": 1, "order_no": "A", "created_time": "t"}]])
def test_insufficient_rows_is_unverified_not_pass(rows: list[dict]) -> None:
    """空集合与单行都**不能**声称多行排序已验证。"""
    assert cpms.sort_verdict(rows)["verdict"] == "unverified"


def test_missing_id_is_unverified_not_pass() -> None:
    rows = [
        {"order_no": "A", "created_time": "t2"},
        {"order_no": "Z", "created_time": "t1"},
    ]
    assert cpms.sort_verdict(rows)["verdict"] == "unverified"


def test_insufficient_rows_exits_unverified(capsys: pytest.CaptureFixture[str]) -> None:
    deps = CpmsDeps(rows=[{"order_no": "A", "id": 2, "created_time": "t2"}])
    assert _run_cpms(deps) == cpms.EXIT_UNVERIFIED


# ---------------------------------------------------------------- 指定单不在第一页


def test_order_beyond_first_page_is_checked_by_order_no() -> None:
    """指定单不在第一页 ≠ 不属于本人：要按号核对归属。"""
    deps = CpmsDeps(
        rows=[
            {"order_no": "A", "id": 3, "created_time": "t3"},
            {"order_no": "B", "id": 2, "created_time": "t2"},
        ],
        by_order={"OLD": [{"order_no": "OLD", "id": 1, "created_time": "t1"}]},
    )
    assert _run_cpms(deps, ["--expect-order", "OLD"]) == cpms.EXIT_PASS


def test_order_not_owned_at_all_is_rejected() -> None:
    deps = CpmsDeps(
        rows=[
            {"order_no": "A", "id": 3, "created_time": "t3"},
            {"order_no": "B", "id": 2, "created_time": "t2"},
        ],
    )
    assert _run_cpms(deps, ["--expect-order", "SOMEONE-ELSE"]) == cpms.EXIT_FAIL
    assert deps.cpms_calls == 0


# ---------------------------------------------------------------- 页面判据

ORDER = "SYNTH-ORDER-0001"
ANSWER = (
    f"Order {ORDER}: finished (status code 1).\n"
    "Source: cpms.chargingInfo; observed at (UTC): 2026-10-23T03:21:45.688717+00:00."
)
QA = "qa_" + "a" * 32


def _post(qa_id: str = QA, status: str = "completed") -> dict:
    return {
        "method": "POST",
        "path": probe.POST_PATH,
        "http": 202,
        "content_type": "application/json",
        "cache_control": "no-store",
        "body": {"type": "qa", "status": status, "qa_id": qa_id},
    }


def _get(qa_id: str, answer: str = ANSWER, status: str = "completed") -> dict:
    return {
        "method": "GET",
        "path": f"{probe.POST_PATH}/{qa_id}",
        "http": 200,
        "content_type": "application/json",
        "cache_control": "no-store",
        "body": {
            "type": "qa",
            "status": status,
            "qa_id": qa_id,
            "result": {"blocks": [{"kind": "text", "text": answer}]},
        },
    }


def _judge(events, payloads, *, region=ANSWER, label="latest", order_no=ORDER):
    return probe.judge(
        events=events,
        payloads=payloads,
        answer_text=probe.answer_from_events(events, qa_id_of(events)),
        order_no=order_no,
        label=label,
        assistant_region_text=region,
    )


def qa_id_of(events) -> str | None:
    for e in events:
        if e["method"] == "POST" and e["path"] == probe.POST_PATH:
            return (e.get("body") or {}).get("qa_id")
    return None


def test_page_happy_path_passes() -> None:
    checks = _judge(
        [_post(), _get(QA)],
        [{"body": {"question": "check my latest order status"}}],
    )
    assert all(checks.values()), [k for k, v in checks.items() if not v]


def test_missing_request_body_fails() -> None:
    """没捕获到提交请求体时不能通过。"""
    checks = _judge([_post(), _get(QA)], [])
    assert checks["request_captured"] is False


def test_get_body_belongs_to_another_qa_fails() -> None:
    """路径匹配本次 qa_id，但响应体里的 qa_id 是另一条。"""
    other = "qa_" + "b" * 32
    rogue = _get(other)
    rogue["path"] = f"{probe.POST_PATH}/{QA}"
    checks = _judge([_post(), rogue], [{"body": {"question": "check my latest order status"}}])
    assert checks["get_matches_own_qa_id"] is False


def test_failed_status_with_stale_blocks_fails() -> None:
    """body.status=failed 但仍带旧 blocks —— 不能当成成功。"""
    checks = _judge(
        [_post(), _get(QA, status="failed")],
        [{"body": {"question": "check my latest order status"}}],
    )
    assert checks["get_completed"] is False


def test_answer_without_status_fails() -> None:
    """回答只有订单号、来源和时间，没有状态 —— 不完整。"""
    partial = (
        f"Order {ORDER}.\nSource: cpms.chargingInfo; observed at (UTC): 2026-10-23T03:21:45.688717+00:00."
    )
    checks = _judge(
        [_post(), _get(QA, answer=partial)],
        [{"body": {"question": "check my latest order status"}}],
        region=partial,
    )
    assert checks["answer_has_status"] is False


def test_user_question_cannot_impersonate_the_answer() -> None:
    qa = "qa_" + "a" * 32
    checks = _judge(
        [_post(qa), _get(qa)],
        [{"body": {"question": f"check order {ORDER} status"}}],
        label="specified",
        region=f"check order {ORDER} status",
    )
    assert checks["assistant_region_has_answer"] is False


def test_repeated_polling_does_not_concatenate_duplicates() -> None:
    """同一个 QA 被轮询多次时，答案只取最后一条匹配 GET 的 blocks，不拼接。"""
    events = [_post(), _get(QA, status="running"), _get(QA, status="running"), _get(QA)]
    answer = probe.answer_from_events(events, QA)
    assert answer.count(ORDER) == 1
    checks = _judge(events, [{"body": {"question": "check my latest order status"}}])
    assert all(checks.values()), [k for k, v in checks.items() if not v]


def test_region_inside_user_bubble_fails() -> None:
    """取到的区域落在 `.user-bubble` 里 ⇒ 那是用户提问，不是回答。"""
    checks = probe.judge(
        events=[_post(), _get(QA)],
        payloads=[{"body": {"question": "check my latest order status"}}],
        answer_text=probe.answer_from_events([_post(), _get(QA)], QA),
        order_no=ORDER,
        label="latest",
        assistant_region_text=ANSWER,
        region_in_user_bubble=True,
    )
    assert checks["region_is_not_user_bubble"] is False


def test_empty_region_is_missing_not_fallback() -> None:
    """选择器没命中共时不得退回整页：空区域判失败。"""
    checks = _judge([_post(), _get(QA)], [{"body": {"question": "check my latest order status"}}], region="")
    assert checks["assistant_region_has_answer"] is False
    assert checks["answer_matches_qa_blocks"] is False


def test_selector_constants_match_the_live_bundle_structure() -> None:
    """选择器必须对着**现役产物**的类名，而不是猜的。

    41 上 `aiPackage-pages-chat-chat.d86b6f8a.js` 的分支是
    `role==="user" ? .user-bubble : .ai-row > .ai-msg`。这里钉住常量本身，
    避免有人再改回 `.msg-list .msg-item` 这种产物里不存在的类名。
    """
    assert any(".ai-msg" in s for s in probe.ASSISTANT_SELECTORS)
    assert all(".user-bubble" in s for s in probe.USER_MESSAGE_SELECTORS)
    for legacy in (".msg-list", ".chat-list", ".message-list"):
        assert all(legacy not in s for s in probe.ASSISTANT_SELECTORS), legacy


def test_replay_selector_script_exists_and_is_explicit_about_limits() -> None:
    """回放脚本要留在仓库里（方法可复跑），且写明它不碰生产。"""
    script = TOOLS / "replay_selector.py"
    assert script.exists()
    text = script.read_text("utf-8")
    assert "不碰生产" in text or "不发任何网络请求到生产" in text
    assert "SYNTH-ORDER-0001" in text


# ---------------------------------------------------------------- 默认输出脱敏

#: 合成敏感标记：出现在终端输出里就是泄露。
SECRET_ORDER = "SYNTH-SECRET-ORDER-9Z"
SECRET_ANSWER = "SYNTH-SECRET-ANSWER-QX"


class _RedactDeps:
    """CPMS 替身：返回带合成敏感标记的完整订单号。"""

    def __init__(self) -> None:
        self.cpms_calls = 0

    def resolve_caller(self):
        return Caller()

    def list_orders(self, _caller, *, page_size: int):
        return [
            {"order_no": SECRET_ORDER, "id": 2, "created_time": "t2"},
            {"order_no": "SYNTH-OTHER", "id": 1, "created_time": "t1"},
        ]

    def get_orders(self, _caller, order_no: str):
        return []

    def read_status(self, endpoint, order_no, *, tenant_id, user_id):
        return {
            "order_no": order_no,
            "status_code": "1",
            "status_role": "finished",
            "observed_at": "2026-10-23T00:00:00+00:00",
            "source": "cpms.chargingInfo",
        }

    def endpoint(self, tenant_id: str) -> str:
        return "https://cpms.example/charging-pile"


def test_terminal_output_has_no_full_order_number(capsys: pytest.CaptureFixture[str]) -> None:
    """终端只打摘要：完整订单号只进私有报告。"""
    deps = _RedactDeps()
    cpms.main(["--expect-order", SECRET_ORDER], deps=deps)
    captured = capsys.readouterr()
    wrote = captured.out + captured.err
    assert SECRET_ORDER not in wrote, "完整订单号出现在终端输出里"
    assert SECRET_ORDER[:6] in wrote, "连脱敏前缀都没有，说明摘要没打出来"


def test_private_report_keeps_the_full_evidence(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    """私有报告保留完整证据，且权限是 0600。"""
    out = tmp_path / "report.json"
    cpms.main(["--expect-order", SECRET_ORDER, "--out", str(out)], deps=_RedactDeps())
    capsys.readouterr()
    assert out.exists()
    assert oct(out.stat().st_mode)[-3:] == "600"
    body = json.loads(out.read_text())
    assert body["expected_fixed"]["order_no"] == SECRET_ORDER


@pytest.mark.parametrize("initial", ["queued", "running", "failed"])
def test_node_page_admission_still_requires_matching_completed_get(initial):
    events = [_post(status=initial), _get(QA)]
    values = dict(
        events=events,
        payloads=[{"body": {"question": "check my latest order status"}}],
        answer_text=ANSWER,
        order_no=ORDER,
        label="latest",
        assistant_region_text=ANSWER,
        execution="node",
    )
    checks = probe.judge(**values)
    assert all(checks.values()) == (initial != "failed")
    values["events"] = [_post(status=initial), _get(QA, status="failed")]
    assert not all(probe.judge(**values).values())
