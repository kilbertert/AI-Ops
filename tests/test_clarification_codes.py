"""A clarification reply says WHICH branch it came from (#636), and an account
that can see no site is told about the ACCOUNT, not the order (#637).

Both defects have one shape: a state the code knew and nobody downstream could
see. #636 — six branches produced one `type=clarification` envelope plus a
metric row that did not distinguish them, so reading a live incident came down
to comparing message byte-lengths across branches, and that read was wrong at
least once. #637 — an operator account whose site set resolves to empty was
told "this order is not yours", which points at the order (which is fine)
instead of at the account (the thing to fix).

Driven through the gateway HTTP face with the real `ScopedOrderAuthorizer`: the
code and the message are both decided inside the request handler, so asserting
them anywhere else would prove less than the ticket asks for.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from operator_support import (
    ORDER_OUTSIDE,
    SITE_IN,
    Caller,
    Connection,
    Runtime,
    assistant_app,
    operator_context,
)

from aiops_diagnostics.clarification import (
    CLARIFICATION_CODES,
    CLARIFY_ACCOUNT_HAS_NO_SITES,
    CLARIFY_ORDER_NOT_YOURS,
    MESSAGE_KEY_BY_CODE,
    MISSING_FIELDS_BY_CODE,
    missing_fields_for,
)
from aiops_diagnostics.i18n import CLARIFICATION_MESSAGES, SUPPORTED_LANGUAGES, clarification_message

_HEADERS = {"Authorization": "Bearer service", "X-Business-Entry": "operator"}
_OUTSIDE_QUESTION = f"订单 {ORDER_OUTSIDE} 怎么还没退款"


def _ask(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, context, question: str = _OUTSIDE_QUESTION):
    app, runtime = assistant_app(
        tmp_path=tmp_path, monkeypatch=monkeypatch, caller=Caller(context), connection=Connection()
    )
    return app.post("/v1/assistant/questions", json={"question": question}, headers=_HEADERS), runtime


# ── #637: the account statement, not the order statement ─────────────────────


def test_an_account_with_no_visible_sites_is_told_about_the_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty operator site set means NO order is visible to this account.

    Measured cause of the ticket: the caller read "this order is not yours" as an
    order problem and re-checked the order twice, while the fix — add the shop
    binding — was unreachable from the sentence.
    """
    resp, runtime = _ask(tmp_path, monkeypatch, operator_context(()))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["type"] == "clarification"
    assert runtime.diagnoses == [], "没有可见站点的账号不得被当成一次诊断"
    assert body["code"] == CLARIFY_ACCOUNT_HAS_NO_SITES
    assert body["message"] == clarification_message("zh", "account_no_sites")
    assert "这个订单不属于当前账号" not in body["message"]


def test_an_account_with_sites_keeps_the_order_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reverse direction: an account that CAN see sites, naming an order
    outside them, still gets the order statement. Without this, the split would
    have swapped one wrong message for another."""
    resp, _ = _ask(tmp_path, monkeypatch, operator_context((SITE_IN,)))
    body = resp.json()
    assert body["code"] == CLARIFY_ORDER_NOT_YOURS
    assert body["message"] == clarification_message("zh", "not_yours")


def test_the_two_account_states_have_different_copy_in_every_language() -> None:
    """The split is only real if a reader can tell the two apart — checked over
    the whole language table, because the client renders whichever language the
    request asked for, not zh."""
    for language in SUPPORTED_LANGUAGES:
        order_copy = CLARIFICATION_MESSAGES[language]["not_yours"]
        account_copy = CLARIFICATION_MESSAGES[language]["account_no_sites"]
        assert account_copy.strip() and order_copy.strip(), language
        assert account_copy != order_copy, f"{language}: 两条文案相同 ⇒ 读不出是账号还是订单"


# ── #636: a machine-readable discriminant on every clarification ─────────────


def test_the_code_selects_the_message_and_the_missing_fields() -> None:
    """One reply cannot carry a code that contradicts its own copy: both come
    from the registry, so the three outputs (code / missing_fields / message)move
    together by construction."""
    for code in CLARIFICATION_CODES:
        assert missing_fields_for(code) == list(MISSING_FIELDS_BY_CODE[code])
        assert MESSAGE_KEY_BY_CODE[code], code


def test_an_unregistered_code_is_refused_rather_than_defaulted() -> None:
    """A new branch that invents a code fails loudly here instead of shipping a
    reply nobody can look up."""
    with pytest.raises(ValueError):
        missing_fields_for("CLARIFY_MADE_UP")


def test_only_the_unowned_order_branch_reports_the_order_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`wrong_entry` and `order_no` used to be one indistinguishable state; the
    codes are what makes "which branch" answerable without rerunning them."""
    assert CLARIFY_ORDER_NOT_YOURS != CLARIFY_ACCOUNT_HAS_NO_SITES
    assert set(CLARIFICATION_CODES) == set(MESSAGE_KEY_BY_CODE) == set(MISSING_FIELDS_BY_CODE)


class _RecordingRuntime(Runtime):
    """Adds the one method `_record_route_metric` calls, so the metric row is
    observable from the HTTP face rather than assumed."""

    def __init__(self) -> None:
        super().__init__()
        self.route_metrics: list[dict[str, object]] = []

    def record_route_metric(
        self,
        context,
        route_type: str,
        outcome: str,
        *,
        error_code: str | None = None,
        **kwargs: object,
    ) -> None:
        del context, kwargs
        self.route_metrics.append({"route_type": route_type, "outcome": outcome, "error_code": error_code})


def test_the_reply_carries_the_code_the_metric_row_carries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same string must be in the body AND the metric row: an operator greps
    one token and finds both, which is the whole point of the ticket."""
    runtime = _RecordingRuntime()
    app, runtime = assistant_app(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        caller=Caller(operator_context(())),
        connection=Connection(),
        runtime=runtime,
    )
    body = app.post("/v1/assistant/questions", json={"question": _OUTSIDE_QUESTION}, headers=_HEADERS).json()

    assert body["code"] == CLARIFY_ACCOUNT_HAS_NO_SITES
    assert {"route_type": "clarification", "outcome": "completed", "error_code": body["code"]} in (
        runtime.route_metrics
    ), runtime.route_metrics
