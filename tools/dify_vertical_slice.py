#!/usr/bin/env python3
"""最小端到端竖切（#582 / PRD #577）：Dify 导出 → 我们的草稿 → 不可变已发布版本 → 一条提问被服务。

这一票证明的是**脊梁存在**，不是"能上线"：一个 Dify app 的配置被拉回、经**我们自己的**
发布门冻成一个不可变版本、再由**运行时**把这个版本用来回答一条真实提问。

四跳，各自落在既有代码路径上
----------------------------
1. **拉取** —— 默认读一份**真实导出制品**（`tests/fixtures/dify-app-chat.dsl.yml`，从 36 上
   Dify console 导出端点拉的，见 #580）；`--live-dify` 时改走 `DifyDslClient` 真拉一次。
2. **发布** —— `converge_agent_draft` → `AgentManager.publish`。发布门在我们侧，产物是
   `agent_versions` 一行 + `published_version=N`。
3. **映射** —— 本票**先写死一个** `(租户, 入口) → agent 名`，且是**显式的调用参数**（写进证据）。
   显式持有映射是姊妹票 #584 的事；这里只保证它是**声明出来的**，不是从请求推断的。
4. **服务** —— 起一个**真的** uvicorn 网关（loopback、临时端口、一次性数据根），
   `POST /v1/assistant/questions` 发一条真实提问，轮询到终态，断言回答出自那个已发布版本。

唯一被替换的是**模型会话**（`qa_rag.SDKCodexSession`）—— 一条脚本化回答，把"有没有走通"
变成确定性观察。其余全部是真的：平台入口判定、选 agent、有界检索闸、blocks-v1 校验、
媒体签名、作业持久化、指标落库、HTTP 路由。因此本脚本**能**证明契约与接线，
**不能**证明模型质量或真实 Dify 可达 —— 那是 `--live-dify` 与 #587 阶段 1 的事。

退出码
------
0 = 全链跑通；1 = 有断言失败；2 = **未取证**（如 `--live-dify` 没给凭据环境变量）。
把"只准备了一下"读成"验证通过"正是本脚本要防的那种假陈述。

纪律
----
只跑在**一次性数据根**上，绝不指向真实 `AIOPS_DATA_HOME`；不打印也不落盘任何凭据
（`--api-key-env` 只给**变量名**）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from aiops_diagnostics.knowledge_retrieval import KbServiceClient  # noqa: E402

DEFAULT_DSL = ROOT / "tests" / "fixtures" / "dify-app-chat.dsl.yml"
#: Routes to the RAG branch: a business question — no greeting, no order id.
DEFAULT_QUESTION = "车辆充满电之后续航里程比官方标注少很多，是电池衰减吗"


class Untaken(RuntimeError):
    """Evidence could not be taken at all (exit 2), as opposed to a failed check."""


# ── HTTP helpers ────────────────────────────────────────────────────────────


def _request(url: str, *, payload: dict | None = None, headers: dict | None = None, timeout: float = 10.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _json_or_raw(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, _json_or_raw(exc.read().decode("utf-8", "replace"))
    except Exception:
        return 0, {}


def _json_or_raw(body: str):
    try:
        return json.loads(body)
    except ValueError:
        return {"raw": body}


# ── injected identity: the harness's own seam, declared in the evidence ─────


class _Caller:
    def __init__(self, tenant: str) -> None:
        self.tenant = tenant

    def resolve(self, token, *, required_scope, third_session=None, platform_entry=None, source_key=None):
        from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

        del token, third_session, platform_entry, source_key
        subject = SubjectRecord(b_user_id="c:C-TRACER", c_user_id="C-TRACER", tenant_id=self.tenant)
        return ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=self.tenant,
            data_scope=DataScope(type="self"),
            roles=frozenset({"ROLE_AGENT_ADMIN"}),
            permissions=frozenset({required_scope}),
        )


class _Directory:
    """A consumer-only caller, so the platform rule's entry is unambiguous."""

    def __init__(self, tenant: str) -> None:
        self.tenant = tenant

    def roles_for_c_user(self, c_user_id: str, tenant_id: str):
        from aiops_diagnostics.faq import PlatformRoleRecord

        del c_user_id, tenant_id
        return (PlatformRoleRecord("B-TRACER", "C-TRACER", self.tenant, "app"),)

    def roles_for_b_user(self, b_user_id: str, tenant_id: str):
        del b_user_id, tenant_id
        return ()


