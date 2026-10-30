"""在临时 Dify 应用执行冻结草稿；不改源应用或正式指针。"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, replace
from typing import Any

from aiops_diagnostics.dify.dify_chat_workflow import DifyWorkflowClient
from aiops_diagnostics.dify.dify_console_admin import ConsoleApiSettings, DifyConsoleAdmin
from aiops_diagnostics.dify.dify_dsl_pull import DifyDslClient, DifyDslSource, workflow_content
from aiops_diagnostics.dify.dify_run_store import DifyRunStore
from aiops_diagnostics.dify.dify_trial import TrialResult
from aiops_diagnostics.execution import chat_data_grant
from aiops_diagnostics.execution.agent_runs import scope_to_json
from aiops_diagnostics.execution.chat_route_bindings import ChatRouteBindingStore, RouteBinding


@dataclass(frozen=True)
class WorkflowTrial:
    dsl: str
    source: DifyDslSource
    route: RouteBinding

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(workflow_content(self.dsl), sort_keys=True).encode()).hexdigest()

    def require_current(self, settings: Any) -> None:
        current = ChatRouteBindingStore(settings.chat_route_bindings_file).by_key(self.route.key)
        if current is None or not current.active or current != self.route:
            raise RuntimeError("trial source route changed")

    def execute(self, runtime: Any, trial_id: str, scope: Any, order: str, language: str) -> TrialResult:
        from aiops_diagnostics.gateway.gateway_runtime import JobRegistration

        settings = runtime.gateway_settings
        self.require_current(settings)
        admin = DifyConsoleAdmin(ConsoleApiSettings(self.source.base_url, self.source.api_key))
        runs = DifyRunStore(runtime.store.path)
        grants = chat_data_grant.DataGrantStore(runtime.store.path)
        app_id = ""
        run_id = "trial_" + secrets.token_hex(16)
        try:
            app_id = admin.import_app_dsl(
                self.source.workspace_id, yaml_content=self.dsl, name="AI-Ops trial " + trial_id
            )
            client = DifyDslClient(replace(self.source, app_id=app_id))
            if not client.publish_workflow():
                raise RuntimeError("temporary workflow was not published")
            workflow_id = client.published_workflow_id()
            if workflow_content(client.export_dsl(workflow_id=workflow_id)) != workflow_content(self.dsl):
                raise RuntimeError("temporary workflow differs from frozen draft")
            key = admin.app_service_key(self.source.workspace_id, app_id)
            self.require_current(settings)
            runtime._require_chat_order_visible(scope, order)
            token, grant = grants.issue(
                scope_fingerprint=scope.scope_fingerprint,
                order_no=order,
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
                order_no=order,
                environment=self.route.environment,
                tenant_id=scope.effective_tenant_id,
                app_id=app_id,
                workflow_id=workflow_id,
                grant_id=grant.grant_id,
                kind="trial",
                route_revision=self.route.revision,
            )
            registration = JobRegistration(route_type="trial")
            runtime._qa_registrations[trial_id] = registration
            engine = DifyWorkflowClient(settings.dify_chat_base_url)

            def started(workflow_run_id: str, task_id: str) -> None:
                if not runs.bind_workflow(run_id, workflow_run_id=workflow_run_id, task_id=task_id):
                    raise RuntimeError("trial identity changed")
                registration.interrupt = lambda: engine.stop(
                    workflow_key=key, task_id=task_id, user="aiops:" + grant.grant_id
                )
                current = runs.get(run_id)
                if registration.cancelled or current is None or current.state != "running":
                    registration.interrupt()

            result = engine.run(
                workflow_key=key,
                workflow_id=workflow_id,
                inputs={"data_grant": token, "order_no": order, "question": "trial", "language": language},
                user="aiops:" + grant.grant_id,
                on_started=started,
            )
            runtime._require_chat_order_visible(scope, order)
            self.require_current(settings)
            answer = runtime._release_dify_answer(
                run_id=run_id,
                grant_id=grant.grant_id,
                binding=self.route,
                context=scope,
                order_no=order,
                language=language,
                outputs=result.outputs,
            )
            if not runs.succeed(run_id):
                raise RuntimeError("trial is no longer active")
            return TrialResult(
                "completed",
                {
                    "summary": answer["blocks"][0]["text"],
                    "workflow_run_id": result.workflow_run_id,
                    "task_id": result.task_id,
                    "workflow_id": workflow_id,
                    "app_id": app_id,
                    "draft_sha256": self.fingerprint(),
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
