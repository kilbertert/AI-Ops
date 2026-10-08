"""The health report's copy follows the language its job was created in (#539).

This surface had NO language handling at all: summary, curve names and every
stop-reason string were Chinese literals, so every reader — consumer included —
got Chinese regardless of `Accept-Language`.

The split asserted here is the one the whole workstream keeps re-learning:
what WE author follows the language; what the UPSTREAM reported is passed
through verbatim.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aiops_diagnostics.config import SafetySettings
from aiops_diagnostics.health_curves import build_curves
from aiops_diagnostics.health_report import build_minimal_health_report
from aiops_diagnostics.health_report_copy import (
    HEALTH_CURVE_NAMES,
    HEALTH_STOP_FALLBACK_MESSAGES,
    HEALTH_SUMMARY_MESSAGES,
    YKC_STOP_REASON_MESSAGES,
)
from aiops_diagnostics.i18n import SUPPORTED_LANGUAGES
from aiops_diagnostics.rules import classify_stop_reason

_LANGS = SUPPORTED_LANGUAGES


def test_every_copy_table_covers_every_supported_language() -> None:
    """Coverage, structurally — a short table would silently fall back to zh.

    Registered here rather than added to `_USER_FACING_MESSAGE_TABLES` because
    these tables live outside `i18n`; the property checked is the same one.
    """
    for name, table in (
        ("HEALTH_SUMMARY_MESSAGES", HEALTH_SUMMARY_MESSAGES),
        ("HEALTH_CURVE_NAMES", HEALTH_CURVE_NAMES),
        ("HEALTH_STOP_FALLBACK_MESSAGES", HEALTH_STOP_FALLBACK_MESSAGES),
        ("YKC_STOP_REASON_MESSAGES", YKC_STOP_REASON_MESSAGES),
    ):
        missing = [lang for lang in _LANGS if lang not in table]
        assert not missing, f"{name} 缺语言：{missing}"
        keys = set(table["zh"])
        for lang in _LANGS:
            assert set(table[lang]) == keys, f"{name}[{lang}] 键集合与 zh 不一致"
            assert all(value.strip() for value in table[lang].values()), f"{name}[{lang}] 有空值"


@pytest.mark.parametrize("language", ["en", "de", "fr", "es", "pt"])
def test_the_assembled_report_has_no_chinese_for_other_languages(language: str) -> None:
    """Asserted on the ASSEMBLED REPORT, not on the lookup helpers.

    A test that calls `health_summary()` directly cannot see the wiring go
    back to a hardcoded literal — which is exactly the mutation it must catch,
    and the mistake this workstream has now made four times (#535, #536, #538,
    and here). So this goes through the real builder.
    """
    from aiops_diagnostics.i18n import chinese_leak

    report = build_minimal_health_report(_Sources([_order()]), "O-1", SafetySettings(), language)
    assert chinese_leak(report["summary"]) == "", report["summary"]
    # EVERY string we author, `stop_reason.value` included. This used to skip
    # `stop_reason` on the grounds that it "may carry the upstream's own text" —
    # and that carve-out is exactly how 66.3%-of-orders' worth of Chinese
    # reached non-Chinese reports (#566 found it, #599 fixed it). The upstream's
    # text now lives in `reported_value`, which is the field that is allowed to
    # be any language; `value` is ours and is judged like every other.
    for indicator in report["indicators"]:
        assert chinese_leak(str(indicator.get("value") or "")) == "", indicator


def test_our_stop_reason_words_follow_the_language() -> None:
    """YKC code 78: the upstream sends the NUMBER, we supply the words."""
    zh = classify_stop_reason("YKC", 78, None).description
    en = classify_stop_reason("YKC", 78, None, language="en").description
    assert zh == "启动失败：余额不足"
    assert en == "Start-up failed: insufficient balance"


def test_the_upstream_sentence_never_becomes_the_localized_description() -> None:
    """`content` is DATA describing what the source reported — not our copy.

    The PRINCIPLE this test was written for still holds and is unchanged:
    translating the upstream's sentence would rewrite what the order actually
    recorded, which is the line this project refuses to cross.

    What changed (#599) is where the principle is APPLIED. The old assertion was
    `result.description == upstream`, i.e. the upstream's sentence WAS the
    reader-facing description — and since `value` was built from that
    description, an English report carried Chinese for 59.6% of real orders.
    Refusing to translate it was right; putting it where the contract says a
    client renders localized copy was not.

    The sentence is not dropped: the report carries it in `reported_value`.
    This test now pins the half that belongs here — `description` is OUR words,
    in the requested language — and `test_the_report_keeps_the_upstream_words_
    beside_the_localized_value` pins the other half.
    """
    from aiops_diagnostics.i18n import chinese_leak

    upstream = "余额耗尽停止订单"
    result = classify_stop_reason("", "-1", upstream, language="en")
    assert result.description != upstream, "上游原句不得成为面向读者的本地化文案"
    assert chinese_leak(result.description) == "", result.description


def test_curve_series_names_are_localized_end_to_end() -> None:
    """Through the real builder, not the lookup: the wiring is what broke before."""
    samples = [
        {"_ts": 1, "power": 1.0, "outputVoltage": 2.0, "temperature": 3.0},
        {"_ts": 2, "power": 2.0, "outputVoltage": 3.0, "temperature": 4.0},
    ]
    names_zh = {series["name"] for group in build_curves(samples).values() for series in group["series"]}
    names_de = {
        series["name"] for group in build_curves(samples, "de").values() for series in group["series"]
    }
    assert "实际功率" in names_zh
    assert "Ist-Leistung" in names_de
    assert not names_zh & names_de, "曲线名没有随语言变化"


class _Sources:
    def __init__(self, orders: list[dict]) -> None:
        self.orders = orders

    def get_orders(self, order_no: str, tenant_id: str | None = None) -> list[dict]:
        return self.orders


def _order(**overrides):
    start = datetime(2026, 9, 1, 1, tzinfo=UTC)
    order = {
        "order_no": "O-1",
        "status": 1,
        "device_code": "D-1",
        "created_time": start,
        "stop_time": start + timedelta(hours=1),
        "device_protocol": "OCPP",
        "stopped_reason_code": "Local",
        "stopped_reason_content": "用户主动停止",
    }
    order.update(overrides)
    return order


def test_reuse_is_per_language_not_across_languages(tmp_path) -> None:
    """A job created in one language must not be handed to another.

    The reuse predicate originally keyed on (scope, order, rule_version) only.
    The report's prose is generated ONCE and stored, so an English request that
    reused a completed Chinese job received a Chinese report while its response
    reported the row's language — a correct-looking 200 whose content was in the
    wrong language, which is the exact defect shape this workstream exists to
    remove. Caught in review.
    """
    from aiops_diagnostics.gateway_store import GatewayStore

    store = GatewayStore(tmp_path / "gateway.db")
    zh_job, zh_created = store.create_or_reuse_health_job("scope-1", "O-1", "health-v2", language="zh")
    en_job, en_created = store.create_or_reuse_health_job("scope-1", "O-1", "health-v2", language="en")

    assert zh_created is True
    assert en_created is True, "英文请求复用了中文作业 —— 会把中文报告交给英文请求者"
    assert zh_job["job_id"] != en_job["job_id"]
    assert zh_job["language"] == "zh"
    assert en_job["language"] == "en"

    # And same-language reuse still works — the fix narrows the predicate, it
    # does not disable reuse.
    again, created = store.create_or_reuse_health_job("scope-1", "O-1", "health-v2", language="en")
    assert created is False
    assert again["job_id"] == en_job["job_id"]


def test_the_declared_script_survives_storage(tmp_path) -> None:
    """`zh-Hant` is STORED as declared (#531) — the second drop point.

    The resolver was only half the bug: even once it preserved the script, this
    entry point truncated the tag before the row was written, so a Traditional
    request got a `zh` row back and its own answer was reported as Simplified.
    "Resolves but does not persist" is the failure mode that makes fixing one
    of the two points look like a fix.

    Asserted through the same store method the other language tests use, and on
    the value READ BACK rather than the value passed in — a normaliser that
    returns the right tag while the row keeps the truncated one would pass the
    weaker form.
    """
    from aiops_diagnostics.gateway_store import GatewayStore

    store = GatewayStore(tmp_path / "gateway.db")
    job, created = store.create_or_reuse_health_job("scope-1", "O-1", "health-v2", language="zh-Hant")
    assert created is True
    assert job["language"] == "zh-Hant"

    read_back, _ = store.create_or_reuse_health_job("scope-1", "O-1", "health-v2", language="zh-Hant")
    assert read_back["language"] == "zh-Hant", "存进去的繁体标签被截断成了简体"

    # Traditional and Simplified are DIFFERENT languages for reuse purposes:
    # treating them as one would hand a Traditional reader a Simplified report.
    zh_job, zh_created = store.create_or_reuse_health_job("scope-1", "O-1", "health-v2", language="zh")
    assert zh_created is True, "繁体作业被复用给了简体请求"
    assert zh_job["job_id"] != job["job_id"]

    # NOT asserted here: a raw `zh-Hant-TW`. The store takes an ALREADY RESOLVED
    # tag — its validator accepts at most one subtag, and the resolver folds the
    # region away before this point, so the three-subtag form never arrives from
    # the HTTP path. Widening the validator for an input nothing produces would
    # be speculative; the resolver's handling of it is asserted in
    # `tests/test_i18n.py`.


@pytest.mark.parametrize("language", ["en", "de", "fr", "es", "pt"])
def test_an_unlisted_ykc_code_does_not_emit_chinese(language: str) -> None:
    """An unlisted code's fallback follows the language too.

    It was briefly an English literal, which put English into a Chinese report —
    a regression this test pins in the other direction.
    """
    from aiops_diagnostics.i18n import chinese_leak

    text = classify_stop_reason("YKC", 999, None, language=language).description
    assert chinese_leak(text) == "", text
    assert "999" in text


def test_an_unlisted_ykc_code_stays_chinese_for_zh() -> None:
    text = classify_stop_reason("YKC", 999, None).description
    assert text == "YKC 停止码 999"


def test_a_predating_database_migrates_and_can_be_opened(tmp_path) -> None:
    """The store must open a database that predates the `language` column.

    This is the one failure a fresh database cannot reproduce, so it needs its
    own check: the reuse index names `language`, and the CREATE TABLE script
    that runs FIRST does not define it. Created there, the index was built
    before the ALTER added the column, and opening the store — on the gateway's
    own boot path — raised `no such column: language`. Every test that builds a
    fresh store passed the whole time. Caught before merge by asking what an
    EXISTING database would do.
    """
    import sqlite3

    from aiops_diagnostics.gateway_store import GatewayStore

    database = tmp_path / "predating.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE health_report_jobs (
            job_id TEXT PRIMARY KEY,
            scope_fingerprint TEXT NOT NULL,
            order_no TEXT NOT NULL,
            rule_version TEXT NOT NULL,
            status TEXT NOT NULL,
            report_json TEXT,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deadline_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            expires_at TEXT
        );
        """
    )
    connection.commit()
    connection.close()

    store = GatewayStore(database)  # migration runs here
    job, created = store.create_or_reuse_health_job("scope-1", "O-1", "health-v2", language="de")
    assert created is True
    assert job["language"] == "de"

    # And the reuse index really exists on the migrated database.
    names = {
        str(row[0])
        for row in sqlite3.connect(database).execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    assert "idx_health_jobs_reuse_language" in names


def test_the_report_keeps_the_upstream_words_beside_the_localized_value() -> None:
    """Both halves ship, in separate fields, with different guarantees (#599).

    Measured on 41 across all 30,955 orders: 66.3% of the upstream sentences are
    Chinese, and the classifications that echoed one into the reader-facing
    value covered 59.6% of orders. So this is not a corner case — it is the
    common path for a non-Chinese report.

    What must hold:
    * `value` is OUR localized text and carries no Chinese;
    * `reported_value` is the upstream sentence, byte-for-byte, or null;
    * `reported_language` is null — the source does not declare one today, and
      guessing would be a claim we cannot support.
    """
    from aiops_diagnostics.i18n import chinese_leak

    order = _order(stopped_reason_content="拔出断电", stopped_reason_code=-7)
    report = build_minimal_health_report(_Sources([order]), "O-1", SafetySettings(), "en")
    indicator = next(i for i in report["indicators"] if i["code"] == "stop_reason")

    assert indicator["reported_value"] == "拔出断电", "上游原句必须逐字保留"
    assert indicator["reported_language"] is None
    assert indicator["value"] != "拔出断电"
    assert chinese_leak(indicator["value"]) == "", indicator["value"]

    # A Chinese report is unaffected: the upstream words are still Chinese and
    # still the truth, only now they are in the field that says so.
    zh = build_minimal_health_report(_Sources([order]), "O-1", SafetySettings(), "zh")
    zh_indicator = next(i for i in zh["indicators"] if i["code"] == "stop_reason")
    assert zh_indicator["reported_value"] == "拔出断电"


def test_no_upstream_content_means_no_reported_value() -> None:
    """`reported_value` is null, not an empty string, when there is nothing."""
    order = _order(stopped_reason_content="", stopped_reason_code="-1")
    report = build_minimal_health_report(_Sources([order]), "O-1", SafetySettings(), "en")
    indicator = next(i for i in report["indicators"] if i["code"] == "stop_reason")
    assert indicator["reported_value"] is None
