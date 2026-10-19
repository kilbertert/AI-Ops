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

    def test_turn_id_containing_a_status_code_is_not_an_outage(self) -> None:
        """Ids are opaque and the wrapper embeds them: "Codex turn t-403 failed".

        Matching a bare "403" anywhere would let an unrelated failure look like
        an outage, and the standby's answer would then hide it.
        """
        assert is_provider_outage(AgentRuntimeError("Codex turn t-403 failed: boom")) is False
        assert is_provider_outage(AgentRuntimeError("Codex turn abc500xyz failed: boom")) is False
        # A real status, in the shapes providers actually emit, still matches.
        assert is_provider_outage(AgentRuntimeError("Codex turn t failed: status 500 boom")) is True
        assert is_provider_outage(AgentRuntimeError("Codex turn t failed: 429 Too Many Requests")) is True

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
        session._turns_run = 0
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

    def test_a_broken_standby_does_not_block_the_next_one(self) -> None:
        """One misconfigured backup must not stop the ones behind it.

        A standby that cannot even be built (missing key file, bad base URL, no
        model) raises while switching. That must be recorded and skipped, not
        propagated — otherwise a single bad entry in the registry disables every
        provider after it.
        """
        session = self._session(
            "baoyun",
            "broken",
            "good",
            default="baoyun",
            outcome={"error": None},
        )
        tried: list[str] = []

        def _switch_provider(provider):
            session.switches.append(provider.name)
            if provider.name == "broken":
                raise ProviderOutage("standby provider broken could not be initialized: no key")
            session._provider = provider

        def _run_once(prompt, *, output_schema=None):
            del prompt, output_schema
            tried.append(session._provider.name)
            if session._provider.name == "baoyun":
                raise ProviderOutage("403 Forbidden 预扣费额度失败")
            from aiops_diagnostics.codex_runtime import CodexTurnOutput

            return CodexTurnOutput("t", "third provider answered", {})

        session._switch_provider = _switch_provider
        session._run_once = _run_once
        result = session.run("hi")
        assert result.final_response == "third provider answered"
        assert tried == ["baoyun", "good"], tried
        assert any(e["type"] == "codex_provider_failover_skipped" for e in session.events)

    def test_failover_is_refused_after_the_first_turn(self) -> None:
        """A mid-conversation switch would drop the context the prompt refers to.

        `next_prompt` in a diagnosis builds on what the model already reported.
        Serving it from a fresh thread on another provider is not a retry — it is
        a different, worse question that would look like a successful answer.
        """
        session = self._session(
            "baoyun",
            "commandcode",
            default="baoyun",
            outcome={"error": ProviderOutage("503")},
        )
        session._turns_run = 1  # one turn already completed on this thread
        with pytest.raises(ProviderOutage):
            session.run("follow-up that needs the earlier turns")
        assert session.switches == [], "no switch may happen mid-conversation"

    def test_single_provider_outage_raises_without_switching(self) -> None:
        session = self._session("only", default="only", outcome={"error": ProviderOutage("503")})
        with pytest.raises(ProviderOutage):
            session.run("hi")
        assert session.switches == []


class TestConstructionFailover:
    """An outage can happen before the first turn — during construction."""

    def test_construction_walks_the_chain(self, tmp_path) -> None:
        """A primary that cannot be BUILT must not block a buildable standby.

        The live failure mode: the primary's key file or base URL is bad, so the
        session never gets as far as running a turn. Building only the primary
        left the standby unreachable for exactly the outage that happened first.
        """

        from types import SimpleNamespace

        from aiops_diagnostics.codex_runtime import ProviderOutage, SDKCodexSession
        from aiops_diagnostics.config import AgentSettings, ProviderConfig

        settings = AgentSettings(
            providers=(
                ProviderConfig(name="primary", base_url="https://a.example/v1", model="m"),
                ProviderConfig(name="standby", base_url="https://b.example/v1", model="m"),
            ),
            default_provider="primary",
            key_dir=str(tmp_path),
        )
        built: list[str] = []

        def _fake_build(self, *, thread_id):
            del thread_id
            built.append(self._provider.name)
            if self._provider.name == "primary":
                # What `_build` actually raises after classification: a
                # provider-side cause, wrapped as an outage.
                raise ProviderOutage("Codex app-server initialization failed: 503 Service Unavailable")
            self._thread = SimpleNamespace(id=f"thread-{self._provider.name}")

        session = object.__new__(SDKCodexSession)
        session.workspace = SimpleNamespace(path=str(tmp_path), run_id="run", language="zh")
        session.settings = settings
        session._provider = settings.select_provider(None)
        session._progress_callback = None
        session._turn_registrar = None
        session._turns_run = 0
        session._codex = None
        session.events = []
        session._record_event = session.events.append
        session._build = _fake_build.__get__(session, SDKCodexSession)
        session._teardown_codex = lambda: None
        session._run_build_chain(thread_id=None)

        assert built == ["primary", "standby"]
        assert session._provider.name == "standby"

    def test_resume_never_walks_the_chain(self, tmp_path) -> None:
        """A resume is pinned to the provider that owns the thread."""
        from types import SimpleNamespace

        from aiops_diagnostics.codex_runtime import SDKCodexSession
        from aiops_diagnostics.config import AgentSettings, ProviderConfig

        settings = AgentSettings(
            providers=(
                ProviderConfig(name="primary", base_url="https://a.example/v1", model="m"),
                ProviderConfig(name="standby", base_url="https://b.example/v1", model="m"),
            ),
            default_provider="primary",
        )
        built: list[str] = []

        def _fake_build(self, *, thread_id):
            built.append(self._provider.name)
            self._thread = SimpleNamespace(id=thread_id or "fresh")

        session = object.__new__(SDKCodexSession)
        session.workspace = SimpleNamespace(path=str(tmp_path), run_id="run", language="zh")
        session.settings = settings
        session._provider = settings.select_provider(None)
        session._progress_callback = None
        session._turn_registrar = None
        session._turns_run = 0
        session._codex = None
        session.events = []
        session._record_event = session.events.append
        session._build = _fake_build.__get__(session, SDKCodexSession)
        session._teardown_codex = lambda: None
        session._run_build_chain(thread_id="t-123")

        assert built == ["primary"], "a resume must not switch provider"
