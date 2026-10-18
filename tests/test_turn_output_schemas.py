"""Every turn is handed its OWN output schema, not the diagnosis one.

``SDKCodexSession.run`` falls back to ``agent_turn_schema()`` when the caller
passes no ``output_schema`` — and that is the DIAGNOSIS schema. A strict
provider then makes the model answer in that shape no matter what the prompt
asked for, so the turn comes back with no field the caller reads.

Measured on 41 (2026-10-19): the zero-order "你好" turn returned
``{"kind":"diagnosis","diagnosis":{...}}``; ``payload["text"]`` was absent, so
the job failed with "zero-order answer returned empty text" and the user got
nothing. It was intermittent — it needed the provider to enforce the schema —
which is exactly why it survived review and only surfaced under a live probe.

These tests pin the wiring, not the schema text: a caller that stops passing its
schema restores the defect, and the assertion fails here rather than in
production.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

SCOPE = "tenant-a"


def _capturing_session(captured: list[dict[str, Any]], answer: str):
    """A stand-in SDKCodexSession recording the schema each turn was given."""

    class _Session:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def run(self, prompt: str, **kwargs):
            del prompt
            captured.append(kwargs)
            from aiops_diagnostics.codex_runtime import CodexTurnOutput

            return CodexTurnOutput(turn_id="t", final_response=answer, usage={})

        def close(self) -> None:
            return None

    return _Session


def test_zero_order_turn_gets_the_qa_schema(tmp_path: Path, monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "aiops_diagnostics.codex_runtime.SDKCodexSession",
        _capturing_session(captured, '{"text": "答案", "reminder": false}'),
    )
    from aiops_diagnostics.agent_runner import run_zero_order_answer
    from aiops_diagnostics.config import Settings

    settings = Settings()
    settings.agent.run_root = str(tmp_path / "runs")
    run_zero_order_answer("你好", settings, language="zh", history="")

    assert len(captured) == 1
    schema = captured[0].get("output_schema")
    assert schema is not None, "零阶回合没传 output_schema —— 会退回诊断 schema"
    # The distinguishing mark: the QA schema has `text`, the diagnosis schema
    # has `kind`/`diagnosis`. Asserting the positive AND the negative is what
    # makes this catch "handed the wrong schema" rather than merely "handed
    # nothing".
    assert "text" in schema["properties"]
    assert "diagnosis" not in schema["properties"]


def test_classifier_turn_gets_the_classifier_schema(tmp_path: Path, monkeypatch) -> None:
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "aiops_diagnostics.codex_runtime.SDKCodexSession",
        _capturing_session(
            captured, '{"intent": "casual", "confidence": "high", "risk": "low", "answer": "你好"}'
        ),
    )
    from aiops_diagnostics.agent_runner import classify_lightweight
    from aiops_diagnostics.config import Settings

    settings = Settings()
    settings.agent.run_root = str(tmp_path / "runs")
    classify_lightweight("你好", settings, language="zh")

    assert len(captured) == 1
    schema = captured[0].get("output_schema")
    assert schema is not None, "分类器回合没传 output_schema —— 会退回诊断 schema"
    assert "intent" in schema["properties"]
    assert "diagnosis" not in schema["properties"]
    # A strict provider rejects a schema whose properties are not all required
    # (`_make_strict_response_schema` lists every property for exactly this
    # reason), so the classifier request would fail outright rather than treat
    # `answer` as optional. The parser keeps accepting a missing `answer`.
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_every_turn_schema_is_strict() -> None:
    """All hand-written turn schemas follow the same strictness rule.

    The rule is the one `_make_strict_response_schema` applies to Pydantic-built
    schemas: every property required, no extras. A hand-written schema (which
    that function never sees) has to satisfy it by construction — otherwise the
    turn fails on a strict provider, which is the failure mode this whole change
    exists to remove.
    """
    from aiops_diagnostics.agent_contracts import (
        CLASSIFIER_TURN_SCHEMA,
        QA_RAG_TURN_SCHEMA,
        QA_TURN_SCHEMA,
    )

    def audit(node: Any, path: str) -> None:
        if isinstance(node, list):
            for index, item in enumerate(node):
                audit(item, f"{path}[{index}]")
            return
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        if isinstance(properties, dict):
            assert node.get("additionalProperties") is False, f"{path}: 缺 additionalProperties"
            assert set(node.get("required") or ()) == set(properties), (
                f"{path}: required 必须与 properties 完全一致（严格 provider 的要求）"
            )
        for key, value in node.items():
            audit(value, f"{path}.{key}")

    for name, schema in (
        ("QA_TURN_SCHEMA", QA_TURN_SCHEMA),
        ("CLASSIFIER_TURN_SCHEMA", CLASSIFIER_TURN_SCHEMA),
        ("QA_RAG_TURN_SCHEMA", QA_RAG_TURN_SCHEMA),
    ):
        audit(schema, name)
