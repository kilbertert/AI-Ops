from datetime import datetime, timedelta, timezone

import pytest

from aiops_diagnostics.rules import (
    ORDER_WINDOW_PADDING_MINUTES,
    classify_stop_reason,
    is_server_billing,
    order_window,
    status_label,
)


def test_order_status_uses_backend_meaning() -> None:
    assert status_label(2) == "不可控异常"
    assert status_label(5) == "可控异常结束"


def test_ykc_temperature_stop_is_abnormal() -> None:
    reason = classify_stop_reason("YKC1.8", "116", "")
    assert reason.classification == "over_temperature"
    assert reason.abnormal is True
    assert "温度" in reason.description


def test_ykc_start_failure_range_takes_precedence_over_temperature_keyword() -> None:
    reason = classify_stop_reason("YKC1.8", "83", "")

    assert reason.classification == "start_failure"
    assert reason.abnormal is True
    assert "启动失败" in reason.description


def test_ykc_stop_reason_boundaries_match_backend_ranges() -> None:
    assert classify_stop_reason("YKC1.8", "73", "").classification == "normal_stop"
    assert classify_stop_reason("YKC1.8", "74", "").classification == "start_failure"
    assert classify_stop_reason("YKC1.8", "102", "").classification == "start_failure"
    assert classify_stop_reason("YKC1.8", "116", "").classification == "over_temperature"


def test_server_billing_protocols() -> None:
    assert is_server_billing("OCPP1.6-J")
    assert is_server_billing("AYK")
    assert not is_server_billing("YKC1.8")


def test_order_window_pads_both_edges_so_evidence_at_the_boundaries_is_read() -> None:
    window = order_window("2026-07-31T10:00:00+08:00", "2026-07-31T10:30:00+08:00", 72)

    assert window is not None
    start, end, clamped = window
    padding = timedelta(minutes=ORDER_WINDOW_PADDING_MINUTES)
    assert start == datetime(2026, 7, 31, 10, 0, tzinfo=timezone(timedelta(hours=8))) - padding
    assert end == datetime(2026, 7, 31, 10, 30, tzinfo=timezone(timedelta(hours=8))) + padding
    assert clamped is False


def test_order_window_clamps_an_overlong_order_instead_of_reading_it_unbounded() -> None:
    """The bounded-read rule: an order spanning 100h is read for `max_hours` plus padding."""
    window = order_window("2026-07-31T10:00:00+08:00", "2026-08-04T14:00:00+08:00", 72)

    assert window is not None
    start, end, clamped = window
    assert clamped is True
    assert end == start + timedelta(hours=72)
    assert end < datetime(2026, 8, 4, 14, tzinfo=timezone(timedelta(hours=8)))


