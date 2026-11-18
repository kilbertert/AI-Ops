"""在固定Dify镜像中验证真实导入控制器；外部持久化用替身，禁止联网。"""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid5

from app_factory import create_flask_app_with_configs
from configs import dify_config
from controllers.console.app import app_import as module
from flask import g
from models.account import Account, Tenant, TenantAccountRole
from services.app_dsl_service import Import, ImportStatus
from werkzeug.exceptions import HTTPException

from services import app_dsl_service as service

app = create_flask_app_with_configs()
request_id = "11111111-1111-1111-1111-111111111111"
namespace = UUID("6e5a60f0-9495-4c43-a714-d954c2609aeb")
created = {}
calls = []


def request(tenant_id, role="editor", extra=None, method="POST"):
    account = Account(name="fixture", email="fixture@example.invalid")
    account.id = "33333333-3333-3333-3333-333333333333"
    account.role = TenantAccountRole(role)
    account._current_tenant = Tenant(name="fixture")
    account._current_tenant.id = tenant_id

    def create(**kwargs):
        app_id = kwargs["import_app_id"]
        assert kwargs["app_id"] is None
        assert app_id not in created
        calls.append(app_id)
        created[app_id] = SimpleNamespace(tenant_id=tenant_id)
        return Import(id=request_id, app_id=app_id, status=ImportStatus.COMPLETED)

    with (
        app.test_request_context(
            "/console/api/apps/imports",
            method=method,
            json={
                "mode": "yaml-content",
                "yaml_content": "app: fixture",
                "request_id": request_id,
                **(extra or {}),
            },
        ),
        patch("controllers.console.wraps._is_setup_completed", return_value=True),
        patch("libs.login.check_csrf_token"),
        patch.object(dify_config, "RBAC_ENABLED", False),
        patch.object(module, "db", SimpleNamespace(engine=object())),
        patch.object(module, "Session") as session,
        patch.object(module.AppDslService, "import_app", side_effect=create),
        patch.object(module.SystemFeatureService, "is_webapp_auth_enabled", return_value=False),
    ):
        g._login_user = account
        session.return_value.__enter__.return_value.get.side_effect = lambda _, key: created.get(key)
        try:
            body, code = getattr(module.AppImportApi(), method.lower())()
            return code, body
        except HTTPException as exc:
            return exc.code, {}


def main():
    a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    assert request(a, method="GET") == (
        200,
        {
            "aiops_idempotent_import": 1,
            "aiops_candidate_import": 1,
            "aiops_candidate_chatflow_import": 1,
        },
    )
    for role in ("normal", "dataset_operator"):
        assert request(a, role=role)[0] == 403
        assert request(a, role=role, method="GET")[0] == 403
    first = request(a)
    assert first[0] == 200, first
    retry = request(a)
    assert retry[0] == 200 and retry[1]["app_id"] == first[1]["app_id"]
    second = request(b)
    assert second[0] == 200 and second[1]["app_id"] != first[1]["app_id"]
    assert len(calls) == 2
    assert request(a, extra={"app_id": first[1]["app_id"]})[0] == 403
    # 即使存储内容与确定性ID矛盾，控制器也不能泄漏另一工作区。
    key = str(uuid5(namespace, a + ":" + request_id))
    created[key] = SimpleNamespace(tenant_id=b)
    assert request(a)[0] == 403
    print("Dify idempotent import: role guards, retry, tenant isolation and conflict passed")
    # 实际服务创建的第一批写入已停用，不能用创建后关入口模拟零暴露窗口。
    account = Account(name="fixture", email="fixture@example.invalid")
    account.id = "33333333-3333-3333-3333-333333333333"
    account._current_tenant = Tenant(name="fixture")
    account._current_tenant.id = a
    for mode, candidate in [(m, c) for m in ("agent-chat", "advanced-chat") for c in (False, True)]:
        session = SimpleNamespace(add=lambda _: None, flush=lambda: None, get=lambda *args: None)
        with (
            patch.object(service, "is_valid_image_icon", return_value=True),
            patch.object(service.app_was_created, "send"),
            patch.object(service.app_model_config_was_updated, "send"),
            patch.object(
                service,
                "WorkflowService",
                return_value=SimpleNamespace(
                    get_draft_workflow=lambda **_: None,
                    sync_draft_workflow=lambda **_: None,
                ),
            ),
        ):
            row = service.AppDslService(session)._create_or_update_app(
                app=None,
                data={
                    "aiops_candidate": candidate,
                    "app": {"mode": mode, "name": "fixture"},
                    "workflow": {"graph": {}},
                    "model_config": {
                        "model": {
                            "provider": "fixture",
                            "name": "fixture",
                            "mode": "chat",
                            "completion_params": {},
                        }
                    },
                },
                account=account,
                import_app_id=str(uuid5(namespace, a + ":" + request_id)),
            )
            assert row.enable_api is (not candidate) and row.enable_site is (not candidate)
    print("Dify candidate creation: disabled initial flags and unchanged default passed")


if __name__ == "__main__":
    main()