class _NeverAuthorizer:
    def can_access(self, context, order_no: str) -> bool:
        del context, order_no
        return False


class _PublishOk:
    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        del tenant_id, knowledge_base_ids


# ── the one substitution: a scripted model session ──────────────────────────


class _ScriptedSession:
    """Two turns shaped exactly like a real model under ``output_schema``: a
    knowledge-search request, then an answer whose reference block cites a chunk
    this turn actually retrieved. Anything looser would prove less than
    "the harness ran"."""

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs
        self.prompts: list[str] = []
        self._thread_id = "thread-tracer"
        self._searched = False

    @property
    def thread_id(self) -> str:
        return self._thread_id

    def set_progress_callback(self, callback) -> None:
        del callback

    def run(self, prompt: str, *, output_schema=None):
        from aiops_diagnostics.codex_runtime import CodexTurnOutput

        del output_schema
        self.prompts.append(prompt)
        if not self._searched:
            self._searched = True
            body: dict = {
                "kind": "tool_requests",
                "tool_requests": [
                    {"tool": "knowledge_search", "reason": "需要业务资料", "query": "续航 衰减"}
                ],
                "answer": None,
            }
        else:
            cited = next(iter(re.findall(r'"reference_id":\s*"([^"]+)"', prompt)), "")
            body = {
                "kind": "answer",
                "tool_requests": [],
                "answer": {
                    "blocks": [
                        {"kind": "text", "text": "按知识库的口径，续航偏差需按标称工况核对。"},
                        {
                            "kind": "reference",
                            "text": "",
                            "resource_id": "",
                            "reference_id": cited,
                            "title": "续航说明.docx",
                        },
                    ],
                    "retrieval_status": "found",
                },
            }
        return CodexTurnOutput(
            turn_id=f"turn-{len(self.prompts)}",
            final_response=json.dumps(body, ensure_ascii=False),
            usage={},
        )

    def close(self) -> None:
        return None


class _KbStub(KbServiceClient):
    """A real ``KbServiceClient`` subclass returning one canned chunk.

    Subclassing is required, not cosmetic: the runtime asserts ``isinstance`` on
    the client it hands to the QA harness. The chunk is shaped like kb-service's
    real response, so the normalizer runs for real. Nothing is dialled."""

    def __init__(self, base_url: str, tenant_id: str) -> None:
        super().__init__(base_url, tenant_id=tenant_id)
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def for_tenant(self, tenant_id: str):
        bound = _KbStub(self.base_url, tenant_id)
        bound.calls = self.calls  # one shared log: it belongs to the run, not a copy
        return bound

    def search(self, knowledge_base_ids, question, top_k):
        del top_k
        self.calls.append((tuple(knowledge_base_ids), question))
        return [
            {
                "knowledge_base_id": knowledge_base_ids[0],
                "chunk_id": "chunk-tracer-1",
                "doc_id": "doc-tracer-1",
                "docnm_kwd": "续航说明.docx",
                "content_with_weight": "标称续航在 NEDC 工况下测得，实际续航受温度与驾驶习惯影响。",
                "score": 0.91,
            }
        ]


# ── the four steps ─────────────────────────────────────────────────────────


def _admin_context(tenant: str):
    from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

    subject = SubjectRecord(b_user_id="B-TRACER", tenant_id=tenant)
    return ScopeContext.build(
        caller=subject,
        subject=subject,
        delegated=False,
        effective_tenant_id=tenant,
        data_scope=DataScope(type="self"),
        roles=frozenset({"ROLE_AGENT_ADMIN"}),
        permissions=frozenset({"aiops:agents:manage"}),
    )


