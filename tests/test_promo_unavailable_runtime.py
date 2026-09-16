"""Runtime-level coverage for the promotional card when nothing was searched.

41 live regression (2026-09-16): tapping 行业方案 returned
"当前没有可用的行业方案，未检索到匹配的宣传资料。" — a claim that the library
holds no match. That shortcut has no promotional target bound, so no query was
ever issued: the system asserted something about content it never looked at.

These drive the real GatewayRuntime, because the branch that builds the card
lives inside it and the lower-level helper cannot reach it.
"""

from __future__ import annotations

import os
import time as time_module
from pathlib import Path
from typing import Any

from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


def _runtime(tmp_path: Path):
    """A GatewayRuntime on a private temp database, mirroring the harness in
    test_agent_metrics.py."""
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime

    os.chmod(tmp_path, 0o750)
    config = tmp_path / "production.env"
    config.write_text("# test\n", encoding="utf-8")
    os.chmod(config, 0o600)
    gateway_settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=config,
    )
    settings = Settings()
    settings.agent.run_root = str(tmp_path / "runs")
    store = GatewayStore(gateway_settings.database_file)
    return GatewayRuntime(store, gateway_settings, settings)


def _context():
    subject = SubjectRecord(b_user_id="B-1", tenant_id="T-1")
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id="T-1",
        data_scope=DataScope(type="self"),
        roles=frozenset(),
        permissions=frozenset({"aiops:qa:write"}),
    )


def _wait_terminal(runtime: Any, context: Any, qa_id: str, *, deadline_s: float = 20.0) -> dict:
    deadline = time_module.time() + deadline_s
    job: dict[str, Any] = {}
    while time_module.time() < deadline:
        job = runtime.get_assistant_qa(context, qa_id) or {}
        if job.get("status") in {"completed", "failed"}:
            return job
        time_module.sleep(0.05)
    return job


def test_promo_without_resolvable_target_does_not_claim_an_empty_library(tmp_path: Path) -> None:
    """No pin resolves -> the card must say the search is unavailable, not that
    the library has nothing."""
    runtime = _runtime(tmp_path)
    context = _context()
    try:
        qa = runtime.start_assistant_qa(context, "给我看看行业解决方案", promo_intent="solution_discovery")
        job = _wait_terminal(runtime, context, qa["qa_id"])
    finally:
        runtime.shutdown()

    assert job["status"] == "completed", job
    result = job["result"]
    assert result["retrieval_status"] == "unavailable"
    text = result["blocks"][0]["text"]
    assert "未检索到匹配的宣传资料" not in text
    assert "不可用" in text


def test_promo_without_resolvable_target_is_english_when_asked(tmp_path: Path) -> None:
    """The outage copy is localized too; an unknown language degrades to zh."""
    runtime = _runtime(tmp_path)
    context = _context()
    try:
        qa = runtime.start_assistant_qa(
            context,
            "show me industry solutions",
            promo_intent="solution_discovery",
            language="en",
        )
        job = _wait_terminal(runtime, context, qa["qa_id"])
    finally:
        runtime.shutdown()

    assert job["status"] == "completed", job
    assert "temporarily unavailable" in job["result"]["blocks"][0]["text"]
