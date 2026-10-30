#!/usr/bin/env python3
"""Prove #925 + #1000 over real HTTP on a throwaway data root.

The repo requires this shape of evidence for anything that changes the gateway's
outward behaviour — `TestClient` calls are unit tests, and the skill says so
explicitly. What this proves and what it does not:

| proves | does not prove |
|---|---|
| the routes exist and answer over a real socket | that a real RAGFlow tenant has libraries |
| a manual binding retrieves without an upstream call | browser behaviour of the Dify picker |
| `tenant-<id>` resolves only for onboarded tenants | production KB liveness |
| `tenant-<id>:<kb>` reaches exactly that library | that Dify has a dataset pointing at it |

A **stub kb-service** runs on its own port for the second half. An unreachable
upstream can only prove the 404 path; the point of #1000 is that the right
library is reached, and that needs an upstream that answers. The stub tags every
chunk with the library that was asked for, so the record itself says which one.

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
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DIFY_KEY = "verify-dify-key-0123456789"
PORT = 8791
#: Stub kb-service. On a **different** port so the gateway's own listener and the
#: upstream it talks to can never be confused for one another.
KB_PORT = 8792
#: What the stub tenant owns. Two libraries so "one id per library" has something
#: to distinguish — with one library, merging and isolating look identical.
KB_LIBRARIES = ("KB-A", "KB-B")


class _KbStub(BaseHTTPRequestHandler):
    """Just enough of kb-service for the two calls this feature makes.

    ``GET /kb/knowledge-bases`` → what this tenant owns (tenant header scopes it).
    ``POST /kb/knowledge-bases/<id>/search`` → one chunk, tagged with the library
    that was asked for — so the gateway's answer proves **which** library was hit.
    """

    def log_message(self, *_args: object) -> None:  # quieter than the default
        return

    def _send(self, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if self.path == "/kb/knowledge-bases":
            # Only the tenant that really has libraries gets them. Every other
            # tenant reads as "onboarded, no libraries" — which is exactly the
            # "upstream has nothing for you" leg the earlier checks need.
            tenant = self.headers.get("tenant-id", "")
            if tenant == "T-DOWN":
                self.send_error(503)
                return
            libraries = KB_LIBRARIES if tenant == "T-LIB" else ()
            self._send(
                {"data": [{"id": kb, "name": kb, "document_count": 1, "chunk_count": 1} for kb in libraries]}
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        prefix, _, rest = self.path.partition("/kb/knowledge-bases/")
        kb_id = rest.partition("/")[0] if prefix == "" else ""
        if not kb_id:
            self.send_error(404)
            return
        self._send(
            {
                "data": {
                    "chunks": [
                        {
                            "chunk_id": f"{kb_id}-c1",
                            "doc_id": f"{kb_id}-doc",
                            "content_with_weight": f"{kb_id} 的处理步骤",
                            "similarity": 0.9,
                            "docnm_kwd": kb_id,
                        }
                    ]
                }
            }
        )


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
    """Seed the throwaway store: three onboarded tenants, one manual binding.

    ``T-AUTO`` has no libraries upstream, ``T-DOWN`` errors, and ``T-LIB`` is the
    one that owns :data:`KB_LIBRARIES` — three different reasons a derived id can
    fail to resolve, kept apart so the checks below can tell them from a bug.
    """
    sys.path.insert(0, str(repo_root() / "src"))
    from aiops_diagnostics.gateway.gateway_store import GatewayStore

    store = GatewayStore(root / "data" / "gateway.db")
    for tenant_id in ("T-AUTO", "T-LIB", "T-DOWN"):
        store.record_ragflow_tenant(
            tenant_id,
            "verify",
            ragflow_email=f"{tenant_id.lower()}@kb.internal",
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
        # The knobs this feature reads. The KB URL points at the stub below: this
        # script has to prove both halves — that a derived id resolves when the
        # upstream answers, and that it degrades to "no auto source" when it does
        # not (that second half is served by a tenant the stub fails on purpose).
        "AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY": DIFY_KEY,
        "AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS": "env-one:T-ENV:KB-ENV",
        "AIOPS_GATEWAY_KB_SERVICE_BASE_URL": f"http://127.0.0.1:{KB_PORT}",
        "AIOPS_GATEWAY_KB_SERVICE_TIMEOUT_SECONDS": "5",
        "AIOPS_GATEWAY_KB_SEARCH_MAX_RESULTS": "5",
        # Without a signing secret the runtime has no media plane, and every
        # retrieval answers 502 regardless of which source resolved — the run
        # would "prove" only that the process was misconfigured.
        "AIOPS_GATEWAY_MEDIA_SIGNING_SECRET": "verify-media-secret-0123456789",
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
    stub = ThreadingHTTPServer(("127.0.0.1", KB_PORT), _KbStub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    print(f"kb stub  : http://127.0.0.1:{KB_PORT}  libraries={KB_LIBRARIES}")
    config = root / "config" / "production.env"
    config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config.write_text("# isolated verification\n", encoding="utf-8")
    config.chmod(0o600)
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
            "onboarded tenant the upstream says has no libraries is not a servable source (404)",
            status == 404,
            {"status": status, "body": text[:120]},
        )

        body["knowledge_id"] = "tenant-T-DOWN"
        status, text = http(f"{base}/v1/dify/retrieval", method="POST", payload=body)
        check(
            "upstream failing is 'no auto source' (404), never a 502",
            status == 404,
            {"status": status, "body": text[:120]},
        )

        body["knowledge_id"] = "tenant-T-NEVER"
        status, _ = http(f"{base}/v1/dify/retrieval", method="POST", payload=body)
        check("never-onboarded tenant -> 404", status == 404, status)

        # #1000: one id per library, resolved against a live upstream. The chunk
        # the stub returns is tagged with the library that was asked for, so the
        # record proves **which** library this id reached — not merely that
        # *something* answered.
        for kb_id in KB_LIBRARIES:
            body["knowledge_id"] = f"tenant-T-LIB:{kb_id}"
            status, text = http(f"{base}/v1/dify/retrieval", method="POST", payload=body)
            records = json.loads(text).get("records", []) if status == 200 else []
            check(
                f"per-library id {body['knowledge_id']} reaches exactly its own library",
                status == 200 and [r["content"] for r in records] == [f"{kb_id} 的处理步骤"],
                {"status": status, "records": [r["content"] for r in records]},
            )

        body["knowledge_id"] = "tenant-T-LIB"
        status, text = http(f"{base}/v1/dify/retrieval", method="POST", payload=body)
        merged = sorted(r["content"] for r in json.loads(text).get("records", [])) if status == 200 else []
        check(
            "the tenant-level id still merges every library (#925 unchanged)",
            status == 200 and merged == [f"{kb} 的处理步骤" for kb in KB_LIBRARIES],
            {"status": status, "records": merged},
        )

        body["knowledge_id"] = "tenant-T-LIB:KB-NOT-MINE"
        status, _ = http(f"{base}/v1/dify/retrieval", method="POST", payload=body)
        check("a library this tenant does not own -> 404", status == 404, status)

        body["knowledge_id"] = "tenant-T-AUTO:KB-A"
        status, _ = http(f"{base}/v1/dify/retrieval", method="POST", payload=body)
        check("another tenant's library id cannot be borrowed", status == 404, status)

        body["knowledge_id"] = "tenant-T-LIB:"
        status, _ = http(f"{base}/v1/dify/retrieval", method="POST", payload=body)
        check("empty dataset segment -> 404, not 'every library'", status == 404, status)

        status, text = http(f"{base}/v1/dify/knowledge")
        ids = sorted(i["knowledge_id"] for i in json.loads(text).get("knowledge_items", []))
        check(
            "picker lists manual + env sources", status == 200 and {"manual-one", "env-one"} <= set(ids), ids
        )
        check(
            "picker offers one entry per library of the tenant that has them",
            {"tenant-T-LIB", *(f"tenant-T-LIB:{kb}" for kb in KB_LIBRARIES)} <= set(ids),
            ids,
        )
        check(
            "picker offers nothing for a tenant whose upstream has no libraries",
            not any(i.startswith(("tenant-T-AUTO", "tenant-T-DOWN")) for i in ids),
            ids,
        )

        status, _ = http(f"{base}/v1/dify/knowledge".replace("/v1/dify/knowledge", "/v1/dify/knowledge"))
        check("unauthenticated is refused", http(f"{base}/v1/runs")[0] == 401, "control route")
    finally:
        stub.shutdown()
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