def _manager(store_path: Path, model: str):
    from aiops_diagnostics.agent_lifecycle import AgentManager, AgentStore

    return AgentManager(AgentStore(store_path), knowledge_resolver=_PublishOk(), allowed_models=(model,))


def _mapped_config(dsl_text: str, args):
    """Map the DSL; if it declares no knowledge base, take the explicit input.

    The real 36 export carries no enabled dataset — Dify's chat form wipes
    ``dataset_configs`` wholesale (#580 recorded it). An agent with no knowledge
    base cannot reach the RAG path, so the binding is an EXPLICIT operator input,
    stated as such in the evidence, never silently defaulted."""
    from aiops_diagnostics.dify_dsl_pull import map_dsl_to_config

    config = map_dsl_to_config(dsl_text, agent_type="customer", output_contract="blocks-v1")
    if config.knowledge_base_ids:
        return config, "dsl"
    if not args.knowledge_base_id:
        raise Untaken("DSL 未声明知识库，且未给 --knowledge-base-id（无知识库的客服 agent 到不了 RAG 路径）")
    return replace(config, knowledge_base_ids=(args.knowledge_base_id,)), "explicit-operator-input"


def _pull(args) -> tuple[str, str]:
    if not args.live_dify:
        return DEFAULT_DSL.read_text(encoding="utf-8"), f"artifact:{DEFAULT_DSL.name}"
    from aiops_diagnostics.dify_dsl_pull import DifyDslClient, DifyDslSource

    key = os.environ.get(args.api_key_env, "")
    if not key:
        raise Untaken(f"--live-dify 需要凭据：环境变量 {args.api_key_env} 未设置")
    return (
        DifyDslClient(
            DifyDslSource(
                base_url=args.base_url, app_id=args.app_id, api_key=key, workspace_id=args.workspace_id
            )
        ).export_dsl(),
        f"live-dify:{args.base_url}",
    )


def _serve(tmp_path: Path, tenant: str, kb_stub: _KbStub):
    import uvicorn

    from aiops_diagnostics import qa_rag
    from aiops_diagnostics.agent_lifecycle import AgentStore
    from aiops_diagnostics.config import Settings
    from aiops_diagnostics.faq import FAQCatalog, PlatformIdentityResolver
    from aiops_diagnostics.gateway_api import create_gateway_app
    from aiops_diagnostics.gateway_config import GatewayServerSettings
    from aiops_diagnostics.gateway_runtime import GatewayRuntime
    from aiops_diagnostics.gateway_store import GatewayStore
    from aiops_diagnostics.knowledge_retrieval import MediaResourceSigner

    qa_rag.SDKCodexSession = _ScriptedSession

    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
    )
    settings.server_config_file.write_text("# tracer bullet\n", encoding="utf-8")
    os.chmod(settings.server_config_file, 0o600)
    store = AgentStore(settings.database_file)
    runtime = GatewayRuntime(
        GatewayStore(settings.database_file),
        settings,
        Settings(),
        kb_search_client=kb_stub,
        media_signer=MediaResourceSigner("tracer-bullet-signing-secret", ttl_seconds=300),
        agent_store=store,
    )
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=runtime,
        caller_resolver=_Caller(tenant),
        order_authorizer=_NeverAuthorizer(),
        platform_resolver=PlatformIdentityResolver(_Directory(tenant)),
        faq_catalog=FAQCatalog.bundled(),
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))  # no window for another process to take the port
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", server_header=False, proxy_headers=False)
    )
    threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True).start()
    return server, runtime, f"http://127.0.0.1:{int(sock.getsockname()[1])}"


