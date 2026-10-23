#!/usr/bin/env python3
"""Prove #1001 over real HTTP on a throwaway data root.

Every RAGFlow library becomes one Dify external dataset, and it is **visible to the
team**, not just to its creator.

The repo requires this shape of evidence for anything that changes outward behaviour.
`TestClient` is a unit test; here the **real CLI process** runs and talks over real
sockets to two stubs, and the evidence records the **bodies** it sent.

| proves | does not prove |
|---|---|
| the CLI sends create-then-grant, PATCH carries permission + both ids | real Dify accepts them |
| a second run writes nothing | that RAGFlow's listing is what we think |
| `--dry-run` writes nothing | that a human can click it in the console |
| an upstream-deleted library is reported, never deleted | production liveness |
| a name the update route would reject fails **before** anything is created | |

Why a stub console and not the real Dify: this command **writes**. A verification that
touches the shared console would create datasets in a tenant that real agents use.

The stub models the one behaviour that caused the original bug: `POST
/console/api/datasets/external` stores `only_me` **even when asked otherwise** — because
that payload has no permission field at all. If the command forgets the follow-up PATCH,
the final state here is `only_me` and the check fails.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

TENANT = "T-SYNC"
WORKSPACE = "ws-0000-0000-0000-000000000001"
API_ID = "api-0000-0000-0000-000000000001"
CONSOLE_PORT = 8793
KB_PORT = 8794

#: What RAGFlow says this tenant owns. One name is deliberately fine, one is 60 bytes
#: so the "too long for the update route" leg has something to refuse.
LIBRARIES = [
    {"id": "kb-a", "name": "客服手册", "document_count": 3, "chunk_count": 30},
    {"id": "kb-b", "name": "售后流程", "document_count": 1, "chunk_count": 5},
]


class _State:
    """What the stub console 'stored'. Shared with the assertions after the run."""

    def __init__(self) -> None:
        self.datasets: dict[str, dict[str, Any]] = {}
        self.writes: list[dict[str, Any]] = []
        self._next = 0

    def create(self, body: dict[str, Any]) -> str:
        self._next += 1
        dataset_id = f"ds-{self._next}"
        # **The behaviour that caused the bug.** The real payload has no `permission`
        # field; the model default is only_me. Anything in `body` that looks like a
        # permission request is ignored here exactly as Dify ignores it.
        self.datasets[dataset_id] = {
            "id": dataset_id,
            "name": str(body.get("name") or ""),
            "permission": "only_me",
            # 创建接口**不写** retrieval_model —— 真 Dify 就是这样（落库 SQL NULL）。
            "external_retrieval_model": None,
            "external_knowledge_info": {
                "external_knowledge_id": str(body.get("external_knowledge_id") or ""),
                "external_knowledge_api_id": str(body.get("external_knowledge_api_id") or ""),
            },
        }
        return dataset_id

    def patch(self, dataset_id: str, body: dict[str, Any]) -> None:
        item = self.datasets.get(dataset_id)
        if item is None:
            raise KeyError(dataset_id)
        if not body.get("external_knowledge_id") or not body.get("external_knowledge_api_id"):
            raise ValueError("external knowledge id and api id are required")
        item["name"] = str(body.get("name") or item["name"])
        item["permission"] = str(body.get("permission") or item["permission"])
        # 与真 Dify 同一条：``external_retrieval_model`` 不给就保持 null，而检索对它
        # 直接 ``.get`` ⇒ 每次检索 AttributeError。所以"补了没有"必须能被观察到。
        if body.get("external_retrieval_model") is not None:
            item["external_retrieval_model"] = body["external_retrieval_model"]


STATE = _State()


class _ConsoleStub(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return

    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            parsed = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if self.path.startswith("/console/api/datasets?"):
            datasets = list(STATE.datasets.values())
            self._json({"data": datasets, "has_more": False, "total": len(datasets)})
            return
        self._json({"message": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        body = self._read()
        if self.path == "/console/api/datasets/external":
            dataset_id = STATE.create(body)
            STATE.writes.append({"op": "create", "dataset_id": dataset_id, "body": body})
            self._json(STATE.datasets[dataset_id], 201)
            return
        self._json({"message": "not found"}, 404)

    def do_PATCH(self) -> None:  # noqa: N802
        body = self._read()
        prefix = "/console/api/datasets/"
        if self.path.startswith(prefix):
            dataset_id = self.path[len(prefix) :]
            try:
                STATE.patch(dataset_id, body)
            except ValueError as exc:
                # Dify's real answer for a missing external id: a 500 whose message
                # names the field. Modelling it keeps a body-shaped bug visible.
                self._json({"message": str(exc)}, 500)
                return
            except KeyError:
                self._json({"message": "Dataset not found."}, 404)
                return
            STATE.writes.append({"op": "patch", "dataset_id": dataset_id, "body": body})
            self._json(STATE.datasets[dataset_id])
            return
        self._json({"message": "not found"}, 404)


class _KbStub(BaseHTTPRequestHandler):
    """Just the tenant-scoped listing this command reads."""

    def log_message(self, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/kb/knowledge-bases":
            self._send({"data": LIBRARIES})
            return
        self.send_error(404)

    def _send(self, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def seed(root: Path) -> None:
    """A throwaway store with one onboarded tenant. A fixture, not the thing under test."""
    sys.path.insert(0, str(repo_root() / "src"))
    from aiops_diagnostics.gateway_store import GatewayStore

    GatewayStore(root / "data" / "gateway.db").record_ragflow_tenant(
        TENANT,
        "verify",
        ragflow_email="sync@kb.internal",
        credential_ciphertext="enc:v1:fixture",
        model_checked=True,
    )


def main() -> int:
    repo = repo_root()
    root = Path(tempfile.mkdtemp(prefix="aiops-verify-1001-"))
    env = {
        **os.environ,
        "AIOPS_HOME": str(root),
        "AIOPS_CONFIG_HOME": str(root / "config"),
        "AIOPS_DATA_HOME": str(root / "data"),
        "AIOPS_GATEWAY_DATA_HOME": str(root / "data"),
        "AIOPS_GATEWAY_ENVIRONMENT": "verify",
        "AIOPS_GATEWAY_DATABASE_FILE": str(root / "data" / "gateway.db"),
        "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL": f"http://127.0.0.1:{CONSOLE_PORT}",
        "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY": "verify-console-key",
        "AIOPS_GATEWAY_KB_SERVICE_BASE_URL": f"http://127.0.0.1:{KB_PORT}",
        "PYTHONPATH": f"{repo / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep),
    }
    evidence: dict[str, Any] = {"temp_root": str(root), "checks": []}
    failures: list[str] = []

    def check(name: str, ok: bool, detail: object) -> None:
        evidence["checks"].append({"check": name, "ok": ok, "detail": detail})
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {detail}")
        if not ok:
            failures.append(name)

    console = ThreadingHTTPServer(("127.0.0.1", CONSOLE_PORT), _ConsoleStub)
    kb = ThreadingHTTPServer(("127.0.0.1", KB_PORT), _KbStub)
    for server in (console, kb):
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["uv", "run", "aiops", "admin", "sync-knowledge-datasets", *args],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    common = (
        "--tenant",
        TENANT,
        "--workspace",
        WORKSPACE,
        "--api-id",
        API_ID,
        "--db",
        str(root / "data" / "gateway.db"),
    )

    try:
        init = subprocess.run(
            ["uv", "run", "aiops", "init"], cwd=repo, env=env, capture_output=True, text=True
        )
        if init.returncode != 0:
            print("aiops init failed:\n" + (init.stdout + init.stderr)[-1500:])
            return 2
        seed(root)

        # ── 1. dry-run ────────────────────────────────────────────────
        proc = cli(*common, "--dry-run")
        payload = json.loads(proc.stdout or "{}")
        check(
            "dry-run plans two creates and writes nothing",
            proc.returncode == 0
            and [p["kind"] for p in payload.get("planned", [])] == ["create", "create"]
            and STATE.writes == [],
            {"rc": proc.returncode, "planned": payload.get("planned"), "writes": len(STATE.writes)},
        )

        # ── 2. the real run ──────────────────────────────────────────
        proc = cli(*common)
        first = json.loads(proc.stdout or "{}")
        check("sync exits 0", proc.returncode == 0, {"rc": proc.returncode, "stderr": proc.stderr[-300:]})
        check(
            "one dataset per library, keyed by the derived knowledge_id",
            sorted(d["external_knowledge_info"]["external_knowledge_id"] for d in STATE.datasets.values())
            == [f"tenant-{TENANT}:kb-a", f"tenant-{TENANT}:kb-b"],
            [d["external_knowledge_info"]["external_knowledge_id"] for d in STATE.datasets.values()],
        )
        check(
            "every dataset ends up visible to the whole team, not just its creator",
            {d["permission"] for d in STATE.datasets.values()} == {"all_team_members"},
            {d["id"]: d["permission"] for d in STATE.datasets.values()},
        )
        creates = [w for w in STATE.writes if w["op"] == "create"]
        patches = [w for w in STATE.writes if w["op"] == "patch"]
        check(
            "the create body carries no permission (the route has no such field)",
            all("permission" not in w["body"] for w in creates),
            [w["body"] for w in creates],
        )
        check(
            "every dataset ends up with a retrieval model (else Dify's retrieval crashes)",
            all(d.get("external_retrieval_model") for d in STATE.datasets.values()),
            {d["id"]: d.get("external_retrieval_model") for d in STATE.datasets.values()},
        )
        check(
            "every patch carries permission plus both external ids",
            len(patches) == 2
            and all(
                w["body"].get("permission") == "all_team_members"
                and w["body"].get("external_knowledge_id")
                and w["body"].get("external_knowledge_api_id") == API_ID
                for w in patches
            ),
            [w["body"] for w in patches],
        )
        check(
            "the first run reports two applied",
            len(first.get("applied", [])) == 2,
            first.get("applied"),
        )

        # ── 3. idempotent on the knowledge_id ────────────────────────
        before = len(STATE.writes)
        proc = cli(*common)
        second = json.loads(proc.stdout or "{}")
        check(
            "a second run writes nothing (idempotent on knowledge_id, not on name)",
            proc.returncode == 0 and len(STATE.writes) == before and second.get("applied") == [],
            {"rc": proc.returncode, "new_writes": len(STATE.writes) - before},
        )
        check(
            "the second run still lists both as unchanged",
            len(second.get("unchanged", [])) == 2,
            second.get("unchanged"),
        )

        # ── 4. a library deleted upstream is reported, never deleted ──
        LIBRARIES.pop()  # kb-b disappears from RAGFlow
        proc = cli(*common)
        third = json.loads(proc.stdout or "{}")
        check(
            "a library gone upstream becomes an orphan report, not a delete",
            proc.returncode == 0
            and third.get("orphans") == [f"tenant-{TENANT}:kb-b"]
            and len(STATE.datasets) == 2,
            {"orphans": third.get("orphans"), "datasets": len(STATE.datasets)},
        )

        # ── 5. a name the update route rejects fails before anything is created ──
        LIBRARIES.append({"id": "kb-c", "name": "库" * 20, "document_count": 0, "chunk_count": 0})
        before = len(STATE.writes)
        proc = cli(*common)
        check(
            "a 60-byte name is refused before any write",
            proc.returncode == 1
            and "DIFY_DATASET_NAME_TOO_LONG" in proc.stderr
            and len(STATE.writes) == before,
            {
                "rc": proc.returncode,
                "stderr": proc.stderr.strip()[-300:],
                "new_writes": len(STATE.writes) - before,
            },
        )

        # ── 6. an un-onboarded tenant is its own answer ──────────────
        proc = cli(
            "--tenant",
            "T-NEVER",
            "--workspace",
            WORKSPACE,
            "--api-id",
            API_ID,
            "--db",
            str(root / "data" / "gateway.db"),
        )
        check(
            "an un-onboarded tenant says so and sends nothing",
            proc.returncode == 1 and "没有开户" in proc.stderr and len(STATE.writes) == before,
            {"rc": proc.returncode, "stderr": proc.stderr.strip()[-200:]},
        )
    finally:
        for server in (console, kb):
            server.shutdown()
        out_dir = (repo / "var/verify-evidence").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        evidence["failures"] = failures
        evidence["final_state"] = STATE.datasets
        (out_dir / "sync-knowledge-datasets.json").write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        shutil.rmtree(root, ignore_errors=True)
        print(f"evidence: {out_dir / 'sync-knowledge-datasets.json'}")

    print("all checks passed" if not failures else f"{len(failures)} failed")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
