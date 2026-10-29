#!/usr/bin/env python3
"""M3-02：真 Gateway + 真 Java 节点，合成编辑器/平台/模型/业务的发布生命周期。"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import verify_chat_node_entry as chain
from verify_dify_chat_entry import _seed_agent, _seed_registry, _seed_workspace

APP = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
APP_B = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
PUBLISH = "synthetic-node-publication-key"
TRIAL = "synthetic-node-trial-key"


class Editor(BaseHTTPRequestHandler):
    prompt = "SOP_VERSION_ONE"

    def log_message(self, *_):
        pass

    def do_GET(self):  # noqa: N802
        prompt = "SOP_OTHER_TENANT" if APP_B in self.path else type(self).prompt
        body = json.dumps(
            {"data": json.dumps({"app": {"mode": "chat"}, "model_config": {"pre_prompt": prompt}})}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix="node-publication-"))
    deps, jdk = os.environ["M3NODE_DEPS"], os.environ["M3NODE_JDK_IMAGE"]
    classes = root / "classes"
    classes.mkdir()
    rc, log = chain._compile(classes, Path(deps), jdk)
    (root / "compile.log").write_text(log)
    if rc:
        return 2
    servers = [
        chain._serve(t) for t in (chain.ModelHandler, chain.CpmsHandler, chain.PlatformHandler, Editor)
    ]
    model, cpms, platform, editor = servers
    process = None
    report = {
        "scope": "real Java node and Gateway; synthetic editor, platform, model, CPMS and identity",
        "checks": [],
    }

    def check(name, ok, detail=None):
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})

    try:
        (root / "config").mkdir()
        (root / "data").mkdir()
        settings = root / "config/production.env"
        settings.write_text("# synthetic\n")
        model_url = f"http://127.0.0.1:{model.server_port}/v1"
        manifest = root / "exec-manifest.json"
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--user",
                "1000:1000",
                "-v",
                f"{classes}:/classes:ro",
                "-v",
                f"{deps}:/deps:ro",
                "-v",
                f"{root}:/case",
                jdk,
                "java",
                "-cp",
                "/classes:/deps/lib/*",
                "m3node.NodeLinkAcceptance",
                "--write-manifest=/case/exec-manifest.json",
                "--model=" + model_url,
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        manifest.chmod(0o600)
        bindings = chain._write_bindings(
            root,
            tenancies={chain.TENANT: "node-local"},
            manifest=manifest,
            platform_url=f"http://127.0.0.1:{platform.server_port}",
        )
        config = json.loads(bindings.read_text())["bindings"][0]["config_file"]
        targets = root / "targets.json"
        targets.write_text(
            json.dumps(
                {
                    "schema": "aiops-node-targets/1",
                    "targets": [
                        {
                            "app_id": APP,
                            "tenant_id": chain.TENANT,
                            "environment": "env-local",
                            "config_file": config,
                            "manifest_file": str(manifest),
                        }
                    ],
                }
            )
        )
        content = json.loads(targets.read_text())
        content["targets"].append({**content["targets"][0], "app_id": APP_B, "tenant_id": chain.OTHER_TENANT})
        targets.write_text(json.dumps(content))
        targets.chmod(0o600)
        orders = root / "data/orders.db"
        with sqlite3.connect(orders) as connection:
            connection.execute(
                "CREATE TABLE synthetic_orders(order_no TEXT PRIMARY KEY,user_id TEXT,"
                "tenant_id TEXT,created_time TEXT,id TEXT)"
            )
            connection.execute(
                "INSERT INTO synthetic_orders VALUES(?,?,?,?,?)",
                (chain.ORDER_LATEST, chain.C_USER, chain.TENANT, "2026-10-24 00:00:00", "1"),
            )
        with sqlite3.connect(orders) as connection:
            connection.execute(
                "INSERT INTO synthetic_orders VALUES(?,?,?,?,?)",
                (chain.ORDER_OTHER, chain.OTHER_C_USER, chain.OTHER_TENANT, "2026-10-24 00:00:00", "2"),
            )
        database = root / "data/gateway.db"
        assert _seed_registry(
            database,
            [
                {
                    "tenant_id": chain.TENANT,
                    "business_entry": "consumer",
                    "app_id": APP,
                    "agent_name": "小趋-客服",
                }
            ],
        )
        assert _seed_workspace(database, chain.TENANT, "workspace-node", "node-publication")
        assert _seed_agent(database, chain.TENANT)
        assert _seed_registry(
            database,
            [
                {
                    "tenant_id": chain.OTHER_TENANT,
                    "business_entry": "consumer",
                    "app_id": APP_B,
                    "agent_name": "小趋-客服",
                }
            ],
        )
        assert _seed_workspace(database, chain.OTHER_TENANT, "workspace-other", "node-publication")
        assert _seed_agent(database, chain.OTHER_TENANT)
        port = chain._free_port()
        base = f"http://127.0.0.1:{port}"
        chain.PlatformHandler.state = {
            "gateway": base,
            "model": model_url,
            "scenario": "normal",
            "managed": True,
            "dispatched": [],
            "node_results": [],
            "threads": [],
            "timeout": 120,
            "classes": str(classes),
            "deps": deps,
            "jdk": jdk,
            "case_dir": str(root),
            "platform_url": f"http://127.0.0.1:{platform.server_port}",
        }
        chain.ModelHandler.state = {"calls": 0, "mode": "normal"}
        chain.CpmsHandler.calls = []
        env = {
            **os.environ,
            "PYTHONPATH": str(chain.REPO / "src"),
            "AIOPS_DATA_HOME": str(root / "data"),
            "AIOPS_CONFIG_HOME": str(root / "config"),
            "AIOPS_GATEWAY_DATA_HOME": str(root / "data"),
            "AIOPS_GATEWAY_SERVER_CONFIG_FILE": str(settings),
            "AIOPS_GATEWAY_DATABASE_FILE": str(database),
            "AIOPS_GATEWAY_BIND": "127.0.0.1",
            "AIOPS_GATEWAY_PORT": str(port),
            "AIOPS_GATEWAY_ENVIRONMENT": "node-publication",
            "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT": "env-local",
            "AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED": "1",
            "AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE": str(database),
            "AIOPS_GATEWAY_CHAT_NODE_TARGETS_FILE": str(targets),
            "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL": f"http://127.0.0.1:{editor.server_port}",
            "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY": "synthetic-console",
            "AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY": PUBLISH,
            "AIOPS_GATEWAY_DIFY_TRIAL_API_KEY": TRIAL,
            "AIOPS_GATEWAY_DIFY_TRIAL_DEBUG_TENANT": chain.TENANT,
            "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL": f"http://127.0.0.1:{cpms.server_port}/charging-pile",
            "AIOPS_M2_LOCAL_TOKEN": chain.TOKEN,
            "AIOPS_M2_SECOND_TOKEN": chain.OTHER_TOKEN,
            "AIOPS_M2_SECOND_C_USER": chain.OTHER_C_USER,
            "AIOPS_M2_SECOND_TENANT": chain.OTHER_TENANT,
            "AIOPS_M2_ORDERS_DB": str(orders),
            "AIOPS_M2_TENANT": chain.TENANT,
            "AIOPS_M2_C_USER": chain.C_USER,
            "AIOPS_M2_PLATFORM_CONSUMER": "1",
        }
        with (root / "gateway.log").open("w") as output:
            process = subprocess.Popen(
                [sys.executable, str(chain.REPO / "tools/m2_close/gateway_launcher.py")],
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 30
        while chain.http(base + "/health")[0] != 200:
            assert time.monotonic() < deadline, (root / "gateway.log").read_text()
            time.sleep(0.2)

        def operation(name, app_id=APP, **extra):
            status, text = chain.http(
                base + "/v1/dify/" + name, method="POST", token=PUBLISH, body={"app_id": app_id, **extra}
            )
            body = json.loads(text)
            check(name + " accepted", status == 200, {"status": status, "body": body})
            return body

        def ask(expected, token=chain.TOKEN):
            chain.ModelHandler.state["prompts"] = []
            status, text = chain.http(
                base + "/v1/assistant/questions",
                method="POST",
                token=token,
                headers={"X-Business-Entry": "consumer"},
                body={"question": "check my latest order status"},
            )
            if expected is None:
                check("retracted no dispatch", status == 410, {"status": status})
                return
            qa = json.loads(text)["qa_id"]
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                code, text = chain.http(
                    base + "/v1/assistant/questions/" + qa,
                    token=token,
                    headers={"X-Business-Entry": "consumer"},
                )
                value = json.loads(text)
                if code == 200 and value.get("status") in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(0.2)
            prompts = json.dumps(chain.ModelHandler.state["prompts"])
            check(
                "App executes " + expected, value.get("status") == "completed" and expected in prompts, value
            )
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT expected_agent_version,observed_agent_version,exec_config_state,"
                    "route_revision FROM agent_runs WHERE qa_id=?",
                    (qa,),
                ).fetchone()
            check(
                "node attests frozen publication",
                row and row[0] == row[1] and row[2] == "matched" and row[3] > 0,
            )

            return qa

        first = operation("publish")
        ask("SOP_VERSION_ONE")
        Editor.prompt = "SOP_VERSION_TWO"
        ask("SOP_VERSION_ONE")
        chain.ModelHandler.state["prompts"] = []
        status, text = chain.http(
            base + "/v1/dify/trial",
            method="POST",
            token=TRIAL,
            body={"app_id": APP, "order_no": chain.ORDER_LATEST},
        )
        trial = json.loads(text).get("trial_id")
        deadline = time.monotonic() + 120
        value = {}
        while trial and time.monotonic() < deadline:
            _, text = chain.http(base + "/v1/dify/trial/" + trial, token=TRIAL)
            value = json.loads(text)
            if value.get("status") in {"completed", "failed"}:
                break
            time.sleep(0.2)
        check(
            "trial executes draft on real node",
            status == 202
            and value.get("status") == "completed"
            and "SOP_VERSION_TWO" in json.dumps(chain.ModelHandler.state["prompts"]),
            value,
        )
        ask("SOP_VERSION_ONE")
        operation("publish")
        ask("SOP_VERSION_TWO")
        operation("publish", app_id=APP_B)
        ask("SOP_OTHER_TENANT", token=chain.OTHER_TOKEN)
        before = len(chain.PlatformHandler.state["dispatched"])
        operation("retract")
        ask(None)
        check("withdrawal sends no new node task", before == len(chain.PlatformHandler.state["dispatched"]))
        ask("SOP_OTHER_TENANT", token=chain.OTHER_TOKEN)
        operation("restore", version_no=first["version_no"])
        ask("SOP_VERSION_ONE")
        # 两条不同租户的已发布版本在同一 JVM 并发，取数/完成仍由真节点发出。
        from aiops_diagnostics import chat_execution
        from aiops_diagnostics.agent_runs import AgentRunStore
        from aiops_diagnostics.gateway_config import GatewayServerSettings
        from aiops_diagnostics.identity.scope_context import DataScope, ScopeContext, SubjectRecord

        run_store = AgentRunStore(database)
        local_settings = GatewayServerSettings(
            data_home=root / "data",
            database_file=database,
            execution_environment="env-local",
            chat_route_bindings_file=str(database),
        )
        paired = []
        for tenant, user, order in (
            (chain.TENANT, chain.C_USER, chain.ORDER_LATEST),
            (chain.OTHER_TENANT, chain.OTHER_C_USER, chain.ORDER_OTHER),
        ):
            subject = SubjectRecord(b_user_id="", c_user_id=user, tenant_id=tenant)
            scope = ScopeContext.build(
                caller=subject,
                subject=subject,
                delegated=False,
                effective_tenant_id=tenant,
                data_scope=DataScope(type="self"),
                roles=frozenset(),
                permissions=frozenset(),
            )
            binding = chat_execution.select_binding(local_settings, scope)
            run, _ = chat_execution.issue_for_store(
                store=run_store,
                settings=local_settings,
                binding=binding,
                caller=scope,
                order_no=order,
                gateway_version="test",
            )
            run_store.bind_qa(run.run_id, "qa_" + run.run_id)
            paired.append(run)
        chain.ModelHandler.state["prompts"] = []
        done = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network=host",
                "--user",
                "1000:1000",
                "-v",
                f"{classes}:/classes:ro",
                "-v",
                f"{deps}:/deps:ro",
                jdk,
                "java",
                "-cp",
                "/classes:/deps/lib/*",
                "m3node.NodeLinkAcceptance",
                "--managed-publication",
                "--scenario=two-runs",
                "--run-id=" + paired[0].run_id,
                "--second-run-id=" + paired[1].run_id,
                "--gateway=" + base,
                "--model=" + model_url,
                "--node-token=" + chain.NODE_TOKEN,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        (root / "two-runs.log").write_text(done.stdout + done.stderr)
        settled = [run_store.get(run.run_id) for run in paired]
        check(
            "two tenants in one JVM keep their manifests",
            done.returncode == 0
            and all(run.state == "succeeded" and run.exec_config_confirmed for run in settled)
            and settled[0].observed_agent_version != settled[1].observed_agent_version,
            {"states": [run.state for run in settled]},
        )
        prompts = chain.ModelHandler.state["prompts"]
        systems = [m.get("content") for messages in prompts for m in messages if m.get("role") == "system"]
        check(
            "concurrent model inputs use both published SOPs",
            "SOP_VERSION_ONE" in systems and "SOP_OTHER_TENANT" in systems,
            systems,
        )
        # 撤回后旧结果和新试用读取均拒绝；另一个租户保持运行能力。
        operation("retract")
        trial_status, trial_body = chain.http(base + "/v1/dify/trial/" + trial, token=TRIAL)
        check(
            "retracted node trial result is refused",
            trial_status in {403, 404, 410, 503} and "result" not in json.loads(trial_body),
            {"status": trial_status},
        )
        # 从实际发布代次签发的一条尚未领取任务，撤回后节点配置和完成都不可得。
        operation("restore", version_no=first["version_no"])
        subject = SubjectRecord(b_user_id="", c_user_id=chain.C_USER, tenant_id=chain.TENANT)
        scope = ScopeContext.build(
            caller=subject,
            subject=subject,
            delegated=False,
            effective_tenant_id=chain.TENANT,
            data_scope=DataScope(type="self"),
            roles=frozenset(),
            permissions=frozenset(),
        )
        binding = chat_execution.select_binding(local_settings, scope)
        pending, _ = chat_execution.issue_for_store(
            store=run_store,
            settings=local_settings,
            binding=binding,
            caller=scope,
            order_no=chain.ORDER_LATEST,
            gateway_version="test",
        )
        run_store.bind_qa(pending.run_id, "qa_" + pending.run_id)
        operation("retract")
        denied, _ = chain.http(
            base + "/v1/internal/charging-status/execution-config/" + pending.run_id,
            method="POST",
            token=chain.NODE_TOKEN,
            body={},
        )
        completion, _ = chain.http(
            base + "/v1/internal/charging-status/completions/" + pending.run_id,
            method="POST",
            token=chain.NODE_TOKEN,
            body={"status": "completed"},
            headers={"X-AI-Ops-Exec-Config": pending.expected_agent_version},
        )
        check(
            "withdrawn node run cannot fetch config or complete",
            denied in {403, 404}
            and completion in {403, 404}
            and run_store.get(pending.run_id).state == "revoked",
            {"config_status": denied, "completion_status": completion},
        )
        report["node_results"] = chain.PlatformHandler.state["node_results"]
    except Exception as exc:
        check("acceptance completed", False, type(exc).__name__)
        raise
    finally:
        if process is not None:
            chain._stop(process)
        for server in servers:
            server.shutdown()
        report["failures"] = sum(not x["ok"] for x in report["checks"])
        report["status"] = "PASS" if report["checks"] and not report["failures"] else "FAIL"
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "checks": len(report["checks"]),
                    "failures": report["failures"],
                    "root": str(root),
                }
            )
        )
    return bool(report["failures"])


if __name__ == "__main__":
    raise SystemExit(main())
