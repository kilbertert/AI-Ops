#!/usr/bin/env python3
"""真 Snail 平台 gRPC → 打包节点 → 原 App API；编辑器/模型/业务/身份为合成。"""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import verify_chat_node_entry as chain
from verify_dify_chat_entry import _seed_agent, _seed_registry, _seed_workspace
from verify_node_publication import APP, PUBLISH, TRIAL, Editor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harness_guard  # noqa: E402
import run_ic as ic  # noqa: E402


class Fixtures(Editor):
    def do_GET(self):  # noqa: N802
        if self.path == "/evidence":
            body = json.dumps({"model": chain.ModelHandler.state, "reads": chain.CpmsHandler.calls}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).prompt = body["prompt"]
        chain.ModelHandler.state["mode"] = body.get("mode", "normal")
        self.send_response(204)
        self.end_headers()


def fixtures():
    for port, handler in ((9100, chain.ModelHandler), (9200, chain.CpmsHandler), (9202, Fixtures)):
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()


class Stack(ic.Stack):
    """复用原平台数据库初始化；所有进程共用隔离容器网络空间，不发布宿主端口。"""

    def __init__(self, repo, platform, node, image, root):
        super().__init__(repo, root / "platform", node)
        self.work = root
        self.platform = platform
        self.image = image

    def start(self):
        ic.docker("network", "create", "--internal", ic.NETWORK)
        schema = self.snail_src / "script/sql"
        schema.mkdir(parents=True)
        shutil.copyfile(
            self.repo / "deploy/m2-live-03/schema/snail_ai_schema.sql", schema / "snail_ai_schema.sql"
        )
        super()._start_mysql()
        ic.docker(
            "exec",
            "-i",
            ic.MYSQL,
            "mysql",
            "-uroot",
            "-p" + ic.MYSQL_PASSWORD,
            "snail_ai",
            stdin=(
                "UPDATE sai_model_config SET api_endpoint='http://127.0.0.1:9100/v1' "
                "WHERE model_key='m2close-model';"
            ),
        )
        self.launch(
            ic.SNAIL,
            "eclipse-temurin:21-jdk",
            [
                "java",
                "-Xmx900m",
                "-jar",
                "/platform.jar",
                "--spring.datasource.url=jdbc:mysql://127.0.0.1:3306/snail_ai?useSSL=false&allowPublicKeyRetrieval=true&allowMultiQueries=true",
                "--spring.datasource.username=root",
                "--spring.datasource.password=" + ic.MYSQL_PASSWORD,
                "--snail-ai.crypto.secret-key=" + ic.CRYPTO_KEY,
                "--snail-ai.crypto.iv=" + ic.CRYPTO_IV,
            ],
            mounts=[f"{self.platform}:/platform.jar:ro"],
            memory="1200m",
        )
        ic.wait_for(lambda: "Started SnailAiApplication" in ic.logs(ic.SNAIL), "real platform")
        self.launch(
            ic.STUB,
            self.image,
            ["python", "/repo/tools/m2_close/m3node/verify_platform_publication.py", "--fixtures"],
        )
        env = self.seed_gateway()
        self.launch(ic.GATEWAY, self.image, ["python", "/repo/tools/m2_close/gateway_launcher.py"], env=env)
        ic.wait_for(lambda: "substituted" in ic.logs(ic.GATEWAY), "gateway", timeout=60)
        self.launch(
            ic.NODE,
            "eclipse-temurin:21-jdk",
            [
                "java",
                "-Xmx400m",
                "-jar",
                "/node.jar",
                "--aiops.node.managed-publication=true",
            ],
            mounts=[f"{self.node_jar}:/node.jar:ro"],
            env={
                "AIOPS_NODE_NAME": "node-a1",
                "AIOPS_NODE_BUILD": "m3-packaged",
                "AIOPS_NODE_HOST": "127.0.0.1",
                "AIOPS_NODE_GRPC_PORT": "18889",
                "AIOPS_NODE_HOME": "/case/node-home",
                "SNAIL_SERVER_HOST": "127.0.0.1",
                "SNAIL_AI_APP_ID": ic.APP_ID,
                "SNAIL_AI_TOKEN": ic.APP_TOKEN,
                "AIOPS_NODE_TOKEN": ic.NODE_TOKEN,
                "AIOPS_GATEWAY_BASE_URL": "http://127.0.0.1:8787",
            },
        )
        ic.wait_for(
            lambda: (
                ic.APP_ID
                in ic.docker(
                    "exec",
                    ic.MYSQL,
                    "mysql",
                    "-uroot",
                    "-p" + ic.MYSQL_PASSWORD,
                    "-N",
                    "-e",
                    "SELECT app_id FROM snail_ai.sai_client_node WHERE expire_dt > NOW()",
                ).stdout
            ),
            "gRPC node registration",
        )

    def launch(self, name, image, command, *, mounts=(), env=None, memory="700m"):
        args = [
            "run",
            "-d",
            "--pull=never",
            "--name",
            name,
            "--network",
            "container:" + ic.MYSQL,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--memory",
            memory,
            "--cpus",
            "1",
            "-v",
            f"{self.repo}:/repo:ro",
            "-v",
            f"{self.work}:/case",
            "-w",
            "/case",
            "-e",
            "PYTHONPATH=/repo/src",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
        ]
        for mount in mounts:
            args += ["-v", mount]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        ic.docker(*args, image, *command)

    def seed_gateway(self):
        sys.path.insert(0, str(self.repo / "tools/m2_close/m3live"))
        from write_exec_manifest import build, write

        (self.work / "config").mkdir()
        (self.work / "node-home").mkdir()
        (self.work / "config/production.env").write_text("# synthetic\n")
        config = {
            "environment": "env-a",
            "snail_base_url": "http://127.0.0.1:8900",
            "snail_app_id": ic.APP_ID,
            "snail_app_token": ic.APP_TOKEN,
            "snail_agent_id": 1,
            "snail_open_id": ic.OPEN_ID,
            "expected_node_id": "node-a1",
            "nodes": [
                {
                    "node_id": "node-a1",
                    "environment": "env-a",
                    "token_sha256": hashlib.sha256(ic.NODE_TOKEN.encode()).hexdigest(),
                }
            ],
        }
        (self.work / "agent-runs.json").write_text(json.dumps(config))
        write(
            self.work / "manifest.json",
            build(model_id="m2close-model", endpoint="http://127.0.0.1:9100/v1", system_message="template"),
        )
        (self.work / "targets.json").write_text(
            json.dumps(
                {
                    "schema": "aiops-node-targets/1",
                    "targets": [
                        {
                            "app_id": APP,
                            "tenant_id": chain.TENANT,
                            "environment": "env-a",
                            "config_file": "/case/agent-runs.json",
                            "manifest_file": "/case/manifest.json",
                        }
                    ],
                }
            )
        )
        database = self.work / "gateway.db"
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
        assert _seed_workspace(database, chain.TENANT, "workspace-local", "platform-publication")
        assert _seed_agent(database, chain.TENANT)
        with sqlite3.connect(self.work / "orders.db") as c:
            c.execute(
                "CREATE TABLE synthetic_orders(order_no TEXT,user_id TEXT,tenant_id TEXT,"
                "created_time TEXT,id TEXT)"
            )
            c.execute(
                "INSERT INTO synthetic_orders VALUES(?,?,?,?,?)",
                (chain.ORDER_LATEST, chain.C_USER, chain.TENANT, "2026-10-24", "1"),
            )
        for p in self.work.rglob("*"):
            if p.is_file():
                p.chmod(0o600)
        return {
            "HOME": "/case",
            "AIOPS_DATA_HOME": "/case",
            "AIOPS_CONFIG_HOME": "/case/config",
            "AIOPS_GATEWAY_DATA_HOME": "/case",
            "AIOPS_GATEWAY_DATABASE_FILE": "/case/gateway.db",
            "AIOPS_GATEWAY_SERVER_CONFIG_FILE": "/case/config/production.env",
            "AIOPS_GATEWAY_BIND": "127.0.0.1",
            "AIOPS_GATEWAY_PORT": "8787",
            "AIOPS_GATEWAY_ENVIRONMENT": "platform-publication",
            "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT": "env-a",
            "AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED": "1",
            "AIOPS_GATEWAY_CHAT_NODE_TARGETS_FILE": "/case/targets.json",
            "AIOPS_GATEWAY_CHAT_ROUTE_BINDINGS_FILE": "/case/gateway.db",
            "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL": "http://127.0.0.1:9200/charging-pile",
            "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL": "http://127.0.0.1:9202",
            "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY": "synthetic",
            "AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY": PUBLISH,
            "AIOPS_GATEWAY_DIFY_TRIAL_API_KEY": TRIAL,
            "AIOPS_GATEWAY_DIFY_TRIAL_DEBUG_TENANT": chain.TENANT,
            "AIOPS_M2_LOCAL_TOKEN": chain.TOKEN,
            "AIOPS_M2_ORDERS_DB": "/case/orders.db",
            "AIOPS_M2_TENANT": chain.TENANT,
            "AIOPS_M2_C_USER": chain.C_USER,
            "AIOPS_M2_PLATFORM_CONSUMER": "1",
        }

    def call(self, path, *, method="GET", token=None, body=None, port=8787):
        script = (
            "import sys,json;sys.path.insert(0,'/repo/tools/m2_close/m3node');"
            "from verify_chat_node_entry import http;print(json.dumps(http(**json.load(sys.stdin))))"
        )
        payload = {
            "url": f"http://127.0.0.1:{port}" + path,
            "method": method,
            "token": token,
            "body": body,
            "headers": {"X-Business-Entry": "consumer"},
            "timeout": 10,
        }
        status, text = json.loads(
            ic.docker("exec", "-i", ic.GATEWAY, "python", "-c", script, stdin=json.dumps(payload)).stdout
        )
        return status, json.loads(text) if text else {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", type=Path, required=True)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    lock = harness_guard.acquire()
    root = Path(tempfile.mkdtemp(prefix="m3-platform-"))
    stack = Stack(chain.REPO, args.platform.resolve(), args.node.resolve(), args.image, root)
    report = {
        "root": str(root),
        "checks": [],
        "scope": "real platform/gRPC/packaged node/Gateway; synthetic editor/model/CPMS/identity",
    }
    for name in (ic.MYSQL, ic.SNAIL, ic.GATEWAY, ic.NODE, ic.STUB, ic.DRIVER):
        if ic.docker("inspect", name, check=False).returncode == 0:
            raise RuntimeError("existing experiment container: " + name)
    if ic.docker("network", "inspect", ic.NETWORK, check=False).returncode == 0:
        raise RuntimeError("existing experiment network")
    registry = json.loads((chain.REPO / "deploy/m2-live-03/images.lock.json").read_text())
    for label, path in (("platform", args.platform), ("node", args.node)):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == registry[label]["sha256"], label + " artifact differs from lock"
        report[label + "_sha256"] = digest
    report["gateway_image_id"] = ic.docker(
        "image", "inspect", args.image, "--format", "{{.Id}}"
    ).stdout.strip()
    for image in ("mysql:8.0", "eclipse-temurin:21-jdk"):
        identity = ic.docker("image", "inspect", image, "--format", "{{.Id}}").stdout.strip()
        assert identity == registry["base_images"]["refs"][image]["image_id"], "base image drift"
    before = harness_guard.code_identity(chain.REPO)
    before["files"][str(Path(__file__).relative_to(chain.REPO))] = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    report["code_identity"] = before
    try:
        stack.start()

        def check(name, ok):
            report["checks"].append({"name": name, "ok": bool(ok)})
            assert ok, name

        def operation(name, **extra):
            code, value = stack.call(
                "/v1/dify/" + name, method="POST", token=PUBLISH, body={"app_id": APP, **extra}
            )
            check(name, code == 200)
            return value

        def ask(prompt, *, failure=False):
            start = time.monotonic()
            code, value = stack.call(
                "/v1/assistant/questions",
                method="POST",
                token=chain.TOKEN,
                body={"question": "查我最新一单状态"},
            )
            check("App returns 202 promptly", code == 202 and time.monotonic() - start < 3)
            qa = value["qa_id"]
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                code, value = stack.call("/v1/assistant/questions/" + qa, token=chain.TOKEN)
                if value.get("status") in {"completed", "failed"}:
                    break
                time.sleep(0.3)
            if failure:
                check(
                    "upstream failure has no answer",
                    value.get("status") == "failed" and not value.get("result"),
                )
                return qa
            check(
                "App delivers same order",
                code == 200
                and value.get("status") == "completed"
                and chain.ORDER_LATEST in json.dumps(value),
            )
            with sqlite3.connect(root / "gateway.db") as c:
                row = c.execute(
                    "SELECT expected_agent_version,observed_agent_version,exec_config_state,"
                    "actual_node_id FROM agent_runs WHERE qa_id=?",
                    (qa,),
                ).fetchone()
            check(
                "packaged node attests publication",
                row and row[0] == row[1] and row[2:] == ("matched", "node-a1"),
            )
            _, evidence = stack.call("/evidence", port=9202)
            check("model consumes published SOP", prompt in json.dumps(evidence["model"]["prompts"][-2:]))
            return qa

        first = operation("publish")
        ask("SOP_VERSION_ONE")
        stack.call("/draft", method="POST", port=9202, body={"prompt": "SOP_VERSION_TWO"})
        ask("SOP_VERSION_ONE")
        code, value = stack.call(
            "/v1/dify/trial", method="POST", token=TRIAL, body={"app_id": APP, "order_no": chain.ORDER_LATEST}
        )
        check("trial accepted", code == 202)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            code, value = stack.call("/v1/dify/trial/" + value["trial_id"], token=TRIAL)
            if value.get("status") in {"completed", "failed"}:
                break
            time.sleep(0.3)
        check("draft trial completes through platform", value.get("status") == "completed")
        _, evidence = stack.call("/evidence", port=9202)
        check("trial consumes V2 draft", "SOP_VERSION_TWO" in json.dumps(evidence["model"]["prompts"][-2:]))
        ask("SOP_VERSION_ONE")
        operation("publish")
        qa = ask("SOP_VERSION_TWO")
        operation("retract")
        code, _ = stack.call(
            "/v1/assistant/questions", method="POST", token=chain.TOKEN, body={"question": "查我最新一单状态"}
        )
        check("retracted refuses new question", code == 410)
        code, _ = stack.call("/v1/assistant/questions/" + qa, token=chain.TOKEN)
        check("retracted hides historical body", code == 410)
        operation("restore", version_no=first["version_no"])
        ask("SOP_VERSION_ONE")
        stack.call(
            "/draft", method="POST", port=9202, body={"prompt": "SOP_VERSION_TWO", "mode": "round2-500"}
        )
        ask("SOP_VERSION_ONE", failure=True)
        _, report["evidence"] = stack.call("/evidence", port=9202)
        report["passed"] = True
    except Exception as exc:
        report.update(passed=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        for name in (ic.SNAIL, ic.NODE, ic.GATEWAY, ic.STUB):
            (root / (name + ".log")).write_text(ic.logs(name))
        for name in (ic.NODE, ic.GATEWAY, ic.STUB, ic.SNAIL, ic.MYSQL):
            ic.docker("rm", "-f", name, check=False)
        ic.docker("network", "rm", ic.NETWORK, check=False)
        after = harness_guard.code_identity(chain.REPO)
        after["files"][str(Path(__file__).relative_to(chain.REPO))] = hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest()
        report["code_drift"] = harness_guard.identity_drift(before, after)
        if report["code_drift"]:
            report["passed"] = False
        harness_guard.release(lock)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(
            json.dumps(
                {k: v for k, v in report.items() if k not in {"evidence", "code_identity"}},
                ensure_ascii=False,
            )
        )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--fixtures"]:
        fixtures()
    else:
        raise SystemExit(main())
