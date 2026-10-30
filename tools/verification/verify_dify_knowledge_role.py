"""原Dify镜像内运行真实角色守卫；账号/目录替身不写库、不联网。"""

import json
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from app_factory import create_flask_app_with_configs
from configs import dify_config
from controllers.console.workspace import plugin
from flask import g
from models.account import Account, Tenant, TenantAccountRole
from werkzeug.exceptions import Forbidden, HTTPException

PARAMS = {
    "plugin_id": "trendpower/consumer_knowledge",
    "provider": "trendpower/consumer_knowledge/consumer_knowledge",
    "action": "consumer_knowledge",
    "parameter": "knowledge_base_ids",
    "provider_type": "tool",
}
app = create_flask_app_with_configs()


def request(role, *, values=None, visible=True, rbac=False):
    account = Account(name="synthetic", email="editor@example.invalid")
    account.id = "11111111-1111-1111-1111-111111111111"
    account.role = TenantAccountRole(role)
    account._current_tenant = Tenant(name="synthetic")
    account._current_tenant.id = "22222222-2222-2222-2222-222222222222"
    query = "/console/api/workspaces/current/plugin/parameters/dynamic-options?" + urlencode(
        {**PARAMS, **(values or {})}
    )
    credentials = [SimpleNamespace(id="visible-current-tenant")] if visible else []
    with (
        app.test_request_context(query),
        patch("controllers.console.wraps._is_setup_completed", return_value=True),
        patch("libs.login.check_csrf_token"),
        patch.object(dify_config, "RBAC_ENABLED", rbac),
        patch.object(plugin, "db", SimpleNamespace(engine=object())),
        patch.object(plugin, "Session", create=True),
        patch(
            "services.tools.builtin_tools_manage_service.BuiltinToolManageService.get_builtin_tool_provider_credentials",
            return_value=credentials,
        ) as directory,
        patch.object(plugin.PluginParameterService, "get_dynamic_select_options", return_value=[]) as fetch,
        patch.object(plugin, "enforce_rbac_checks", side_effect=Forbidden(), create=True) as enterprise,
    ):
        g._login_user = account
        try:
            body = plugin.PluginFetchDynamicSelectOptionsApi().get()
            code = 200
        except HTTPException as exc:
            code, body = exc.code, exc.description
        if directory.called:
            assert directory.call_args.args[:2] == (account.current_tenant_id, PARAMS["provider"])
            assert directory.call_args.kwargs == {"user": account}
        if fetch.called:
            assert fetch.call_args.kwargs["tenant_id"] == account.current_tenant_id
            assert fetch.call_args.kwargs["credential_id"] == "visible-current-tenant"
        return code, body, fetch.call_count, enterprise.call_count


def main():
    cases = [
        (role, {}, role in {"owner", "admin", "editor"})
        for role in ("owner", "admin", "editor", "normal", "dataset_operator")
    ]
    cases += [
        ("editor", {"values": {key: value}}, False)
        for key, value in (
            ("plugin_id", "other/plugin"),
            ("provider", "other/plugin/tool"),
            ("action", "another_action"),
            ("parameter", "private_data"),
            ("provider_type", "trigger"),
            ("credential_id", "foreign-or-private"),
        )
    ]
    cases += [("editor", {"visible": False}, False), ("editor", {"rbac": True}, False)]
    results = []
    for role, kwargs, allowed in cases:
        status, body, calls, enterprise_calls = request(role, **kwargs)
        passed = status == (200 if allowed else 403) and calls == int(allowed)
        if kwargs.get("rbac"):
            passed = passed and enterprise_calls == 1
        results.append({"role": role, "case": kwargs, "status": status, "passed": passed})
    print(json.dumps(results))
    assert all(r["passed"] for r in results), "editor choices or privilege boundary failed"


if __name__ == "__main__":
    main()
