from __future__ import annotations

import hashlib
import json
import secrets
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiops_diagnostics.agent_contracts import AgentDiagnosis, IncidentManifest, RunState
from aiops_diagnostics.private_files import (
    append_private_text,
    ensure_private_directory,
    protect_private_file,
    validate_private_directory,
    write_private_text,
)

BACKEND_REFERENCES = (
    "backend-v2-domestic/cloud-charging-pile/cloud-charging-pile-core/src/main/java/"
    "com/qushiyun/cloud/charging/pile/core/application/listener/handler/car/impl/"
    "AbnormalOrderTxDataHandler.java",
    "backend-v2-domestic/cloud-charging-pile/cloud-charging-pile-core/src/main/java/"
    "com/qushiyun/cloud/charging/pile/core/domain/liteflow/components/stop/"
    "FourPriceComputeComponent.java",
    "backend-v2-domestic/cloud-charging-pile/cloud-charging-pile-data/src/main/java/"
    "com/qushiyun/cloud/charging/pile/data/po/ChOrderInfo.java",
)


@dataclass(frozen=True, slots=True)
class AgentWorkspace:
    run_id: str
    path: Path

    @classmethod
    def create(
        cls,
        project_root: Path,
        run_root: Path,
        manifest: IncidentManifest,
        *,
        fixture_path: Path | None = None,
        provider_base_url: str = "",
        provider: str = "",
        key_slot: str = "default",
    ) -> AgentWorkspace:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"run-{timestamp}-{manifest.source_hash[:8]}-{secrets.token_hex(2)}"
        path = run_root.expanduser().resolve() / run_id
        ensure_private_directory(path)
        for child in ("evidence", "references"):
            ensure_private_directory(path / child)
        workspace = cls(run_id=run_id, path=path)
        workspace.write_json("incident.json", manifest.model_dump(mode="json"))
        workspace._stage_references(project_root.resolve())
        workspace.write_text("AGENTS.md", _runtime_instructions())
        staged_fixture, fixture_sha256 = workspace._stage_fixture(fixture_path)
        workspace.save_state(
            RunState(
                run_id=run_id,
                incident_id=manifest.incident_id,
                next_prompt="",
                fixture_path=staged_fixture,
                fixture_sha256=fixture_sha256,
                provider_base_url=provider_base_url,
                provider=provider,
                key_slot=key_slot,
            )
        )
        return workspace

    @classmethod
    def open(cls, run_root: Path, run_id: str) -> AgentWorkspace:
        if not run_id.startswith("run-") or "/" in run_id or ".." in run_id:
            raise ValueError("非法 run_id")
        path = run_root.expanduser().resolve() / run_id
        if path.is_symlink():
            raise PermissionError(f"诊断运行目录不得是符号链接: {run_id}")
        if not path.is_dir():
            raise FileNotFoundError(f"诊断运行不存在: {run_id}")
        validate_private_directory(path)
        return cls(run_id=run_id, path=path)

    def load_manifest(self) -> IncidentManifest:
        return IncidentManifest.model_validate(self.read_json("incident.json"))

    def load_state(self) -> RunState:
        state = RunState.model_validate(self.read_json("state.json"))
        if state.run_id != self.run_id:
            raise ValueError("state.json 的 run_id 与运行目录不一致")
        return state

    def save_state(self, state: RunState) -> None:
        self.write_json("state.json", state.model_dump(mode="json"))

    def save_result(self, result: AgentDiagnosis) -> None:
        self.write_json("result.json", result.model_dump(mode="json"))

    def load_result(self) -> AgentDiagnosis:
        return AgentDiagnosis.model_validate(self.read_json("result.json"))

    def resolve_fixture(self) -> Path | None:
        state = self.load_state()
        if not state.fixture_path:
            return None
        target = self._resolve(state.fixture_path)
        if not target.is_file() or not state.fixture_sha256:
            raise ValueError("诊断 fixture 缺失或未记录哈希")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != state.fixture_sha256:
            raise ValueError("诊断 fixture 已变化，拒绝在同一 incident/thread 中继续")
        return target

    def write_json(self, relative: str, payload: Any) -> Path:
        return self.write_text(
            relative,
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        )

    def write_text(self, relative: str, content: str) -> Path:
        target = self._resolve(relative)
        return write_private_text(target, content)

    def append_event(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "at": datetime.now(UTC).isoformat(),
            **event,
        }
        target = self._resolve("events.jsonl")
        append_private_text(target, json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return payload

    def read_json(self, relative: str) -> Any:
        return json.loads(self._resolve(relative).read_text(encoding="utf-8"))

    def _resolve(self, relative: str) -> Path:
        target = (self.path / relative).resolve()
        if self.path not in target.parents and target != self.path:
            raise ValueError("运行文件路径越界")
        return target

    def _stage_references(self, project_root: Path) -> None:
        references = (
            "SOP.md",
            "充电桩问题排查SOP.md",
            "docs/architecture.md",
            "src/aiops_diagnostics/engine.py",
            "src/aiops_diagnostics/rules.py",
            *BACKEND_REFERENCES,
        )
        copied: list[str] = []
        missing: list[str] = []
        for relative in references:
            source = project_root / relative
            if not source.is_file():
                missing.append(relative)
                continue
            target = self._resolve(f"references/{relative}")
            ensure_private_directory(target.parent)
            shutil.copyfile(source, target)
            protect_private_file(target)
            copied.append(relative)
        self.write_text(
            "references/INDEX.md",
            "# Diagnostic References\n\n"
            + "## Staged\n\n"
            + "\n".join(f"- `{item}`" for item in copied)
            + "\n\n## Missing\n\n"
            + ("\n".join(f"- `{item}`" for item in missing) if missing else "- None")
            + "\n",
        )

    def _stage_fixture(self, fixture_path: Path | None) -> tuple[str | None, str | None]:
        if fixture_path is None:
            return None, None
        if fixture_path.expanduser().is_symlink():
            raise ValueError("诊断 fixture 不得是符号链接")
        source = fixture_path.expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"诊断 fixture 不存在: {source}")
        relative = ".inputs/fixture.json"
        target = self._resolve(relative)
        ensure_private_directory(target.parent)
        shutil.copyfile(source, target)
        protect_private_file(target)
        return relative, hashlib.sha256(target.read_bytes()).hexdigest()


def _runtime_instructions() -> str:
    return """# AI-Ops Diagnostic Runtime

- This workspace contains a single immutable charging-order incident.
- You are the causal diagnostic actor. The harness only enforces safety, evidence, and delivery contracts.
- Read only files inside this run workspace. Do not inspect parent directories,
  home directories, credentials, or host configuration.
- Request production evidence only through the structured tool request response.
  Never write SQL or invoke database, Redis, SSH, service, refund, replay,
  recalculation, or mutation commands.
- `known_runbook` is advisory deterministic evidence, not ground truth.
- Every causal hypothesis and final conclusion must cite evidence IDs from the journal.
- Distinguish missing data from a failed or blocked source. Never raise confidence
  when a required source failed.
- The first release may recommend an engineer action, but it must never claim that an action was executed.
"""
