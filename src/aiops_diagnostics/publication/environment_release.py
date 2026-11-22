"""目标消费已核冻结内容；原Agent版本与route CAS仍是唯一发布真值。"""

import json
from pathlib import Path

from aiops_diagnostics.common.private_files import validate_private_file, write_private_text
from aiops_diagnostics.consumer.consumer_publication import bootstrap_config
from aiops_diagnostics.dify.dify_app_registry import DifyAppBinding
from aiops_diagnostics.dify.dify_dsl_pull import SyncOutcome, commit_workflow_snapshot
from aiops_diagnostics.dify.self_service_store import SelfServiceStore
from aiops_diagnostics.execution import node_publication
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore
from aiops_diagnostics.execution.node_capabilities import require_compatible
from aiops_diagnostics.knowledge.delegated_knowledge_client import for_application
from aiops_diagnostics.publication.agent_lifecycle import AgentConfig
from aiops_diagnostics.publication.agent_manifest import admin_context


def context_for(settings):
    """短命运维进程只装配发布依赖，不执行Gateway启动恢复或接管在飞作业。"""
    from types import SimpleNamespace

    from aiops_diagnostics.common.config import Settings
    from aiops_diagnostics.dify.self_service_config import approved_model_names
    from aiops_diagnostics.publication.agent_lifecycle import (
        AgentManager,
        AgentStore,
        allowed_models_from_settings,
    )

    settings.validate()
    models = (
        *allowed_models_from_settings(Settings.from_config(settings.server_config_file)),
        *approved_model_names(settings),
    )
    return SimpleNamespace(
        settings=settings,
        agent_manager=AgentManager(AgentStore(settings.database_file), allowed_models=models),
        runtime=SimpleNamespace(gateway_settings=settings),
    )


def frozen_consumer(manager, admin, binding, workspace, workflow, workflow_id, settings):
    config = AgentConfig(**bootstrap_config(settings["knowledge_base_ids"], settings)({"workflow": workflow}))
    manager._validate_config(config, enforce_model=True)
    agent = next((a for a in manager.list(admin) if a.name == binding.agent_name), None)
    if agent is None:
        agent = manager.create(admin, name=binding.agent_name, description="", config=config)
    if agent.status not in {"draft", "published"}:
        raise ValueError("publication agent disabled")
    previous = (
        manager.store.version(agent.agent_id, binding.tenant_id, agent.published_version).snapshot
        if agent.published_version
        else {"name": agent.name, "description": agent.description, **config.to_dict()}
    )
    snapshot = {
        "kind": "dify-workflow",
        "app_id": binding.app_id,
        "workspace_id": workspace,
        "workflow_id": workflow_id,
        "workflow": workflow,
        "consumer_service": settings,
        "knowledge_base_ids": settings["knowledge_base_ids"],
        "prompt": config.prompt,
        "model": config.model,
    }
    if all(previous.get(k) == v for k, v in snapshot.items()):
        return SyncOutcome(
            binding.tenant_id,
            "consumer",
            binding.app_id,
            binding.agent_name,
            "unchanged",
            agent.published_version,
            workflow_id=workflow_id,
            agent_id=agent.agent_id,
            route_scenario="consumer_service",
        )
    return commit_workflow_snapshot(manager, admin, binding, agent, previous, snapshot, workflow_id, True)


