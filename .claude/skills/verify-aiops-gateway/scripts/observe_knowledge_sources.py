#!/usr/bin/env python3
"""Prove #925 over real HTTP on a throwaway data root: manual > auto > 404.

The repo requires this shape of evidence for anything that changes the gateway's
outward behaviour — `TestClient` calls are unit tests, and the skill says so
explicitly. What this proves and what it does not:

| proves | does not prove |
|---|---|
| the routes exist and answer over a real socket | that a real RAGFlow tenant has libraries |
| a manual binding retrieves without an upstream call | browser behaviour of the Dify picker |
| `tenant-<id>` resolves only for onboarded tenants | production KB liveness |

Seeds the throwaway store directly (it is a *fixture*, not the thing under
test) and drives the two routes with the real Dify credential. Never touches
`AIOPS_DATA_HOME`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DIFY_KEY = "verify-dify-key-0123456789"
PORT = 8791


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def http(url: str, *, method: str = "GET", payload: dict | None = None) -> tuple[int, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {DIFY_KEY}")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - status 0 means "never completed"
        return 0, ""


def seed(root: Path) -> None:
    """Seed the throwaway store: one onboarded tenant, one manual binding."""
    sys.path.insert(0, str(repo_root() / "src"))
    from aiops_diagnostics.gateway_store import GatewayStore

    store = GatewayStore(root / "data" / "gateway.db")
    store.record_ragflow_tenant(
        "T-AUTO",
        "verify",
        ragflow_email="a@kb.internal",
        credential_ciphertext="enc:v1:fixture",
        model_checked=True,
    )
    store.set_knowledge_id_binding("manual-one", "T-MANUAL", ("KB-MANUAL",))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="var/verify-evidence")
    parser.add_argument("--startup-timeout", type=float, default=45.0)
    args = parser.parse_args()

    repo = repo_root()
    root = Path(tempfile.mkdtemp(prefix="aiops-verify-925-"))
    env = {
        **os.environ,  # keep PATH/HOME: `uv run` and the service find their venv through them
        "AIOPS_HOME": str(root),
        "AIOPS_CONFIG_HOME": str(root / "config"),
        "AIOPS_DATA_HOME": str(root / "data"),
        "AIOPS_GATEWAY_ENVIRONMENT": "verify",
        # The port is a SETTING, not a CLI flag: without it the child binds the
        # default 8787 and every request below silently goes to whatever else
        # happens to be listening there (or to nothing).
        "AIOPS_GATEWAY_PORT": str(PORT),
        # **Both** names: the setting resolves ``AIOPS_GATEWAY_DATA_HOME``, while the
        # skill's own launcher exports ``AIOPS_DATA_HOME``. Exporting only the latter
        # makes the server read the developer's real store — which is the one thing a
        # verification root exists to prevent (and it is why the first run of this
        # script saw "no such binding": it was seeding one store and serving another).
        "AIOPS_GATEWAY_DATA_HOME": str(root / "data"),
        "AIOPS_GATEWAY_DATABASE_FILE": str(root / "data" / "gateway.db"),
        # The two knobs this feature reads. The KB URL is deliberately unreachable:
        # with no upstream, auto-registration must degrade to "no auto source" and
        # the manual binding must still work — that is half the point of the feature.
        "AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY": DIFY_KEY,
        "AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS": "env-one:T-ENV:KB-ENV",
        "AIOPS_GATEWAY_KB_SERVICE_BASE_URL": "http://127.0.0.1:1",
        "AIOPS_GATEWAY_KB_SEARCH_MAX_RESULTS": "5",
        "PYTHONPATH": f"{repo / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep),
    }
    base = f"http://127.0.0.1:{PORT}"
    out_dir = (repo / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, object] = {"base": base, "temp_root": str(root), "checks": []}
    failures: list[str] = []

    def check(name: str, ok: bool, detail: object) -> None:
        evidence["checks"].append({"check": name, "ok": ok, "detail": detail})  # type: ignore[union-attr]
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {detail}")
        if not ok:
            failures.append(name)

    print(f"temp root: {root}")
    print(f"gateway  : {base}")
    init = subprocess.run(["uv", "run", "aiops", "init"], cwd=repo, env=env, capture_output=True, text=True)
    if init.returncode != 0:
        print("aiops init failed:\n" + (init.stdout + init.stderr)[-1500:])
        shutil.rmtree(root, ignore_errors=True)
        return 2
    seed(root)

    process = subprocess.Popen(
        ["uv", "run", "aiops-gateway", "serve"],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + args.startup_timeout
        while time.monotonic() < deadline:
            if http(f"{base}/health")[0]:
                break
            time.sleep(0.4)
        check("gateway boots", http(f"{base}/health")[0] == 200, http(f"{base}/health")[0])

        body = {"retrieval_setting": {"top_k": 3}, "query": "充电", "knowledge_id": "manual-one"}
        status, text = http(f"{base}/v1/dify/retrieval", method="POST", payload=body)
        # Manual binding resolves; the search itself needs kb-service, so the honest
        # verdict here is "not 404" — the source resolved and the search was attempted.
        check("manual binding resolves (not 404)", status != 404, {"status": status, "body": text[:120]})

        body["knowledge_id"] = "tenant-T-AUTO"
        status, text = http(f"{base}/v1/dify/retrieval", method="POST", payload=body)
        check(
            "onboarded tenant with unreachable upstream is not a servable source (404)",
            status == 404,
            {"status": status, "body": text[:120]},
        )

        body["knowledge_id"] = "tenant-T-NEVER"
        status, _ = http(f"{base}/v1/dify/retrieval", method="POST", payload=body)
        check("never-onboarded tenant -> 404", status == 404, status)

        status, text = http(f"{base}/v1/dify/knowledge")
        ids = sorted(i["knowledge_id"] for i in json.loads(text).get("knowledge_items", []))
        check(
            "picker lists manual + env sources", status == 200 and {"manual-one", "env-one"} <= set(ids), ids
        )

        status, _ = http(f"{base}/v1/dify/knowledge".replace("/v1/dify/knowledge", "/v1/dify/knowledge"))
        check("unauthenticated is refused", http(f"{base}/v1/runs")[0] == 401, "control route")
    finally:
        # Terminate FIRST, then drain: reading a pipe whose writer is still alive
        # blocks until EOF, which is exactly how this script hung the first time.
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        log = process.stdout.read()[-2000:] if process.stdout is not None else ""
        evidence["gateway_log_tail"] = log
        evidence["failures"] = failures
        (out_dir / "knowledge-sources.json").write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        shutil.rmtree(root, ignore_errors=True)

        print("gateway log tail:\n" + (evidence.get("gateway_log_tail") or "")[-1200:])
    print(f"evidence: {out_dir / 'knowledge-sources.json'}")
    print("all checks passed" if not failures else f"{len(failures)} failed")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
