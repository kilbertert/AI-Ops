"""在临时 Chatflow 执行草稿，沿用源路由与试用身份，不推进正式版本。

主路径直接按现役 ``callback_version == 6`` 智能体执行；历史 1–5 版试运行兼容逻辑
已收拢至 :mod:`aiops_diagnostics.consumer.legacy_callbacks`。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, replace

from aiops_diagnostics.consumer import legacy_callbacks
from aiops_diagnostics.consumer.consumer_knowledge import _binding_identity
from aiops_diagnostics.dify.dify_chatflow import DifyChatflowClient
from aiops_diagnostics.dify.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
from aiops_diagnostics.dify.dify_dsl_pull import DifyDslClient, workflow_content
from aiops_diagnostics.dify.dify_run_store import DifyRunSpec, still_valid
from aiops_diagnostics.dify.dify_trial import TrialResult
from aiops_diagnostics.execution.agent_runs import scope_to_json
from aiops_diagnostics.execution.chat_data_grant import DataGrantStore
from aiops_diagnostics.execution.chat_execution import ChatBinding
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore


@dataclass
class ConsumerTrial:
    dsl: str
    source: object
    route: object
    content: dict
    question: str
    binding: ChatBinding | None = None
    draft: bool = False
    authorize: object = None
    candidate: object = None
    published: object = None

    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(
                {"graph": workflow_content(self.dsl), "content": self.content, "question": self.question},
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def require_current(self, settings):
        if self.candidate is not None:
            self.candidate.authorize()
            return
        if self.authorize:
            self.authorize()
        if self.draft:
            from aiops_diagnostics.dify.self_service_store import SelfServiceStore

            row = SelfServiceStore(settings.database_file).app(self.route.app_id)
            if (
                row is None
                or row["tenant_id"] != self.route.tenant_id
                or row["entry"] != "consumer"
                or row["environment"] != self.route.environment
            ):
                raise ValueError("trial draft is no longer managed")
            return
        row = ChatRouteBindingStore(settings.chat_route_bindings_file).by_key(self.route.key)
        if (
            row is None
            or not row.active
            or row.revision != self.route.revision
            or row.app_id != self.route.app_id
        ):
            raise ValueError("trial source route changed")

    def require_order(self, runtime, scope, order):
        if self.candidate is not None:
            self.candidate.require_order(order)
        else:
            runtime._require_chat_order_visible(scope, order)

    def execute(self, runtime, trial_id, scope, order, language):
        from aiops_diagnostics.gateway.gateway_runtime import JobRegistration

        admin = (
            DifyConsoleAdmin(ConsoleApiSettings(self.source.base_url, self.source.api_key))
            if self.published is None
            else None
        )
        grants = DataGrantStore(runtime.store.path)
        runs = runtime.dify_run_store
        app_id = ""
        run_id = "consumer_trial_" + secrets.token_hex(16)
        registration = JobRegistration(route_type="trial")
        runtime._qa_registrations[trial_id] = registration
        try:
            self.require_current(runtime.gateway_settings)
            if self.published is not None:
                app_id, workflow_id, key = self.published()
            else:
                if self.candidate is not None:
                    from uuid import UUID, uuid5

                    from aiops_diagnostics.dify.self_service_prepare import IMPORT_NAMESPACE

                    request_id = str(UUID(trial_id.removeprefix("tr_")))
                    app_id = str(uuid5(UUID(IMPORT_NAMESPACE), self.source.workspace_id + ":" + request_id))
                    created = admin._call(
                        self.source.workspace_id,
                        "/console/api/apps/imports",
                        method="POST",
                        body={
                            "mode": "yaml-content",
                            "yaml_content": self.dsl,
                            "name": "WF01 trial " + trial_id,
                            "request_id": request_id,
                        },
                    )
                    if created.get("app_id") != app_id or created.get("status") not in {
                        "completed",
                        "completed-with-warnings",
                    }:
                        raise ValueError("candidate temporary application unconfirmed")
                    admin._call(
                        self.source.workspace_id,
                        "/console/api/apps/" + app_id + "/site-enable",
                        method="POST",
                        body={"enable_site": False},
                    )
                else:
                    app_id = admin.import_app_dsl(
                        self.source.workspace_id, yaml_content=self.dsl, name="WF01 trial " + trial_id
                    )
                client = DifyDslClient(replace(self.source, app_id=app_id))
                if not client.publish_workflow():
                    raise ValueError("temporary Chatflow publish failed")
                workflow_id = client.published_workflow_id()
                if workflow_content(client.export_dsl(workflow_id=workflow_id)) != workflow_content(self.dsl):
                    raise ValueError("trial graph changed")
                key = admin.app_service_key(self.source.workspace_id, app_id)
            self.binding = ChatBinding(
                environment=self.route.environment,
                tenant_id=self.route.tenant_id,
                entry="consumer",
                scenario="consumer_service",
                engine="dify",
                app_id=app_id,
                workflow_id=workflow_id,
                agent_id=self.route.agent_id,
                agent_version_no=self.route.agent_version_no,
                route_revision=self.route.revision,
                content_snapshot=self.content["content"],
                knowledge_base_ids=tuple(self.content["knowledge_base_ids"]),
                callback_version=self.content.get("callback_version", 1),
                live_order_status=self.content["live_order_status"],
                live_billing_record=self.content["live_billing_record"],
            )
            is_agent_v6 = self.binding.callback_version == 6
            if is_agent_v6:
                kind, live = "agent", bool(order)
                if order:
                    self.require_order(runtime, scope, order)
            else:
                kind, live = legacy_callbacks.prepare_legacy_trial(
                    runtime, scope, self.binding, self.question, order, language
                )
            self.require_current(runtime.gateway_settings)
            current_trial = runtime.store.get_standard_diagnosis(trial_id, scope.scope_fingerprint)
            if registration.cancelled or not current_trial or current_trial["status"] != "running":
                raise ValueError("trial cancelled")
            token, grant = grants.issue(
                scope_fingerprint=scope.scope_fingerprint,
                order_no=order if live else "",
                environment=self.route.environment,
                tenant_id=scope.effective_tenant_id,
                qa_id=trial_id,
                workflow_id=workflow_id,
            )
            runs.issue_spec(
                DifyRunSpec(
                    run_id=run_id,
                    qa_id=trial_id,
                    scope_fingerprint=scope.scope_fingerprint,
                    scope_json=scope_to_json(scope),
                    binding_key=self.route.key,
                    order_no=order if live else "",
                    environment=self.route.environment,
                    tenant_id=scope.effective_tenant_id,
                    app_id=app_id,
                    workflow_id=workflow_id,
                    grant_id=grant.grant_id,
                    kind="service_trial",
                    route_revision=self.route.revision,
                    binding_digest=_binding_identity(self.binding),
                    language=language,
                    data_kind=kind,
                )
            )
            if is_agent_v6:
                from aiops_diagnostics.consumer import consumer_agent

                consumer_agent.initialize(
                    runtime,
                    run_id,
                    {
                        "history": [],
                        "selected_order": order,
                        "historical_results_are_not_current": True,
                    },
                )
            engine = DifyChatflowClient(runtime.gateway_settings.dify_chat_base_url)
            user = "trial:" + grant.grant_id

            def started(workflow_run_id, task_id, conversation_id, message_id):
                if not runs.bind_workflow(run_id, workflow_run_id=workflow_run_id, task_id=task_id):
                    raise ValueError("trial run changed")
                if not runs.bind_message(run_id, conversation_id, message_id):
                    raise ValueError("trial message changed")
                registration.interrupt = lambda: engine.stop(key=key, task_id=task_id, user=user)
                if registration.cancelled or runs.get(run_id).state != "running":
                    registration.interrupt()
                    raise ValueError("trial cancelled")

            if is_agent_v6:
                inputs = {
                    "language": language,
                    "data_grant": token,
                    "conversation_context": json.dumps(
                        runs.context(run_id)["conversation"], ensure_ascii=False
                    ),
                }
            else:
                inputs = legacy_callbacks.build_legacy_trial_inputs(
                    self.binding, language=language, kind=kind, token=token
                )
            reply = engine.run(
                key=key,
                workflow_id=workflow_id,
                user=user,
                query=self.question,
                inputs=inputs,
                **({"agent_final_node": "answer"} if is_agent_v6 else {}),
                on_started=started,
            )
            if not is_agent_v6:
                legacy_callbacks.verify_legacy_trial_pre_commit(runtime, scope, self.binding, live=live)
            if order:
                self.require_order(runtime, scope, order)
            self.require_current(runtime.gateway_settings)
            latest = runs.get(run_id)
            if (
                latest is None
                or latest.state != "running"
                or not still_valid(latest)
                or registration.cancelled
            ):
                raise ValueError("trial ended")
            context = runs.context(run_id)
            if is_agent_v6:
                consumer_agent.require_evidence(runtime, scope, self.binding, context, trial=self)
                answer = consumer_agent.final_answer(
                    reply.answer, context, language=language, question=self.question
                )
            else:
                answer = legacy_callbacks.finish_legacy_trial(
                    live=live,
                    kind=kind,
                    order=order,
                    context=context,
                    reply_answer=reply.answer,
                    language=language,
                    content=self.content["content"],
                )
            if not runs.store_snapshot(run_id, answer) or not runs.succeed(run_id):
                raise ValueError("trial result not stored")
            return TrialResult(
                "completed",
                {
                    "summary": "\n\n".join(b["text"] for b in answer["blocks"] if b.get("kind") == "text"),
                    "workflow_run_id": reply.workflow_run_id,
                    "task_id": reply.task_id,
                    "workflow_id": workflow_id,
                    "app_id": app_id,
                    "draft_sha256": self.fingerprint(),
                    "source_app_id": self.route.app_id,
                    "managed_draft": self.draft,
                    "knowledge_base_ids": list(self.binding.knowledge_base_ids),
                    "callback_version": self.binding.callback_version,
                },
                (),
                (),
                (),
                language,
            )
        except Exception:
            runs.fail(run_id, "trial_failed")
            raise
        finally:
            grants.revoke_for_qa(trial_id)
            runtime._qa_registrations.pop(trial_id, None)
            try:
                if app_id and self.published is None:
                    admin.delete_app(self.source.workspace_id, app_id)
            finally:
                if self.candidate is not None:
                    self.candidate.close()