def import_versions(context, document):
    """仅导入版本；准入和指针由后面的明确动作推进。"""
    settings = context.settings
    if document["environment"] != settings.environment:
        raise ValueError("release environment mismatch")
    store = SelfServiceStore(settings.database_file)
    result = {}
    for entry in ("operator", "consumer"):
        app = document["apps"][entry]
        row = store.app(app["app_id"])
        if not row or (row["environment"], row["tenant_id"], row["workspace_id"], row["entry"]) != (
            document["environment"],
            document["tenant_id"],
            document["workspace_id"],
            entry,
        ):
            raise ValueError("release app not registered")
        binding = DifyAppBinding(row["tenant_id"], entry, row["app_id"], row["agent_name"])
        admin = admin_context(row["tenant_id"])
        if entry == "operator":
            target = node_publication.managed_target(
                settings, row["profile_id"], row["app_id"], row["tenant_id"]
            )
            manifest = json.loads(app["manifest"])
            require_compatible(settings.database_file, target, manifest)
            config = AgentConfig(
                "operations", app["prompt"], (), manifest["model"]["model_key"], "diagnosis-v1"
            )
            outcome = node_publication.publish_frozen(
                context.agent_manager, admin, binding, row["workspace_id"], app["manifest"], config
            )
        else:
            remote = for_application(context.runtime, row["tenant_id"], row["app_id"])
            if remote is None:
                raise ValueError("release requires delegated knowledge")
            from aiops_diagnostics.consumer.consumer_knowledge_selection import require_owned

            require_owned(app["settings"]["knowledge_base_ids"], remote.list_knowledge_bases())
            outcome = frozen_consumer(
                context.agent_manager,
                admin,
                binding,
                row["workspace_id"],
                app["workflow"],
                app["workflow_id"],
                app["settings"],
            )
        result[entry] = {
            "agent_id": outcome.agent_id,
            "version_no": outcome.version_no,
            "app_id": outcome.app_id,
            "workflow_id": outcome.workflow_id or "",
            "binding": {
                "environment": document["environment"],
                "tenant_id": document["tenant_id"],
                "entry": entry,
                "scenario": "operator_order_diagnosis" if entry == "operator" else "consumer_service",
                "engine": "node" if entry == "operator" else "dify",
                "agent_id": outcome.agent_id,
                "agent_version_no": outcome.version_no,
                "app_id": outcome.app_id,
                "workflow_id": outcome.workflow_id or "",
                "workflow_key_ref": str(Path(settings.chat_dify_key_dir) / (outcome.app_id + ".key"))
                if entry == "consumer"
                else "",
                "node_config_file": target.config_file if entry == "operator" else "",
                "state": "active",
            },
        }
    return result


def exact_route(route, expected, revision):
    return (
        route is not None
        and route.revision == revision
        and all(getattr(route, key) == value for key, value in expected.items())
    )


def advance(context, document, versions, expected):
    from aiops_diagnostics.gateway.gateway_api import _advance_route_binding

    routes = ChatRouteBindingStore(context.settings.chat_route_bindings_file)
    result = {}
    for entry, version in versions.items():
        row = SelfServiceStore(context.settings.database_file).app(version["app_id"])
        scenario = "operator_order_diagnosis" if entry == "operator" else "consumer_service"
        current = routes.get(document["environment"], document["tenant_id"], entry, scenario)
        if exact_route(current, version["binding"], expected[entry] + 1):
            result[entry] = current.revision
            continue
        if (current.revision if current else 0) != expected[entry]:
            raise ValueError("publication route changed")
        outcome = SyncOutcome(
            document["tenant_id"],
            entry,
            row["app_id"],
            row["agent_name"],
            "synced",
            version["version_no"],
            workflow_id=version["workflow_id"],
            agent_id=version["agent_id"],
            route_scenario=scenario,
        )
        binding = DifyAppBinding(document["tenant_id"], entry, row["app_id"], row["agent_name"])
        if entry == "operator":
            target = node_publication.managed_target(
                context.settings, row["profile_id"], row["app_id"], row["tenant_id"]
            )
            if target.config_file != version["binding"]["node_config_file"]:
                raise ValueError("publication node configuration changed")
            require_compatible(
                context.settings.database_file, target, json.loads(document["apps"]["operator"]["manifest"])
            )
            node_publication.advance(context, outcome, binding, target, expected[entry], register_source=True)
        else:
            if (
                str(Path(context.settings.chat_dify_key_dir) / (row["app_id"] + ".key"))
                != version["binding"]["workflow_key_ref"]
            ):
                raise ValueError("publication key reference changed")
            _advance_route_binding(
                context,
                outcome,
                binding,
                initiator=(document["actor"], "", ""),
                expected_revision=expected[entry],
            )
        observed = routes.get(document["environment"], document["tenant_id"], entry, scenario)
        if not exact_route(observed, version["binding"], expected[entry] + 1):
            raise ValueError("publication route changed")
        result[entry] = observed.revision
    return result