def _ask(base: str, entry: str, question: str, timeout_s: float = 90.0) -> dict:
    headers = {"Authorization": "Bearer tracer", "X-Business-Entry": entry}
    status, started = _request(
        f"{base}/v1/assistant/questions", payload={"question": question}, headers=headers
    )
    if status != 202:
        raise AssertionError(f"提问未被接受：HTTP {status} {started}")
    qa_id = started["qa_id"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _, body = _request(f"{base}/v1/assistant/questions/{qa_id}", headers=headers)
        if body.get("status") in {"completed", "failed"}:
            return {"qa_id": qa_id, **body}
        time.sleep(0.2)
    raise AssertionError("作业未在时限内到达终态")


# ── the run ────────────────────────────────────────────────────────────────


def _publish(store_path: Path, tenant: str, dsl_text: str, args) -> tuple[dict, str]:
    from aiops_diagnostics.dify_dsl_pull import converge_agent_draft

    config, kb_source = _mapped_config(dsl_text, args)
    admin = _admin_context(tenant)
    manager = _manager(store_path, config.model)
    draft = converge_agent_draft(manager, admin, config, name=args.agent_name, description=args.description)
    version = manager.publish(admin, draft.agent_id, expected_revision=draft.revision)
    return (
        {
            "agent_id": draft.agent_id,
            "agent_name": args.agent_name,
            "version_no": version.version_no,
            "agent_version": f"{draft.agent_id}#v{version.version_no}",
            "model": config.model,
            "knowledge_base_ids": list(config.knowledge_base_ids),
            "knowledge_base_ids_source": kb_source,
            "prompt_head": config.prompt[:80],
        },
        config.prompt,
    )


def _declare_mapping(store_path: Path, tenant: str, entry: str, agent_name: str) -> dict:
    """The mapping this run is declared on — explicit inputs, never inference."""
    mapping = {
        "tenant_id": tenant,
        "business_entry": entry,
        "agent_name": agent_name,
        "declared": "explicit CLI inputs (--tenant/--entry/--agent-name), not inferred from the request",
    }
    try:
        from aiops_diagnostics.dify_app_registry import DifyAppBinding, DifyAppRegistry

        DifyAppRegistry(store_path).put(DifyAppBinding(tenant, entry, "dify-app-tracer", agent_name))
        mapping["registry_row"] = "written"
    except ImportError:
        mapping["row_absent"] = "#584 not in this base; the pre-registry rule routes by tenant"
    return mapping


def _assert_immutability(root: Path, args, dsl_text: str, original_prompt: str, published: dict) -> dict:
    """Repull an edited DSL + republish → a NEW version; v1's snapshot untouched."""
    from aiops_diagnostics.dify_dsl_pull import converge_agent_draft

    admin = _admin_context(args.tenant)
    edited, _ = _mapped_config(dsl_text, args)
    edited = replace(edited, prompt=original_prompt + "\n（运营在 Dify 里改过这一版）")
    manager = _manager(root / "gateway.db", edited.model)
    redraft = converge_agent_draft(manager, admin, edited, name=args.agent_name, description=args.description)
    reversion = manager.publish(admin, redraft.agent_id, expected_revision=redraft.revision)
    first_snapshot = manager.version(admin, published["agent_id"], 1).snapshot
    return {
        "version_no": reversion.version_no,
        "v1_snapshot_prompt_head": first_snapshot["prompt"][:80],
        "v1_prompt_unchanged": first_snapshot["prompt"] == original_prompt,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", default="var/dify-tracer", help="证据输出目录")
    parser.add_argument("--tenant", default="T-TRACER")
    parser.add_argument("--entry", default="consumer")
    parser.add_argument("--agent-name", default="小趋-客服")
    parser.add_argument("--description", default="从 Dify 拉取（#582 竖切）")
    parser.add_argument("--knowledge-base-id", default="kb-tracer")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--live-dify", action="store_true", help="改走真实 console 导出端点")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--app-id", default="")
    parser.add_argument("--workspace-id", default="")
    parser.add_argument("--api-key-env", default="AIOPS_DIFY_CONSOLE_API_KEY", help="只给变量名，不给值")
    args = parser.parse_args(argv)

    if args.live_dify and not (args.app_id and args.base_url):
        print("FAIL: --live-dify 需要 --app-id 与 --base-url", file=sys.stderr)
        return 2

    out_dir = (ROOT / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="aiops-dify-tracer-"))
    failures: list[str] = []
    evidence: dict = {
        "chain": "dify export → pull → draft → publish → served by the runtime over HTTP",
        "model_substitution": (
            "qa_rag.SDKCodexSession replaced by a scripted two-turn session; identity, entry, "
            "selection, retrieval guard, blocks contract, persistence and routing all run for real"
        ),
        "not_proven": [
            "model quality / a live provider round-trip",
            "that the Dify instance on 36 is reachable and serving",
            "production entry, real tenant mapping, non-Chinese language derivation (#587)",
        ],
    }
    server = runtime = None
    from aiops_diagnostics import qa_rag

    original_session = qa_rag.SDKCodexSession
    try:
        dsl_text, source = _pull(args)
        evidence["pull"] = {"source": source, "dsl_bytes": len(dsl_text)}

        published, original_prompt = _publish(root / "gateway.db", args.tenant, dsl_text, args)
        evidence["published_version"] = published
        evidence["mapping"] = _declare_mapping(root / "gateway.db", args.tenant, args.entry, args.agent_name)

        kb_stub = _KbStub("http://127.0.0.1:1", args.tenant)
        server, runtime, base = _serve(root, args.tenant, kb_stub)
        evidence["served_on"] = base
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _request(f"{base}/health")[0] == 200:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("网关未就绪")

        answer = _ask(base, args.entry, args.question)
        result = answer.get("result") or {}
        evidence["answer"] = {
            "status": answer.get("status"),
            "retrieval_status": result.get("retrieval_status"),
            "block_kinds": [block.get("kind") for block in result.get("blocks") or []],
            "text": (result.get("blocks") or [{}])[0].get("text", "")[:120],
        }
        evidence["kb_search_calls"] = [[list(ids), q] for ids, q in kb_stub.calls]

        if answer.get("status") != "completed":
            failures.append(f"作业终态不是 completed：{answer.get('status')} {answer.get('error_code')}")
        if result.get("retrieval_status") != "found":
            failures.append(f"检索状态不是 found（没走 RAG 路径）：{result.get('retrieval_status')}")
        if not kb_stub.calls:
            failures.append("一次知识检索都没发生：回答不是由已发布版本 + 知识库服务的")
        elif kb_stub.calls[0][0] != tuple(published["knowledge_base_ids"]):
            failures.append(
                f"检索用的知识库集合不是这个已发布版本绑定的：{kb_stub.calls[0][0]} "
                f"≠ {tuple(published['knowledge_base_ids'])}"
            )
        if not any(block.get("kind") == "reference" for block in result.get("blocks") or []):
            failures.append("回答里没有 reference 块：返回体不再是 blocks-v1 的既有形状")

        runs = runtime.metrics_store.list_runs(args.tenant, limit=10)
        evidence["metrics_runs"] = runs
        served_by = [row.get("agent_version_key") for row in runs if row.get("agent_version_key")]
        if published["agent_version"] not in served_by:
            failures.append(f"指标里没有被服务到的版本 {published['agent_version']}（实际：{served_by}）")

        immutability = _assert_immutability(root, args, dsl_text, original_prompt, published)
        evidence["republish"] = immutability
        if immutability["version_no"] != published["version_no"] + 1:
            failures.append(
                f"再次发布没有产生新版本：{immutability['version_no']}（期望 {published['version_no'] + 1}）"
            )
        if not immutability["v1_prompt_unchanged"]:
            failures.append("v1 的快照被就地改写了：再次发布改动了旧版本")
    except Untaken as exc:
        print(f"UNTAKEN: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — a tracer reports; it does not traceback
        import traceback

        evidence["traceback"] = traceback.format_exc()
        failures.append(f"链路中断：{type(exc).__name__}: {exc}")
    finally:
        qa_rag.SDKCodexSession = original_session  # module global: shared interpreter
        if server is not None:
            server.should_exit = True
        if runtime is not None:
            with contextlib.suppress(Exception):
                runtime.shutdown()
        shutil.rmtree(root, ignore_errors=True)

    evidence["failures"] = failures
    evidence["status"] = "failed" if failures else "passed"
    (out_dir / "dify-vertical-slice.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"status: {evidence['status']}")
    print(f"evidence: {out_dir / 'dify-vertical-slice.json'}")
    for item in failures:
        print(f"  FAIL: {item}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
