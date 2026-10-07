#!/usr/bin/env python3
"""Derive `zh-Hant` copy from the Simplified authority (#531).

Traditional Chinese is the SAME language in another script, so it is not a
translation: it is a deterministic conversion of the authoritative `zh` text.
Keeping it that way matters — a hand-written Traditional passage has no
source to re-derive from, so the next person to edit the Simplified copy would
have to remember to hand-edit this one too, and nothing would catch them if
they did not.

The conversion runs at DEVELOPMENT time only. The service reads `zh-Hant` out
of the data files like any other language; it never converts at runtime.

`opencc` is therefore deliberately NOT in `pyproject.toml` — neither the runtime
dependencies nor either dev group. `deploy/deploy-41.sh` refuses to deploy when
the committed `pyproject.toml` differs from the host's, because the artifact
carries `src/` only and a changed manifest means the running environment may be
missing a package. A build-time converter that the service never imports has no
business tripping that signal, and upgrading a production environment to ship
150 derived strings is the wrong trade. Install it on the machine that runs this
script:

    uv pip install --system 'opencc>=1.1,<2'   # once, on the development host

    python3 tools/derive_zh_hant.py --check   # verify the tree is current
    python3 tools/derive_zh_hant.py           # rewrite the zh-Hant entries

`--check` is the gate: it regenerates in memory and fails when the committed
copy differs, so an edited `zh` string that was not re-derived is reported here
rather than shipped as mismatched scripts. It is a DEVELOPMENT-side gate and
cannot run in CI — a clean checkout has no `opencc` and must not gain one. What
CI does hold is that every `zh-Hant` entry EXISTS (the inventory-driven coverage
checks); what it cannot hold is that the entry equals the conversion of its `zh`
authority. Run this script after editing any `zh` string.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CATALOG = ROOT / "src" / "aiops_diagnostics" / "faq_catalog.json"
RECOMMENDATIONS = ROOT / "src" / "aiops_diagnostics" / "faq_recommendations.json"
COPY_MODULES = (
    ROOT / "src" / "aiops_diagnostics" / "i18n.py",
    ROOT / "src" / "aiops_diagnostics" / "health_report_copy.py",
    ROOT / "src" / "aiops_diagnostics" / "shortcut_lifecycle.py",
)

#: `s2twp`: Simplified -> Traditional with Taiwan phrase conventions. The plain
#: `s2t` profile would produce 软件/信息-style mainland vocabulary in Traditional
#: characters; a Taiwanese reader expects 軟體/資訊. Same reasoning as choosing a
#: locale over a bare script conversion for any user-facing copy.
CONFIG = "s2twp"

SOURCE = "zh"
TARGET = "zh-Hant"


def _converter():
    try:
        from opencc import OpenCC
    except ImportError:  # pragma: no cover - dev dependency
        raise SystemExit("opencc is required: uv pip install --system 'opencc>=1.1,<2'") from None
    return OpenCC(CONFIG)


def convert(text: str, cc) -> str:
    """One string, Simplified -> Traditional.

    Idempotent by construction: Traditional input passes through unchanged, so
    re-running this over an already-derived tree is a no-op rather than a
    double conversion.
    """
    return cc.convert(text)


def derive_catalog(catalog: dict, cc) -> dict:
    """`faq_catalog.json`: every platform, every entry, both fields."""
    for entries in catalog["platforms"].values():
        for entry in entries:
            translated = dict(entry.get("i18n") or {})
            translated[TARGET] = {
                "question": convert(entry["question"], cc),
                "answer": convert(entry["answer"], cc),
            }
            entry["i18n"] = translated
    return catalog


def derive_recommendations(recommendations: dict, cc) -> dict:
    """`faq_recommendations.json`: the title of every recommendation."""
    for entries in recommendations["platforms"].values():
        for entry in entries:
            translated = dict(entry.get("i18n") or {})
            translated[TARGET] = {"title": convert(entry["title"], cc)}
            entry["i18n"] = translated
    return recommendations


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _entries(value: Any) -> dict[str, str]:
    """Flatten one language's entry to ``{field: text}``.

    A table value is either the string itself or a sub-dict of strings; both
    shapes appear in these tables, and the check does not care which.
    """
    if isinstance(value, str):
        return {"": value}
    return {key: text for key, text in value.items() if isinstance(text, str)}


def _check_language_map(mapping: dict, where: str, cc, stale: list[str]) -> None:
    """Every entry carrying `zh` must carry the `zh-Hant` DERIVATION of it.

    BOTH failure modes are reported, because both ship the same defect: a
    MISSING key falls back to `zh`, so Traditional users get Simplified; and a
    PRESENT-but-stale key serves text that no longer matches its authority.
    """
    if SOURCE not in mapping:
        return
    target_entries = _entries(mapping.get(TARGET, {})) if TARGET in mapping else {}
    for field, text in _entries(mapping[SOURCE]).items():
        expected = convert(text, cc)
        actual = target_entries.get(field)
        at = f"{where}[{TARGET!r}]" + (f"[{field!r}]" if field else "")
        if actual is None:
            stale.append(f"{at} 缺失 —— 会回退成简体")
        elif actual != expected:
            stale.append(f"{at} 与 zh 权威的派生结果不一致")


#: Flat `{tag: str | {field: str}}` tables — the message catalogs.
FLAT_TABLES = (
    ("aiops_diagnostics.i18n", "QA_FALLBACK_MESSAGES"),
    ("aiops_diagnostics.i18n", "PROMO_EMPTY_MESSAGES"),
    ("aiops_diagnostics.i18n", "PROMO_UNAVAILABLE_MESSAGES"),
    ("aiops_diagnostics.i18n", "CLARIFICATION_MESSAGES"),
    ("aiops_diagnostics.i18n", "ZERO_ORDER_REMINDER_MESSAGES"),
    ("aiops_diagnostics.i18n", "DIAGNOSIS_FAILURE_MESSAGES"),
    ("aiops_diagnostics.i18n", "DIAGNOSIS_ERROR_MESSAGES"),
    ("aiops_diagnostics.i18n", "FREE_TEXT_UNAVAILABLE_MESSAGES"),
    ("aiops_diagnostics.health_report_copy", "HEALTH_SUMMARY_MESSAGES"),
    ("aiops_diagnostics.health_report_copy", "HEALTH_CURVE_NAMES"),
    ("aiops_diagnostics.health_report_copy", "HEALTH_STOP_FALLBACK_MESSAGES"),
    ("aiops_diagnostics.health_report_copy", "YKC_STOP_REASON_MESSAGES"),
)


def check_python_tables(cc) -> list[str]:
    """Verify the copy tables that live in source, not in a data file.

    These are imported rather than textually scanned — its these tables ARE the
    seed for a fresh install, so a gap here is a Traditional button that reads
    Simplified the day it is created.

    This replaced a textual "does the module mention `zh-Hant` at all" test,
    which was worthless: once ANY key carried the tag, every later edit to a
    `zh` string still passed. The gate has to compare VALUES, not presence.
    """
    import importlib

    from aiops_diagnostics import shortcut_lifecycle

    stale: list[str] = []
    for module_name, table_name in FLAT_TABLES:
        table = getattr(importlib.import_module(module_name), table_name)
        _check_language_map(table, f"{module_name}.{table_name}", cc, stale)

    # The shortcut seeds are nested (`entry -> code -> field -> {tag: text}`),
    # so they need walking rather than a flat lookup.
    for entry, fields in shortcut_lifecycle._BUNDLED_SHORTCUTS:
        for code, spec in fields.items():
            for field in ("labels", "descriptions", "question_templates"):
                if field in spec:
                    where = f"shortcut_lifecycle._BUNDLED_SHORTCUTS[{entry}][{code}].{field}"
                    _check_language_map(spec[field], where, cc, stale)
    for code, spec in shortcut_lifecycle._SHARED_ACTION_FIELDS.items():
        if "question_templates" in spec:
            _check_language_map(
                spec["question_templates"],
                f"shortcut_lifecycle._SHARED_ACTION_FIELDS[{code}].question_templates",
                cc,
                stale,
            )
    return stale


def _import_path() -> None:
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the committed zh-Hant copy is not what the zh authority derives",
    )
    args = parser.parse_args()
    cc = _converter()
    _import_path()

    stale: list[str] = []
    for path, derive in (
        (CATALOG, derive_catalog),
        (RECOMMENDATIONS, derive_recommendations),
    ):
        current = load(path)
        derived = dump(derive(json.loads(dump(current)), cc))
        if derived == dump(current):
            continue
        if args.check:
            stale.append(str(path.relative_to(ROOT)))
        else:
            path.write_text(derived, encoding="utf-8")

    # The Python copy tables are derived too, but they are source files: this
    # script verifies them rather than rewriting them, so a conversion change
    # has to be applied deliberately rather than by a tool that edits code.
    stale.extend(check_python_tables(cc))

    if args.check and stale:
        print("zh-Hant 不是 zh 权威原文的派生结果：", file=sys.stderr)
        for item in stale:
            print(f"  - {item}", file=sys.stderr)
        print("重新运行 tools/derive_zh_hant.py 并按需更新 Python 文案表。", file=sys.stderr)
        return 1

    print(json.dumps({"converted": not args.check, "stale": stale}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
