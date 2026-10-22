#!/usr/bin/env python3
"""真 Gateway HTTP、真实发布/绑定、合成业务读取；不证明 Dify 或真实业务。"""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import uvicorn

from aiops_diagnostics.agent_contracts import OPS_TOOL_NAMES
from aiops_diagnostics.agent_lifecycle import AgentConfig, AgentManager, AgentStore
from aiops_diagnostics.dify_app_registry import DifyAppBinding
from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.ops_bindings import token_digest
from aiops_diagnostics.ops_tools import OpsToolService
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    checks = []
    with tempfile.TemporaryDirectory(prefix="ops-http-") as tmp:
        root = Path(tmp)
        database = root / "gateway.db"
        config = root / "config.env"
        config.write_text("# isolated\n")
        config.chmod(0o600)
        token = secrets.token_urlsafe(32)
        manager = AgentManager(AgentStore(database))
        context = SimpleNamespace(
            effective_tenant_id="tenant-a",
            roles={"ROLE_AGENT_ADMIN"},
            subject=SimpleNamespace(b_user_id="fixture"),
        )
        agent = manager.create(
            context,
            name="ops",
            description="",
            config=AgentConfig(
                "operations", "查询订单状态", (), "aiops-api", "diagnosis-v1", allowed_tools=OPS_TOOL_NAMES
            ),
        )
        manager.publish(context, agent.agent_id, expected_revision=agent.revision)
        binding_file = root / "binding.json"
        binding = {
            "key_sha256": token_digest(token),
            "environment": "verify",
            "tenant_id": "tenant-a",
            "workspace_id": "ws-a",
            "app_id": "app-a",
            "agent_id": agent.agent_id,
            "active": True,
        }

        def save_binding():
            binding_file.write_text(json.dumps([binding]))
            binding_file.chmod(0o600)

        save_binding()
        settings = GatewayServerSettings(
            data_home=root,
            database_file=database,
            server_config_file=config,
            environment="verify",
            ops_bindings_file=str(binding_file),
            ops_charging_status_base_url="tenant-a:http://127.0.0.1:9",
        )
        settings.validate()
        service = OpsToolService(settings, manager)
        service.workspaces.claim("tenant-a", "verify", owner="fixture", lease_seconds=30)
        service.workspaces.mark_created("tenant-a", "verify", "ws-a")
        service.registry.put(DifyAppBinding("tenant-a", "operator", "app-a", "ops"))
        calls = []

        def synthetic_read(self, bound, tool, inputs, base):
            calls.append(tool)
            if tool == "charging_info":
                return {
                    "order_no": inputs.order_no,
                    "status_code": 1,
                    "status_role": "finished",
                    "observed_at": "synthetic",
                    "source": "fixture.cpms",
                }
            return {"orders": [{"order_no": "O-1"}], "source": "fixture.mysql"}

        with patch.object(OpsToolService, "_read", synthetic_read):
            subject = SubjectRecord(b_user_id="fixture", c_user_id="fixture", tenant_id="tenant-a")
            caller = ScopeContext.build(
                caller=subject,
                subject=subject,
                delegated=False,
                effective_tenant_id="tenant-a",
                data_scope=DataScope(type="self"),
                roles=frozenset(),
                permissions=frozenset(),
            )
            app = create_gateway_app(
                settings=settings,
                agent_manager=manager,
                runtime=SimpleNamespace(kb_search_client=None),
                caller_resolver=SimpleNamespace(resolve=lambda *args, **kwargs: caller),
            )
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 15
                while not server.started and thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert server.started, "gateway did not start"

                def request(tenant, tool, payload, key):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/v1/dify/ops/{tenant}/{tool}",
                        data=json.dumps(payload).encode(),
                        headers={
                            "Content-Type": "application/json",
                            **({"Authorization": f"Bearer {key}"} if key else {}),
                        },
                    )
                    try:
                        response = urllib.request.urlopen(req, timeout=5)
                    except urllib.error.HTTPError as error:
                        response = error
                    with response:
                        return response.status, json.load(response), response.headers.get("Cache-Control")

                def check(name, expected, tenant="tenant-a", tool="order_list", payload=None, key=token):
                    before = len(calls)
                    status, body, cache = request(tenant, tool, payload or {}, key)
                    assert status == expected, (name, status, body)
                    assert cache == "no-store", (name, cache)
                    assert token not in json.dumps(body)
                    if status != 200:
                        assert len(calls) == before, (name, "read despite refusal")
                    checks.append({"case": name, "status": status, "business_calls": len(calls) - before})
                    return body

                check("missing credential", 401, key="")
                check("wrong tenant", 403, tenant="tenant-b")
                check("list", 200)
                body = check("status", 200, tool="charging_info", payload={"order_no": "O-1"})
                assert body["result"]["order_no"] == "O-1"
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/orders/O-1/charging-status",
                    headers={"Authorization": "Bearer consumer-fixture", "X-Business-Entry": "consumer"},
                )
                before = len(calls)
                try:
                    urllib.request.urlopen(req, timeout=5)
                    raise AssertionError("consumer status was enabled by ops settings")
                except urllib.error.HTTPError as error:
                    assert error.code == 404
                    assert json.load(error)["error"]["code"] == "CHARGING_STATUS_DISABLED"
                assert len(calls) == before
                checks.append(
                    {"case": "ops endpoint keeps consumer disabled", "status": 404, "business_calls": 0}
                )
                a = manager.get(context, agent.agent_id)
                draft = manager.fork_draft(context, a.agent_id, expected_revision=a.revision)
                draft = manager.update(
                    context,
                    draft.agent_id,
                    expected_revision=draft.revision,
                    name=draft.name,
                    description=draft.description,
                    config=replace(draft.config, allowed_tools=()),
                )
                check("draft does not revoke publication", 200)
                manager.publish(context, draft.agent_id, expected_revision=draft.revision)
                check("empty publication denies list", 403)
                check(
                    "empty publication denies status", 403, tool="charging_info", payload={"order_no": "O-1"}
                )
                binding["environment"] = "other"
                save_binding()
                check("wrong environment", 403)
                binding["environment"] = "verify"
                binding["active"] = False
                save_binding()
                check("revoked credential", 403)
                check("extra identity", 422, payload={"tenant_id": "other"})
            finally:
                server.should_exit = True
                thread.join(10)
                sock.close()
                assert not thread.is_alive(), "gateway did not stop"
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "checks": checks,
        "scope": "real Gateway HTTP and authorization; synthetic business; no Dify/model/production",
        "passed": True,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"PASS: {len(checks)} HTTP cases; synthetic business only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
