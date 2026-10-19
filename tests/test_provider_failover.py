"""A provider that runs dry must not take the product down with it.

41 live (2026-10-19): the paid provider answered every turn with

    403 Forbidden [sk-x4CF…] 预扣费额度失败, 用户剩余额度: ¥0.007319,
    需要预扣费额度: ¥0.017302

and because nothing else could serve, EVERYTHING stopped — knowledge-base
answers, chit-chat, promotional cards — while a funded key slot sat unused in
the same config file. These tests pin the two halves of the fix: when a standby
is used, and when it deliberately is not.
"""

from __future__ import annotations

import pytest

from aiops_diagnostics.codex_runtime import (
    AgentContractError,
    AgentRuntimeError,
    AgentTurnTimeout,
    ProviderOutage,
    is_provider_outage,
)


class TestOutageClassification:
    """Which failures may be retried elsewhere — and which must not be."""

    @pytest.mark.parametrize(
        "message",
        [
            # The live 41 failure. Its text is Chinese and contains no English
            # status word, which is exactly why a status-only rule was not enough.
            "Codex turn t failed: unexpected status 403 Forbidden: 预扣费额度失败, 用户剩余额度: ¥0.007319",
            "Codex turn t failed: unexpected status 401 Unauthorized: invalid api key",
            "Codex turn t failed: 429 Too Many Requests",
            "Codex turn t failed: 502 Bad Gateway",
            "Codex turn t failed: 503 Service Unavailable",
            "Codex turn t failed: upstream quota exceeded",
            "Codex turn t failed: insufficient balance",
            "Codex turn t failed: Account in Arrearage",
            "Codex turn t failed: 额度不足",
            "Codex turn t failed: connection reset by peer",
        ],
    )
    def test_provider_side_failures_are_retryable(self, message: str) -> None:
        assert is_provider_outage(AgentRuntimeError(message)) is True

    @pytest.mark.parametrize(
        "message",
        [
            # The model answered; it just answered wrongly. Re-asking a second
            # provider would produce a plausible answer and hide the defect.
            "customer QA answer failed contract validation: blocks.0.kind Field required",
            "Codex turn t returned invalid JSON",
            "Codex turn t returned no final response",
            "lightweight classifier returned invalid intent",
        ],
    )
    def test_contract_violations_are_never_retryable(self, message: str) -> None:
        assert is_provider_outage(AgentContractError(message)) is False
        assert is_provider_outage(AgentRuntimeError(message)) is False

    def test_timeout_is_not_a_provider_outage(self) -> None:
        """We cut it off; that is our deadline, not their outage.

        Retrying a timeout on a second provider would double the wall-clock a
        user waits, and the first provider may simply be slow rather than down.
        """
        assert is_provider_outage(AgentTurnTimeout("Codex turn t exceeded 600 seconds")) is False

    def test_unrecognised_errors_stay_themselves(self) -> None:
        """Conservative default: anything not understood is NOT retried.

        A blanket retry would dress an unanticipated defect as a success on the
        second provider.
        """
        assert is_provider_outage(AgentRuntimeError("something nobody anticipated")) is False
        assert is_provider_outage(RuntimeError("403 Forbidden")) is False
        assert is_provider_outage(ValueError("403")) is False


class TestProviderChain:
    """Registry order is priority order; nothing else selects a standby."""

    def _settings(self, *names: str, default: str):
        from aiops_diagnostics.config import AgentSettings, ProviderConfig

        providers = tuple(
            ProviderConfig(name=n, base_url=f"https://{n}.example/v1", model=f"m-{n}") for n in names
        )
        return AgentSettings(providers=providers, default_provider=default)

    def test_selected_provider_is_first_and_later_ones_are_standbys(self) -> None:
        settings = self._settings("baoyun", "commandcode", default="baoyun")
        chain = settings.provider_chain(None)
        assert [p.name for p in chain] == ["baoyun", "commandcode"]

    def test_providers_before_the_default_are_not_standbys(self) -> None:
        """Superseding a provider must not let it take priority back on failure."""
        settings = self._settings("baoyun", "commandcode", default="commandcode")
        assert [p.name for p in settings.provider_chain(None)] == ["commandcode"]

    def test_explicit_provider_selects_its_own_chain(self) -> None:
        settings = self._settings("a", "b", "c", default="a")
        assert [p.name for p in settings.provider_chain("b")] == ["b", "c"]

    def test_single_provider_yields_a_one_element_chain(self) -> None:
        settings = self._settings("only", default="only")
        assert [p.name for p in settings.provider_chain(None)] == ["only"]

    def test_legacy_single_provider_config_still_works(self) -> None:
        """No registry at all: one synthesized provider, no special-casing needed."""
        from aiops_diagnostics.config import AgentSettings

        settings = AgentSettings(providers=())
        chain = settings.provider_chain(None)
        assert len(chain) == 1
        assert chain[0].name == "aiops-api"