def test_order_window_clamping_boundary_sits_at_max_hours_minus_both_paddings() -> None:
    """Pin where `clamped` flips, because that is where the read bound actually lands.

    Both edges are padded before the cap is applied, so the boundary is not
    ``created + max_hours`` but ``created + max_hours - 2 * padding``: an order
    stopping exactly there still fits, and one minute later does not. Writing the
    boundary down is the point — the padding is what moves it, and a change to
    either value silently shifts how far back every diagnosis may read.
    """
    created = datetime(2026, 7, 31, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    boundary = created + timedelta(hours=72) - timedelta(minutes=ORDER_WINDOW_PADDING_MINUTES * 2)

    at_boundary = order_window(created.isoformat(), boundary.isoformat(), 72)
    assert at_boundary is not None
    assert at_boundary[2] is False
    assert at_boundary[1] == created - timedelta(minutes=ORDER_WINDOW_PADDING_MINUTES) + timedelta(hours=72)

    one_minute_later = order_window(created.isoformat(), (boundary + timedelta(minutes=1)).isoformat(), 72)
    assert one_minute_later is not None
    assert one_minute_later[2] is True


def test_order_window_strictly_extends_past_both_edges_of_the_order() -> None:
    """The window must reach *beyond* the order, which is the only reason padding exists.

    The gun/comm evidence for an order starts just before `created_time` and ends
    just after `stop_time`, so a window that merely equals the order's span reads
    the evidence that overlaps the boundaries incompletely. Asserted as a strict
    inequality rather than against the constant: the padding's value is a
    calibration knob, but it must never be reduced to nothing.
    """
    created = datetime(2026, 7, 31, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    stopped = datetime(2026, 7, 31, 10, 30, tzinfo=timezone(timedelta(hours=8)))

    window = order_window(created.isoformat(), stopped.isoformat(), 72)
    assert window is not None
    start, end, _ = window
    assert start < created
    assert end > stopped


def test_order_window_without_created_time_refuses_to_invent_one() -> None:
    """No usable start means no window: the caller must not query unbounded."""
    assert order_window(None, "2026-07-31T10:30:00+08:00", 72) is None
    assert order_window("", "2026-07-31T10:30:00+08:00", 72) is None
    assert order_window("not-a-timestamp", "2026-07-31T10:30:00+08:00", 72) is None


def test_order_window_without_stop_time_ends_at_now_not_at_infinity() -> None:
    """An order still charging has no `stop_time`; the window must still be bounded."""
    window = order_window("2026-07-31T10:00:00+08:00", None, 72)

    assert window is not None
    start, end, _ = window
    assert end is not None
    assert end > start
    assert end <= datetime.now(tz=start.tzinfo) + timedelta(minutes=ORDER_WINDOW_PADDING_MINUTES)


@pytest.mark.parametrize(
    ("created_time", "stop_time", "expected_tz"),
    [
        ("2026-07-31 10:00:00", "2026-07-31T10:30:00+08:00", timezone(timedelta(hours=8))),
        ("2026-07-31T10:00:00+08:00", "2026-07-31 10:30:00", timezone(timedelta(hours=8))),
    ],
)
def test_order_window_reconciles_mixed_timezone_columns(
    created_time: str, stop_time: str, expected_tz: timezone
) -> None:
    """The two columns come from different writers with different tzinfo completeness.

    A naive side adopts the other's zone rather than being treated as UTC, so a
    mixed pair yields the real 30-minute span instead of a shifted one.
    """
    window = order_window(created_time, stop_time, 72)

    assert window is not None
    start, end, clamped = window
    assert clamped is False
    assert start.tzinfo == expected_tz
    assert end.tzinfo == expected_tz
    assert end - start == timedelta(minutes=30 + ORDER_WINDOW_PADDING_MINUTES * 2)


def test_order_window_converts_aware_columns_to_one_zone() -> None:
    """Both aware but in different zones: the span is real, not the literal difference."""
    window = order_window("2026-07-31T02:00:00+00:00", "2026-07-31T10:30:00+08:00", 72)

    assert window is not None
    start, end, clamped = window
    assert clamped is False
    assert end - start == timedelta(minutes=30 + ORDER_WINDOW_PADDING_MINUTES * 2)


def test_both_diagnosis_paths_agree_on_what_the_shared_window_delegates_to() -> None:
    """The two callers of the window rule must agree, and now they are provably the same call.

    The deterministic engine and the agent tool layer query the same TDengine
    tables for the same order, so a different window on either path would report a
    different slice of the same evidence. Before this rule was shared, that
    agreement rested on both modules importing one private helper -- a coupling
    with no contract and no test. The tool layer now calls `rules.order_window`
    directly, and this guard pins the remaining adapter in `engine` to it, so a
    divergence shows up here rather than as two different reports for one order.

    A *clamping* order is used deliberately: it is the only input that exercises
    the `max_hours` the adapter passes through, so an adapter that forwarded a
    different cap would pass on a short order and fail here.
    """
    from aiops_diagnostics.engine import _order_window

    order = {"created_time": "2026-07-31T10:00:00+08:00", "stop_time": "2026-08-04T14:00:00+08:00"}
    expected = order_window(order["created_time"], order["stop_time"], 72)

    assert expected is not None and expected[2] is True  # the clamp is what makes this meaningful
    assert _order_window(order, 72) == expected
