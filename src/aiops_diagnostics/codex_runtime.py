from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from openai_codex import ApprovalMode, Codex, CodexConfig

from aiops_diagnostics.agent_contracts import agent_turn_schema
from aiops_diagnostics.agent_workspace import AgentWorkspace
from aiops_diagnostics.config import AgentSettings, ProviderConfig, canonical_provider_base_url
from aiops_diagnostics.private_files import (
    PrivatePathError,
    ensure_private_directory,
    validate_private_directory,
    validate_private_file,
    write_private_text,
)

PROVIDER_ID = "aiops-api"
PROVIDER_KEY_ENV = "AIOPS_CODEX_PROVIDER_KEY"

RUNTIME_CONFIG_TEMPLATE = """web_search = "disabled"
default_permissions = "aiops-diagnostic"
model_provider = "{provider_name}"
project_root_markers = []

[analytics]
enabled = false

[otel]
exporter = "none"
trace_exporter = "none"
metrics_exporter = "none"
log_user_prompt = false

[shell_environment_policy]
inherit = "core"
ignore_default_excludes = false

[permissions.aiops-diagnostic]
description = "Read only access to one private AI-Ops diagnostic run workspace."

[permissions.aiops-diagnostic.filesystem]
glob_scan_max_depth = 4
":minimal" = "read"
{codex_bin_path} = "read"

[permissions.aiops-diagnostic.filesystem.":workspace_roots"]
"." = "read"
"**/*.env" = "deny"
".inputs/**" = "deny"
"state.json" = "deny"
"events.jsonl" = "deny"
"result.json" = "deny"
"evidence-journal.jsonl" = "deny"

[permissions.aiops-diagnostic.network]
enabled = false

[features]
multi_agent = false

[windows]
sandbox = {windows_sandbox}

[model_providers.{provider_name}]
name = "AI-Ops pluggable API provider"
base_url = {base_url}
env_key = "AIOPS_CODEX_PROVIDER_KEY"
wire_api = "{wire_api}"
request_max_retries = 3
stream_max_retries = 3
"""


class AgentRuntimeError(RuntimeError):
    """The Codex diagnostic runtime could not complete a turn."""


class AgentTurnTimeout(AgentRuntimeError):
    """A Codex turn exceeded the configured deadline and was interrupted."""


@dataclass(frozen=True, slots=True)
class CodexTurnOutput:
    turn_id: str
    final_response: str
    usage: dict[str, Any]


class CodexSession(Protocol):
    @property
    def thread_id(self) -> str: ...

    def run(self, prompt: str) -> CodexTurnOutput: ...

    def set_progress_callback(self, callback: Callable[[dict[str, Any]], None] | None) -> None: ...

    def close(self) -> None: ...