def record_path(settings, identifier):
    from aiops_diagnostics.gateway.environment_drafts import REFERENCE_PATTERN

    if not isinstance(identifier, str) or not REFERENCE_PATTERN.fullmatch(identifier):
        raise ValueError("invalid publication operation")
    return Path(settings.data_home) / "environment-publications" / (identifier + ".json")


def read_record(settings, identifier):
    path = record_path(settings, identifier)
    validate_private_file(path)
    return json.loads(path.read_text())


def save_record(settings, identifier, value):
    write_private_text(record_path(settings, identifier), json.dumps(value, sort_keys=True) + "\n")


def restore(context, document, previous, expected):
    """恢复是新的route修订；不恢复数据库备份或撤销后的知识能力。"""
    from aiops_diagnostics.gateway.gateway_api import _advance_route_binding

    routes = ChatRouteBindingStore(context.settings.chat_route_bindings_file)
    outcomes = {}
    for entry, old in previous.items():
        scenario = "operator_order_diagnosis" if entry == "operator" else "consumer_service"
        current = routes.get(document["environment"], document["tenant_id"], entry, scenario)
        if current is None:
            outcomes[entry] = 0
            continue
        if current.revision != expected[entry]:
            raise ValueError("restore route changed")
        if not old or old["state"] != "active":
            outcomes[entry] = routes.retract(
                environment=current.environment,
                tenant_id=current.tenant_id,
                entry=entry,
                scenario=scenario,
                expected_revision=current.revision,
                updated_by=document["actor"],
            ).revision
            continue
        snapshot = context.agent_manager.store.version(
            old["agent_id"], document["tenant_id"], old["agent_version_no"]
        ).snapshot
        row = SelfServiceStore(context.settings.database_file).app(old["app_id"])
        if (
            not row
            or snapshot.get("app_id") != old["app_id"]
            or snapshot.get("workspace_id") != row["workspace_id"]
        ):
            raise ValueError("restore publication identity changed")
        binding = DifyAppBinding(document["tenant_id"], entry, row["app_id"], row["agent_name"])
        outcome = SyncOutcome(
            document["tenant_id"],
            entry,
            row["app_id"],
            row["agent_name"],
            "synced",
            old["agent_version_no"],
            workflow_id=old.get("workflow_id", ""),
            agent_id=old["agent_id"],
            route_scenario=scenario,
        )
        if entry == "operator":
            target = node_publication.managed_target(
                context.settings, row["profile_id"], row["app_id"], row["tenant_id"]
            )
            require_compatible(context.settings.database_file, target, json.loads(snapshot["node_manifest"]))
            node_publication.advance(
                context, outcome, binding, target, current.revision, register_source=True
            )
        else:
            remote = for_application(context.runtime, row["tenant_id"], row["app_id"])
            if remote is None:
                raise ValueError("restore knowledge authority missing")
            from aiops_diagnostics.consumer.consumer_knowledge_selection import require_owned

            require_owned(snapshot["consumer_service"]["knowledge_base_ids"], remote.list_knowledge_bases())
            _advance_route_binding(
                context,
                outcome,
                binding,
                initiator=(document["actor"], "", ""),
                expected_revision=current.revision,
            )
        outcomes[entry] = routes.get(document["environment"], document["tenant_id"], entry, scenario).revision
    return outcomes


