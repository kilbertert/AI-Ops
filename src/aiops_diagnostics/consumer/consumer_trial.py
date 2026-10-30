"""在临时Chatflow执行草稿，沿用源路由与试用身份，不推进正式版本。"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, replace

from aiops_diagnostics.consumer.consumer_knowledge import _binding_identity, final_answer
from aiops_diagnostics.dify.dify_chatflow import DifyChatflowClient
from aiops_diagnostics.dify.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
from aiops_diagnostics.dify.dify_dsl_pull import DifyDslClient, workflow_content
from aiops_diagnostics.dify.dify_run_store import still_valid
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
    #: 草稿预览：这个副本还没有正式版，`route` 是合成的（`state="draft"`）。
    #: 试用本来就在**临时应用**里跑这份 DSL，所以它不需要正式路由也能证明"草稿可用"；
    #: 需要的是"这份草稿仍然属于这个客户"，那一条由 `require_current` 的草稿分支查目录。
    draft: bool = False
    authorize: object = None

    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(
                {"graph": workflow_content(self.dsl), "content": self.content, "question": self.question},
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def require_current(self, settings):
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

    def execute(self, runtime, trial_id, scope, order, language):
        from aiops_diagnostics.gateway.gateway_runtime import JobRegistration

        self.require_current(runtime.gateway_settings)
        admin = DifyConsoleAdmin(ConsoleApiSettings(self.source.base_url, self.source.api_key))
        grants = DataGrantStore(runtime.store.path)
        runs = runtime.dify_run_store
        app_id = ""
        run_id = "consumer_trial_" + secrets.token_hex(16)
        registration = JobRegistration(route_type="trial")
        runtime._qa_registrations[trial_id] = registration
        try:
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
            # 试用订单只用于既有试用身份授权；知识问题不向模型交出该单数据。
            from aiops_diagnostics.consumer.consumer_intent import message, validate
            from aiops_diagnostics.execution.chat_execution import ChatRunUnavailable

            decision = validate(
                runtime.classify_consumer(self.question, language=language, selected_order=bool(order))
            )
            intent = decision["intent"]
            if intent not in {"knowledge", "order_status", "billing_record"} or decision["uses_context"]:
                raise ChatRunUnavailable("TRIAL_CLARIFICATION_REQUIRED", message("clarify", language))
            kind = "billing" if intent == "billing_record" else "status"
            live = intent in {"order_status", "billing_record"}
            if live and (not order or decision["order_reference"] != "explicit"):
                raise ChatRunUnavailable(
                    "TRIAL_ORDER_REQUIRED", "试用查询请明确使用选定订单，不支持另选最新单"
                )
            if live and not (
                self.binding.live_billing_record if kind == "billing" else self.binding.live_order_status
            ):
                raise ChatRunUnavailable("CHATFLOW_LIVE_DISABLED", message("disabled", language))
            if order:
                runtime._require_chat_order_visible(scope, order)
            if not live:
                from aiops_diagnostics.consumer.consumer_knowledge_selection import require_available

                require_available(runtime, scope, self.binding)
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
            runs.issue(
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

            reply = engine.run(
                key=key,
                workflow_id=workflow_id,
                user=user,
                query=self.question,
                inputs={
                    "language": language,
                    "has_photo": "no",
                    "data_grant": token,
                    **(
                        {"data_kind": kind if live else "knowledge"}
                        if self.binding.callback_version >= 2
                        else {}
                    ),
                },
                on_started=started,
            )
            if not live:
                require_available(runtime, scope, self.binding)
            if order:
                runtime._require_chat_order_visible(scope, order)
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
            if live:
                snapshot = (context or {}).get("live_snapshot", {})
                expected_source = "cpms.orderDetail" if kind == "billing" else "cpms.chargingInfo"
                if snapshot.get("order_no") != order or snapshot.get("source") != expected_source:
                    raise ValueError("trial has no matching live snapshot")
            answer = final_answer(reply.answer, context, language, self.content["content"])
            if not runs.store_snapshot(run_id, answer) or not runs.succeed(run_id):
                raise ValueError("trial result not stored")
            return TrialResult(
                "completed",
                {
                    "summary": answer["blocks"][0]["text"],
                    "workflow_run_id": reply.workflow_run_id,
                    "task_id": reply.task_id,
                    "workflow_id": workflow_id,
                    "app_id": app_id,
                    "draft_sha256": self.fingerprint(),
                    "source_app_id": self.route.app_id,
                    "managed_draft": self.draft,
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
            if app_id:
                admin.delete_app(self.source.workspace_id, app_id)
