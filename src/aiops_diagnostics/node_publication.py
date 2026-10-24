"""将已登记节点的 SOP 发布为不可变执行清单，复用既有版本和正式路由。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from aiops_diagnostics import execution_manifest
from aiops_diagnostics.agent_lifecycle import config_from_snapshot
from aiops_diagnostics.agent_runs import AgentRunConfig
from aiops_diagnostics.chat_route_bindings import ChatRouteBindingStore, RouteBindingError
from aiops_diagnostics.dify_dsl_pull import DifyDslClient, SyncOutcome
from aiops_diagnostics.order_visibility import (
    TenantVisibility,
    VisibilityProfile,
    normalize_tenant,
    visible_orders,
)
from aiops_diagnostics.private_files import validate_private_file


class NodeRouteConflict(ValueError):
    """正式指针已变化，必须保留当前已发布状态。"""


@dataclass(frozen=True)
class NodeTarget:
    app_id: str
    tenant_id: str
    environment: str
    config_file: str
    manifest_file: str


def targets(path: str) -> tuple[NodeTarget, ...]:
    if not path:
        return ()
    declared = Path(path).expanduser().absolute()
    validate_private_file(declared)
    body = json.loads(declared.read_text())
    if body.get("schema") != "aiops-node-targets/1":
        raise ValueError("unsupported node target schema")
    result = tuple(NodeTarget(**row) for row in body["targets"])
    if len({(r.environment, r.tenant_id, r.app_id) for r in result}) != len(result):
        raise ValueError("duplicate node target")
    if any(
        not all(
            isinstance(value, str) and value.strip()
            for value in (row.app_id, row.tenant_id, row.environment, row.config_file, row.manifest_file)
        )
        for row in result
    ):
        raise ValueError("node target fields must be nonempty strings")
    for row in result:
        if AgentRunConfig.load(Path(row.config_file)).environment != row.environment:
            raise ValueError("node target environment mismatch")
        if execution_manifest.load(row.manifest_file) is None:
            raise ValueError("node target manifest missing")
    return result


def target_for(settings: Any, binding: Any) -> NodeTarget | None:
    return next(
        (
            r
            for r in targets(settings.chat_node_targets_file)
            if r.app_id == binding.app_id
            and visible_orders(
                [{"tenant_id": r.tenant_id}],
                TenantVisibility(
                    profile=VisibilityProfile.CALLER, allowed=frozenset({normalize_tenant(binding.tenant_id)})
                ),
            ).rows
            and r.environment == settings.execution_environment
        ),
        None,
    )


def candidate(source: Any, target: NodeTarget) -> str:
    """首片节点 SOP 只取 chat 应用的 pre_prompt；执行工具/模型由可信模板限定。"""
    document = yaml.safe_load(DifyDslClient(source).export_dsl())
    if document.get("app", {}).get("mode") not in {"chat", "agent-chat", "completion"}:
        raise ValueError("node SOP requires a chat/completion editor, not a workflow graph")
    prompt = document.get("model_config", {}).get("pre_prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
        raise ValueError("node SOP prompt missing or too large")
    validate_private_file(Path(target.manifest_file))
    raw_text = Path(target.manifest_file).read_text()
    execution_manifest.from_text(raw_text)
    raw = json.loads(raw_text)
    body = {key: raw[key] for key in ("schema", "name", "history_policy")}
    body["model_options"] = {
        key: value
        for key, value in raw["model_options"].items()
        if key
        in {
            "temperature",
            "max_tokens",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
            "seed",
            "stop_sequences",
        }
    }
    body["tools"] = [
        {key: tool[key] for key in ("name", "description", "input_schema")} for tool in raw["tools"]
    ]
    body["model"] = {
        key: raw["model"][key] for key in ("provider_key", "adapter_key", "model_key", "endpoint")
    }
    body["system_messages"] = [prompt]
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    execution_manifest.from_text(text)
    return text


def publish(manager: Any, admin: Any, binding: Any, source: Any, target: NodeTarget) -> SyncOutcome:
    agent = next((a for a in manager.list(admin) if a.name == binding.agent_name), None)
    if agent is None or agent.status != "published" or agent.published_version is None:
        raise ValueError("node publication requires an existing published agent")
    previous = manager.store.version(agent.agent_id, binding.tenant_id, agent.published_version).snapshot
    text = candidate(source, target)
    if (
        previous.get("node_manifest") == text
        and previous.get("app_id") == binding.app_id
        and previous.get("workspace_id") == source.workspace_id
    ):
        return SyncOutcome(
            binding.tenant_id,
            binding.business_entry,
            binding.app_id,
            binding.agent_name,
            "unchanged",
            agent.published_version,
            agent_id=agent.agent_id,
        )
    snapshot = {
        **previous,
        "kind": "node-sop",
        "node_manifest": text,
        "app_id": binding.app_id,
        "workspace_id": source.workspace_id,
    }
    # 既有 AgentConfig 继续服务其他入口；节点 SOP 独立保存在同一不可变版本中。
    config_from_snapshot(snapshot)
    draft = manager.fork_draft(admin, agent.agent_id, expected_revision=agent.revision)
    version = manager.store.publish(
        agent.agent_id, binding.tenant_id, draft.revision, snapshot, published_by="node-sop-publisher"
    )
    return SyncOutcome(
        binding.tenant_id,
        binding.business_entry,
        binding.app_id,
        binding.agent_name,
        "synced",
        version.version_no,
        agent_id=agent.agent_id,
    )


def advance(context: Any, outcome: Any, binding: Any, target: NodeTarget, revision: int) -> None:
    if type(revision) is not int or revision < 0:
        raise NodeRouteConflict("node route revision missing")
    if AgentRunConfig.load(Path(target.config_file)).environment != target.environment:
        raise NodeRouteConflict("node target environment changed")
    store = ChatRouteBindingStore(Path(context.settings.chat_route_bindings_file))
    previous = store.get(target.environment, binding.tenant_id, "consumer", "consumer_own_order_status")
    if previous and previous.revision != revision:
        raise NodeRouteConflict("node route revision changed")
    if (
        outcome.action == "unchanged"
        and previous
        and previous.active
        and previous.agent_id == outcome.agent_id
        and previous.agent_version_no == outcome.version_no
        and previous.app_id == binding.app_id
        and previous.engine == "node"
        and previous.node_config_file == target.config_file
    ):
        return
    try:
        store.publish(
            environment=target.environment,
            tenant_id=binding.tenant_id,
            entry="consumer",
            scenario="consumer_own_order_status",
            engine="node",
            agent_id=outcome.agent_id,
            agent_version_no=outcome.version_no,
            app_id=binding.app_id,
            node_config_file=target.config_file,
            expected_revision=revision,
        )
    except RouteBindingError as exc:
        raise NodeRouteConflict(str(exc)) from exc


@dataclass(frozen=True)
class NodeTrial:
    manifest_text: str
    route: Any
    engine: str = "node"

    def fingerprint(self) -> str:
        return execution_manifest.from_text(self.manifest_text).identity

    def execute(self, runtime: Any, trial_id: str, scope: Any, order: str, language: str):
        from dataclasses import replace

        from aiops_diagnostics import chat_execution
        from aiops_diagnostics.dify_trial import TrialResult

        binding = chat_execution.binding_for_key(runtime.gateway_settings, self.route.key)
        if binding is None or binding.route_revision != self.route.revision:
            raise ValueError("node trial route changed")
        runtime._require_chat_order_visible(scope, order)
        run, config = chat_execution.issue_for_store(
            store=runtime.agent_run_store,
            settings=runtime.gateway_settings,
            binding=replace(binding, manifest_text=self.manifest_text),
            caller=scope,
            order_no=order,
            gateway_version="node-trial",
        )
        runtime.agent_run_store.bind_qa(run.run_id, trial_id)
        try:
            chat_execution.dispatch_run(store=runtime.agent_run_store, config=config, run=run)
            chat_execution.settled_run(store=runtime.agent_run_store, run_id=run.run_id)
            runtime._require_chat_order_visible(scope, order)
            released = chat_execution.final_release(
                store=runtime.agent_run_store,
                settings=runtime.gateway_settings,
                caller=scope,
                run_id=run.run_id,
            )
            answer = chat_execution.answer_from_run(released, language=language)
            return TrialResult(
                "completed",
                {
                    "summary": answer["blocks"][0]["text"],
                    "node_run_id": run.run_id,
                    "manifest_sha256": self.fingerprint(),
                },
                (),
                (),
                (),
                language,
            )
        except Exception:
            runtime.agent_run_store.fail(run.run_id, "node_trial_failed")
            raise