def operation(context, document, action):
    """受管本机入口；版本导入/指针对账的意图与原账本同数据根。"""
    from dataclasses import asdict

    from aiops_diagnostics.gateway.environment_registry import load

    if (
        document.get("schema") != "aiops-environment-release/1"
        or document["environment"] != context.settings.environment
    ):
        raise ValueError("release scope mismatch")
    identifier = document["id"]
    path = record_path(context.settings, identifier)
    import hashlib

    digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
    if path.exists():
        state = read_record(context.settings, identifier)
        if state["digest"] != digest:
            raise ValueError("release operation changed")
    else:
        if action != "prepare":
            raise ValueError("release intent missing")
        routes = ChatRouteBindingStore(context.settings.chat_route_bindings_file)
        previous = {}
        for entry, scenario in [("operator", "operator_order_diagnosis"), ("consumer", "consumer_service")]:
            row = routes.get(document["environment"], document["tenant_id"], entry, scenario)
            previous[entry] = asdict(row) if row else None
        state = {"digest": digest, "previous": previous, "versions": {}, "routes": {}, "phase": "preparing"}
        save_record(context.settings, identifier, state)
    if action == "observe":
        routes = ChatRouteBindingStore(context.settings.chat_route_bindings_file)
        if state["phase"] == "published":
            for entry, desired in state["versions"].items():
                scenario = "operator_order_diagnosis" if entry == "operator" else "consumer_service"
                route = routes.get(document["environment"], document["tenant_id"], entry, scenario)
                if not exact_route(route, desired["binding"], state["routes"][entry]):
                    raise ValueError("publication routes changed")
        return state
    if action == "prepare":
        current = load(context.settings.environment_registry_file)
        row = next(r for r in current["environments"] if r["id"] == document["environment"])
        if any(t["id"] == document["tenant_id"] and t["enabled"] for t in row["tenants"]):
            raise ValueError("first publication requires disabled target tenant")
        if state["phase"] == "preparing":
            state["versions"] = import_versions(context, document)
            state["phase"] = "prepared"
            save_record(context.settings, identifier, state)
        return state
    if action == "publish":
        if state["phase"] not in {"prepared", "publishing", "published"}:
            raise ValueError("release not prepared")
        state["phase"] = "publishing"
        save_record(context.settings, identifier, state)
        for entry in ("operator", "consumer"):
            version = state["versions"][entry]
            original = state["previous"][entry]
            expected = original["revision"] if original else 0
            # 每端原CAS成功后即记账；响应丢失只接受本次预期的下一revision。
            changed = advance(context, document, {entry: version}, {entry: expected})
            state["routes"].update(changed)
            save_record(context.settings, identifier, state)
        state["phase"] = "published"
        save_record(context.settings, identifier, state)
        return state
    if action == "restore":
        routes = ChatRouteBindingStore(context.settings.chat_route_bindings_file)
        state.setdefault("restoring", {})
        state.setdefault("restored_routes", {})
        for entry in ("operator", "consumer"):
            original = state["previous"][entry]
            scenario = "operator_order_diagnosis" if entry == "operator" else "consumer_service"
            current = routes.get(document["environment"], document["tenant_id"], entry, scenario)
            row = asdict(current) if current else None
            intent = state["restoring"].get(entry)
            if intent is None:
                expected = original["revision"] if original else 0
                if row == original:
                    # 此端尚未写入；记录原样，不把外部后续写入当成可恢复对象。
                    intent = {"before": row, "changed": False}
                else:
                    desired = state["versions"].get(entry, {})
                    if not exact_route(current, desired.get("binding", {}), expected + 1):
                        raise ValueError("restore route changed")
                    intent = {"before": row, "changed": True}
                state["restoring"][entry] = intent
                save_record(context.settings, identifier, state)
            before = intent["before"]
            if not intent["changed"]:
                if row != before:
                    raise ValueError("restore route changed")
                revision = current.revision if current else 0
            elif row == before:
                restored = restore(context, document, {entry: original}, {entry: before["revision"]})
                revision = restored[entry]
            elif (
                current
                and current.revision == before["revision"] + 1
                and restored_binding(row, original, before)
            ):
                revision = current.revision
            else:
                raise ValueError("restore route changed")
            state["restored_routes"][entry] = revision
            save_record(context.settings, identifier, state)
        state["phase"] = "restored"
        save_record(context.settings, identifier, state)
        return state
    raise ValueError("unknown publication action")


def restored_binding(current, original, before):
    """只对账本次CAS的一步结果；同内容的后来写入也不能被认领。"""
    reference = original if original and original["state"] == "active" else {**before, "state": "retracted"}
    return all(
        current[k] == reference[k]
        for k in (
            "environment",
            "tenant_id",
            "entry",
            "scenario",
            "engine",
            "agent_id",
            "agent_version_no",
            "app_id",
            "workflow_id",
            "workflow_key_ref",
            "node_config_file",
            "state",
        )
    )