class TestSessionFailover:
    """The loop in `SDKCodexSession.run`: does a turn actually move providers?"""

    @staticmethod
    def _session(*names: str, default: str, outcome: dict[str, Exception | None]):
        """A session wired to a scripted per-provider outcome, no transport."""
        from types import SimpleNamespace

        from aiops_diagnostics.codex_runtime import CodexTurnOutput, SDKCodexSession
        from aiops_diagnostics.config import AgentSettings, ProviderConfig

        providers = tuple(
            ProviderConfig(name=n, base_url=f"https://{n}.example/v1", model=f"m-{n}") for n in names
        )
        session = object.__new__(SDKCodexSession)
        session.workspace = SimpleNamespace(path="/tmp", run_id="run-test", language="zh")
        session.settings = AgentSettings(providers=providers, default_provider=default)
        session._provider = session.settings.select_provider(None)
        session._provider_key = "k"
        session._turn_registrar = None
        session._progress_callback = None
        session._codex = None
        session.events = []
        session.switches = []

        def _record_event(event):
            session.events.append(event)

        session._record_event = _record_event

        def _run_once(prompt, *, output_schema=None):
            del prompt, output_schema
            raise outcome["error"] if outcome["error"] else CodexTurnOutput("t", "ok", {})

        session._run_once = _run_once

        def _switch_provider(provider):
            session.switches.append(provider.name)
            session._provider = provider

        session._switch_provider = _switch_provider
        return session

    def test_outage_moves_the_turn_to_the_standby(self) -> None:
        session = self._session(
            "baoyun",
            "commandcode",
            default="baoyun",
            outcome={"error": ProviderOutage("403 预扣费额度失败")},
        )
        # The standby "succeeds" — scripted so the SECOND attempt returns.
        calls: list[str] = []

        def _run_once(prompt, *, output_schema=None):
            del prompt, output_schema
            calls.append(session._provider.name)
            if session._provider.name == "baoyun":
                raise ProviderOutage("403 Forbidden 预扣费额度失败, 用户剩余额度: ¥0.007319")
            from aiops_diagnostics.codex_runtime import CodexTurnOutput

            return CodexTurnOutput("t", "standby answered", {})

        session._run_once = _run_once
        result = session.run("hi")
        assert result.final_response == "standby answered"
        assert calls == ["baoyun", "commandcode"]
        assert session.switches == ["commandcode"]
        assert any(e["type"] == "codex_provider_failover" for e in session.events)

    def test_last_provider_failure_is_raised_not_swallowed(self) -> None:
        """When every provider is out the error must surface, not vanish."""
        session = self._session(
            "baoyun",
            "commandcode",
            default="baoyun",
            outcome={"error": ProviderOutage("403 预扣费额度失败")},
        )

        def _run_once(prompt, *, output_schema=None):
            del prompt, output_schema
            raise ProviderOutage(f"403 Forbidden from {session._provider.name}")

        session._run_once = _run_once
        with pytest.raises(ProviderOutage):
            session.run("hi")

    def test_contract_violation_never_fails_over(self) -> None:
        """A wrong answer is a defect; a second provider must not paper over it."""
        session = self._session(
            "baoyun",
            "commandcode",
            default="baoyun",
            outcome={"error": AgentContractError("blocks.0.kind Field required")},
        )
        with pytest.raises(AgentContractError):
            session.run("hi")
        assert session.switches == [], "a contract violation must not reach the standby"

    def test_single_provider_outage_raises_without_switching(self) -> None:
        session = self._session("only", default="only", outcome={"error": ProviderOutage("503")})
        with pytest.raises(ProviderOutage):
            session.run("hi")
        assert session.switches == []