class SDKCodexSession:
    def __init__(
        self,
        workspace: AgentWorkspace,
        settings: AgentSettings,
        *,
        provider: ProviderConfig | None = None,
        thread_id: str | None = None,
    ) -> None:
        self.workspace = workspace
        self.settings = settings
        self._provider = provider or settings.select_provider(None)
        self._progress_callback: Callable[[dict[str, Any]], None] | None = None
        provider_key = ""
        codex: Codex | None = None
        try:
            provider_key = resolve_provider_api_key(settings, provider=self._provider)
            self._provider_key = provider_key
            runtime_home = prepare_runtime_home(settings, provider=self._provider)
            launch_args = codex_launch_args(settings)
            config = CodexConfig(
                launch_args_override=launch_args,
                cwd=str(workspace.path),
                env={
                    "CODEX_HOME": str(runtime_home),
                    PROVIDER_KEY_ENV: provider_key,
                },
                client_name="aiops_diagnostics",
                client_title="AI-Ops Diagnostic Harness",
            )
            codex = Codex(config)
            self._codex = codex
            codex.__enter__()
            kwargs: dict[str, Any] = {
                "approval_mode": ApprovalMode.deny_all,
                "cwd": str(workspace.path),
                "developer_instructions": _developer_instructions(),
                "model_provider": self._provider.name,
            }
            if self._provider.model:
                kwargs["model"] = self._provider.model
            if thread_id:
                self._thread = self._codex.thread_resume(thread_id, **kwargs)
            else:
                self._thread = self._codex.thread_start(**kwargs)
                with contextlib.suppress(Exception):
                    self._thread.set_name(f"AI-Ops {workspace.run_id}")
        except Exception as exc:
            if codex is not None:
                with contextlib.suppress(Exception):
                    codex.__exit__(None, None, None)
            message = str(exc).replace(provider_key, "REDACTED")
            raise AgentRuntimeError(f"Codex app-server initialization failed: {message}") from exc

    @property
    def thread_id(self) -> str:
        return self._thread.id

    def set_progress_callback(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        self._progress_callback = callback

    def run(self, prompt: str) -> CodexTurnOutput:
        handle = self._thread.turn(prompt, output_schema=agent_turn_schema())
        self._record_event({"type": "codex_turn_started", "thread_id": self.thread_id, "turn_id": handle.id})
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aiops-codex-turn")
        future = executor.submit(handle.run)
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(heartbeat_stop, handle.id),
            name="aiops-codex-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            result = future.result(timeout=self.settings.turn_timeout_seconds)
        except FutureTimeoutError as exc:
            with contextlib.suppress(Exception):
                handle.interrupt()
            self._record_event(
                {"type": "codex_turn_timeout", "thread_id": self.thread_id, "turn_id": handle.id}
            )
            raise AgentTurnTimeout(
                f"Codex turn {handle.id} exceeded {self.settings.turn_timeout_seconds} seconds"
            ) from exc
        except Exception as exc:
            message = str(exc).replace(self._provider_key, "REDACTED")
            self._record_event(
                {
                    "type": "codex_turn_failed",
                    "thread_id": self.thread_id,
                    "turn_id": handle.id,
                    "error_type": exc.__class__.__name__,
                    "error": message,
                }
            )
            raise AgentRuntimeError(f"Codex turn {handle.id} failed: {message}") from exc
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
            executor.shutdown(wait=False, cancel_futures=True)
        if not result.final_response:
            raise AgentRuntimeError(f"Codex turn {handle.id} returned no final response")
        usage = (
            result.usage.model_dump(mode="json")
            if result.usage and hasattr(result.usage, "model_dump")
            else {}
        )
        self._record_event(
            {
                "type": "codex_turn_completed",
                "thread_id": self.thread_id,
                "turn_id": handle.id,
                "status": str(result.status),
                "usage": usage,
            }
        )
        return CodexTurnOutput(
            turn_id=handle.id,
            final_response=result.final_response,
            usage=usage,
        )

    def _heartbeat_loop(self, stop: threading.Event, turn_id: str) -> None:
        started = time.monotonic()
        interval = self.settings.heartbeat_interval_seconds
        while not stop.wait(interval):
            self._record_event(
                {
                    "type": "codex_turn_heartbeat",
                    "thread_id": self.thread_id,
                    "turn_id": turn_id,
                    "elapsed_seconds": int(time.monotonic() - started),
                }
            )

    def _record_event(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = self.workspace.append_event(event)
        if self._progress_callback is not None:
            with contextlib.suppress(Exception):
                self._progress_callback(payload)
        return payload

    def close(self) -> None:
        self._codex.__exit__(None, None, None)

    def __enter__(self) -> SDKCodexSession:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()


def prepare_runtime_home(settings: AgentSettings, *, provider: ProviderConfig | None = None) -> Path:
    selected = provider or settings.select_provider(None)
    settings.validate()
    runtime_home = Path(settings.codex_runtime_home).expanduser().resolve()
    try:
        ensure_private_directory(runtime_home)
    except PrivatePathError as exc:
        raise AgentRuntimeError(str(exc)) from exc
    auth_link = runtime_home / "auth.json"
    if auth_link.exists() or auth_link.is_symlink():
        raise AgentRuntimeError("API provider runtime home 不得包含 auth.json")
    try:
        write_private_text(runtime_home / "config.toml", runtime_config(settings, provider=selected))
    except PrivatePathError as exc:
        raise AgentRuntimeError(str(exc)) from exc
    return runtime_home


def runtime_config(settings: AgentSettings, *, provider: ProviderConfig | None = None) -> str:
    selected = provider or settings.select_provider(None)
    base_url = canonical_provider_base_url(selected.base_url)
    codex_bin_path = json.dumps(str(Path(settings.codex_bin).expanduser().resolve()), ensure_ascii=False)
    return RUNTIME_CONFIG_TEMPLATE.format(
        provider_name=selected.name,
        base_url=json.dumps(base_url, ensure_ascii=False),
        wire_api=selected.wire_api,
        codex_bin_path=codex_bin_path,
        windows_sandbox=json.dumps(settings.windows_sandbox),
    )


def codex_launch_args(settings: AgentSettings) -> tuple[str, ...]:
    codex_bin = str(Path(settings.codex_bin).expanduser().resolve())
    app_server_args = (codex_bin, "app-server", "--listen", "stdio://")
    if getattr(sys, "frozen", False):
        return (sys.executable, "__codex-launcher", *app_server_args)
    return (
        sys.executable,
        "-m",
        "aiops_diagnostics.codex_launcher",
        *app_server_args,
    )


def resolve_provider_api_key(
    settings: AgentSettings,
    *,
    provider: ProviderConfig | None = None,
    key_slot: str | None = None,
) -> str:
    selected = provider or settings.select_provider(None)
    settings.validate()
    if selected.api_key_env:
        value = os.getenv(selected.api_key_env, "").strip()
        if value:
            return value
    if settings.api_key_file:
        configured_path = Path(settings.api_key_file).expanduser()
        if configured_path.is_symlink():
            raise AgentRuntimeError(f"Codex API key 文件不得是符号链接: {configured_path}")
        key_path = configured_path.resolve()
    else:
        effective_slot = key_slot or selected.resolved_key_slot()
        configured_dir = Path(settings.key_dir).expanduser()
        if configured_dir.is_symlink():
            raise AgentRuntimeError(f"Codex key slot 目录不得是符号链接: {configured_dir}")
        key_dir = configured_dir.resolve()
        try:
            validate_private_directory(key_dir)
        except PrivatePathError as exc:
            raise AgentRuntimeError(str(exc)) from exc
        key_path = key_dir / f"{effective_slot}.key"
    if key_path.is_symlink() or not key_path.is_file():
        raise AgentRuntimeError(
            f"Codex API key 不存在: slot={settings.key_slot}; 请设置 {settings.api_key_env} 或受限 key 文件"
        )
    try:
        validate_private_directory(key_path.parent)
        validate_private_file(key_path)
    except PrivatePathError as exc:
        raise AgentRuntimeError(str(exc)) from exc
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(key_path, flags)
    except OSError as exc:
        raise AgentRuntimeError(f"Codex API key 文件无法安全打开: {key_path}") from exc
    try:
        with os.fdopen(descriptor, encoding="utf-8") as key_file:
            descriptor = -1
            value = key_file.read().strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not value:
        raise AgentRuntimeError(f"Codex API key 文件为空: {key_path}")
    return value


def provider_key_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _developer_instructions() -> str:
    return """You are the primary causal diagnostic actor for one charging-order incident.
The surrounding Python harness is intentionally thin. It enforces identity,
read-only tools, evidence journaling, redaction, limits, resume, and output
validation. It does not decide the root cause for you.

Operate only inside the current run workspace. Use staged SOP and backend
references to understand business behavior. Do not inspect parent directories,
home directories, host configuration, credentials, or unrelated repositories.
Do not use network access.

You cannot query production directly. To obtain evidence, return
kind=tool_requests with allowed tool names and a concrete reason. The harness
executes fixed, bounded, read-only operations and resumes this same thread with
evidence IDs and artifact paths. Request order_snapshot before dependent tools.
You may request known_runbook as advisory evidence, but it is not ground truth.

When evidence is sufficient, return kind=diagnosis. Preserve incident identity
exactly. Every hypothesis and causal conclusion must cite evidence IDs.
Distinguish absent data, blocked dependencies, and failed sources. A failed
source limits confidence. Never claim that recalculation, refund, replay,
resend, order modification, service restart, or another mutation was executed.

Write every human-facing field in Simplified Chinese: summary, root_cause,
each hypothesis title and explanation, and each next_steps entry. Keep order
numbers, tenant IDs, field names, tool names, evidence IDs, and other code
identifiers in their original form without translation.
"""
