#!/usr/bin/env python3
"""M2-CLOSE-01 隔离环境验收：真 Snail 服务端 + 真执行节点 + 真 Gateway 代码。

它证明的是**链路**，不是模型质量。拓扑（全部在本机 Docker 内网）：

    driver ──HTTP──► gateway ──OpenAPI──► snail server ──gRPC──► node
                        ▲   │                                      │
                        │   └──HTTP──► cpms stub (:9200)           │
                        └──────────── 领取（节点凭据）◄────────────┘

被替换的是模型、业务上游、调用者身份与订单授权源，报告逐项注明。
Snail 服务端、执行节点、Gateway 的授权与生命周期全部是真代码。

预置产物（脚本不构建、不下载）：
  SNAIL_SRC   平台源码 checkout（需含 snail-ai-starter/target/snail-ai-server-exec.jar）
  NODE_JAR    执行节点 jar（services/execution-node 构建产物）

退出码 0=PASS，1=有判据失败，2=环境不足，3=另一个实验正在运行（未做任何清理）。

实验的容器/网络名是固定的，两份同时跑会互相删除对方的容器。脚本因此在**任何 Docker 调用
之前**拿一把跨工作树的 flock；拿不到就报 ``experiment busy`` 并退出。
报告里的 ``code_identity`` 是本进程实际加载的源文件与节点 jar 的摘要，开始与结束各采一次；
两次不一致即判 FAIL（运行期间代码变了，这次结果不能作数）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 同目录的公共设施（互斥锁与代码身份）。脚本按路径执行，所以把自身目录放进搜索路径，
# 而不是依赖 `tools` 是一个可导入的包。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_guard  # noqa: E402

NETWORK = "m2close"
MYSQL, SNAIL, GATEWAY, NODE, STUB, DRIVER = (
    "m2close-mysql",
    "m2close-snail",
    "m2close-gateway",
    "m2close-node",
    "m2close-stub",
    "m2close-driver",
)
MYSQL_PASSWORD = "m2closepw"
CRYPTO_KEY = "0123456789abcdef0123456789abcdef"
CRYPTO_IV = "fedcba9876543210fedcba9876543210"
APP_ID = "aiops-m2close"
APP_TOKEN = "SAI_m2close_app_token"
AGENT_ID = 1
OPEN_ID = "m2close-open-id"
LOCAL_TOKEN = "m2close-local-token"
#: 第二个调用者（只用于「两个用户交错」那条判据）。
SECOND_TOKEN = "m2close-second-token"
NODE_TOKEN = "m2close-node-token"
C_USER = "USER-A"
ORDERS = {
    "ORDER-A": {"user": C_USER, "status": "0"},
    "ORDER-C": {"user": C_USER, "status": "1"},
    "ORDER-B": {"user": "USER-B", "status": "0"},
}


def docker(*args: str, check: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, input=stdin)


def logs(name: str) -> str:
    result = docker("logs", name, check=False)
    return (result.stdout or "") + (result.stderr or "")


def wait_for(predicate: Callable[[], bool], description: str, timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(1.5)
    raise RuntimeError(f"等待超时: {description}")


class Stack:
    def __init__(self, repo: Path, snail_src: Path, node_jar: Path) -> None:
        # docker -v 需要绝对路径；调用方传相对路径时也必须能跑（本地调试就是这样）。
        self.repo = repo.resolve()
        self.snail_src = snail_src.resolve()
        self.node_jar = node_jar.resolve()
        self.work = self.repo / ".m2close-run"

    # ------------------------------------------------------------------ start
    def start(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True)
        self._seed_files()
        for name in (DRIVER, STUB, NODE, GATEWAY, SNAIL, MYSQL):
            docker("rm", "-f", name, check=False)
        docker("network", "rm", NETWORK, check=False)
        docker("network", "create", NETWORK)
        self._start_mysql()
        self._start_snail()
        self._start_stub()
        self._start_gateway()
        self._start_node()
        self._start_driver()
        self._wait_ready()

    def _seed_files(self) -> None:
        (self.work / "nodes.json").write_text(json.dumps(ORDERS))
        (self.work / "config").mkdir(exist_ok=True)
        production = self.work / "config/production.env"
        production.write_text("# synthetic; every external dependency is stubbed in-run\n")
        production.chmod(0o600)
        agent_runs = self.work / "agent-runs.json"
        agent_runs.write_text(
            json.dumps(
                {
                    "environment": "env-a",
                    "snail_base_url": "http://127.0.0.1:8900",
                    "snail_app_id": APP_ID,
                    "snail_app_token": APP_TOKEN,
                    "snail_agent_id": AGENT_ID,
                    "snail_open_id": OPEN_ID,
                    "expected_node_id": "node-a1",
                    "nodes": [
                        {
                            "node_id": "node-a1",
                            "environment": "env-a",
                            "token_sha256": hashlib.sha256(NODE_TOKEN.encode()).hexdigest(),
                        }
                    ],
                }
            )
        )
        agent_runs.chmod(0o600)
        import sqlite3

        connection = sqlite3.connect(self.work / "gateway-orders.db")
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS synthetic_orders "
                "(order_no TEXT PRIMARY KEY, user_id TEXT NOT NULL, tenant_id TEXT NOT NULL)"
            )
            for order_no, record in ORDERS.items():
                connection.execute(
                    "INSERT OR REPLACE INTO synthetic_orders VALUES (?, ?, 'TENANT-A')",
                    (order_no, record["user"]),
                )
            connection.commit()
        finally:
            connection.close()

    def _start_mysql(self) -> None:
        docker(
            "run",
            "-d",
            "--name",
            MYSQL,
            "--network",
            NETWORK,
            "--cpus",
            "1",
            "--memory",
            "700m",
            "-e",
            f"MYSQL_ROOT_PASSWORD={MYSQL_PASSWORD}",
            "-e",
            "MYSQL_DATABASE=snail_ai",
            "mysql:8.0",
            "--default-authentication-plugin=mysql_native_password",
            "--character-set-server=utf8mb4",
            "--collation-server=utf8mb4_unicode_ci",
            "--lower_case_table_names=1",
        )
        wait_for(
            lambda: (
                "alive"
                in docker(
                    "exec", MYSQL, "mysqladmin", "ping", "-uroot", f"-p{MYSQL_PASSWORD}", check=False
                ).stdout
            ),
            "mysql",
        )
        schema = (self.snail_src / "script/sql/snail_ai_schema.sql").read_text()
        docker("exec", "-i", MYSQL, "mysql", "-uroot", f"-p{MYSQL_PASSWORD}", "snail_ai", stdin=schema)
        # `sai_model_config.api_key` 是 SM4 密文（`CryptoHelper` 用配置里的 key/iv 解密）。
        # 明文或空串会让服务端在 `ModelResolveHandler` 抛 CryptoException —— 这里用一个
        # 用同一把 key/iv 加密的合成值，模型桩并不校验它。
        seed = f"""
INSERT INTO sai_model_config
 (provider_id, model_name, model_key, model_type, adapter_key, api_key, api_endpoint,
  is_default, is_enabled, scope)
 SELECT id, 'm2close', 'm2close-model', 'CHAT', 'openai-compatible',
        'Yjz/mfR6PGWOQ7UEXGS81w==',
        'http://m2close-stub:9100/v1', 1, 1, 'GLOBAL'
   FROM sai_model_provider WHERE provider_key = 'openai';
INSERT INTO sai_app (app_id, app_name, token, route_strategy, status)
 VALUES ('{APP_ID}', '{APP_ID}', '{APP_TOKEN}', 'LEAST_LOAD', 1);
INSERT INTO sai_agent (id, name, instruction, chat_model_id, status, app_id, creator_id, memory_enabled)
 SELECT {AGENT_ID}, 'm2close-agent',
        '用 charging_status_snapshot 工具取一次快照，并原样作答。',
        id, 1, '{APP_ID}', 1, 0
   FROM sai_model_config WHERE model_key = 'm2close-model';
INSERT INTO sai_openapi_user (app_id, open_id, platform_user_id, external_id)
 VALUES ('{APP_ID}', '{OPEN_ID}', 1, '1');
INSERT INTO sai_user_agent (user_id, agent_id) VALUES (1, {AGENT_ID});
"""
        docker("exec", "-i", MYSQL, "mysql", "-uroot", f"-p{MYSQL_PASSWORD}", "snail_ai", stdin=seed)

    def _start_snail(self) -> None:
        docker(
            "run",
            "-d",
            "--name",
            SNAIL,
            "--network",
            NETWORK,
            "--cpus",
            "2",
            "--memory",
            "1500m",
            "-v",
            f"{self.snail_src}:/workspace:ro",
            "eclipse-temurin:21-jdk",
            "java",
            "-jar",
            "/workspace/snail-ai-starter/target/snail-ai-server-exec.jar",
            "--spring.datasource.url=jdbc:mysql://m2close-mysql:3306/snail_ai"
            "?useSSL=false&allowPublicKeyRetrieval=true&characterEncoding=utf8&useUnicode=true"
            "&allowMultiQueries=true",
            "--spring.datasource.username=root",
            f"--spring.datasource.password={MYSQL_PASSWORD}",
            f"--snail-ai.crypto.secret-key={CRYPTO_KEY}",
            f"--snail-ai.crypto.iv={CRYPTO_IV}",
        )
        wait_for(lambda: "Started SnailAiApplication" in logs(SNAIL), "snail server")

    def _start_stub(self) -> None:
        docker(
            "run",
            "-d",
            "--name",
            STUB,
            "--network",
            NETWORK,
            "--cpus",
            "1",
            "--memory",
            "600m",
            "-v",
            f"{self.repo}/tools/m2_close:/work:ro",
            "-v",
            f"{self.work}:/work-data:ro",
            "-e",
            "M2CLOSE_MODEL_ONLY=1",
            "-e",
            f"M2CLOSE_SECRET_LOCAL={LOCAL_TOKEN}",
            "-e",
            f"M2CLOSE_SECRET_SECOND={SECOND_TOKEN}",
            "-e",
            f"M2CLOSE_SECRET_NODE={NODE_TOKEN}",
            "-e",
            f"M2CLOSE_SECRET_APP={APP_TOKEN}",
            "python:3.13-slim",
            "python",
            "/work/synthetic_upstream.py",
        )
        wait_for(lambda: "model stub" in logs(STUB), "synthetic upstream", timeout=90)

    def _start_gateway(self) -> None:
        env = {
            "AIOPS_DATA_HOME": "/data",
            "AIOPS_CONFIG_HOME": "/data/config",
            "HOME": "/data",
            "AIOPS_GATEWAY_DATA_HOME": "/data",
            "AIOPS_GATEWAY_BIND": "0.0.0.0",
            "AIOPS_GATEWAY_PORT": "8787",
            "AIOPS_GATEWAY_SERVER_CONFIG_FILE": "/data/config/production.env",
            "AIOPS_GATEWAY_DATABASE_FILE": "/data/gateway.db",
            "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL": "http://127.0.0.1:9200/charging-pile",
            "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT": "env-a",
            "AIOPS_GATEWAY_AGENT_RUN_CONFIG_FILE": "/data/agent-runs.json",
            "AIOPS_M2_LOCAL_TOKEN": LOCAL_TOKEN,
            "AIOPS_M2_SECOND_TOKEN": SECOND_TOKEN,
            "AIOPS_M2_SECOND_C_USER": "USER-B",
            "AIOPS_M2_ORDERS_DB": "/data/gateway-orders.db",
            "AIOPS_M2_TENANT": "TENANT-A",
            "AIOPS_M2_C_USER": C_USER,
        }
        args = [
            "run",
            "-d",
            "--name",
            GATEWAY,
            "--network",
            NETWORK,
            "--cpus",
            "1",
            "--memory",
            "900m",
            "-v",
            f"{self.repo}:/repo:ro",
            "-v",
            f"{self.work}:/data",
            "-w",
            "/repo",
            "-u",
            f"{os.getuid()}:{os.getgid()}",
        ]
        for key, value in env.items():
            args.extend(["-e", f"{key}={value}"])
        args.extend(
            [
                "python:3.13-slim",
                "sh",
                "-c",
                # 依赖装到 /data（root 拥有的镜像层写不进去）；CPMS 桩与 Gateway **同容器**，
                # 因为 `canonical_gateway_url` 只允许对回环地址用明文 HTTP。
                "pip install -q --no-cache-dir --target /data/site-packages fastapi "
                "'python-dotenv<2' 'pymysql[rsa]' 'redis<7' 'uvicorn<1' 'cryptography<47' "
                "'pyyaml<7' 'typer<1' 'rich<15' 'keyring<26' 'httpx<1' "
                "; "
                "M2CLOSE_ORDERS_FILE=/data/nodes.json "
                "M2CLOSE_FORWARD=m2close-snail:8900 "
                "python /repo/tools/m2_close/synthetic_upstream.py & "
                "PYTHONPATH=/repo/src:/data/site-packages "
                "exec python /repo/tools/m2_close/gateway_launcher.py",
            ]
        )
        docker(*args)
        wait_for(lambda: "substituted" in logs(GATEWAY), "gateway launcher", timeout=300)

    def _start_node(self) -> None:
        home = self.work / "node-home"
        home.mkdir(exist_ok=True)
        docker(
            "run",
            "-d",
            "--name",
            NODE,
            "--network",
            NETWORK,
            "--cpus",
            "1",
            "--memory",
            "900m",
            "-v",
            f"{self.node_jar.parent}:/app:ro",
            "-v",
            f"{home}:/home/node",
            "-w",
            "/home/node",
            "-u",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            "AIOPS_NODE_NAME=node-a1",
            "-e",
            "AIOPS_NODE_BUILD=m2close-node-1",
            "-e",
            "AIOPS_NODE_GRPC_PORT=18889",
            "-e",
            "AIOPS_NODE_HOST=m2close-node",
            "-e",
            "AIOPS_NODE_HOME=/home/node",
            "-e",
            "SNAIL_SERVER_HOST=m2close-snail",
            "-e",
            f"SNAIL_AI_APP_ID={APP_ID}",
            "-e",
            f"SNAIL_AI_TOKEN={APP_TOKEN}",
            "-e",
            "AIOPS_GATEWAY_BASE_URL=http://m2close-gateway:8787",
            "-e",
            f"AIOPS_NODE_TOKEN={NODE_TOKEN}",
            "eclipse-temurin:21-jdk",
            "java",
            "-jar",
            f"/app/{self.node_jar.name}",
        )

    def _start_driver(self) -> None:
        docker(
            "run",
            "-d",
            "--name",
            DRIVER,
            "--network",
            NETWORK,
            "alpine:latest",
            "sh",
            "-c",
            "apk add --no-cache curl >/dev/null 2>&1; sleep 7200",
        )

    def _wait_ready(self) -> None:
        def registered() -> bool:
            output = docker(
                "exec",
                MYSQL,
                "mysql",
                "-uroot",
                f"-p{MYSQL_PASSWORD}",
                "--skip-column-names",
                "-e",
                f"SELECT app_id FROM snail_ai.sai_client_node WHERE app_id='{APP_ID}'",
                check=False,
            ).stdout
            return APP_ID in output

        wait_for(registered, "execution node registration")
        wait_for(
            lambda: (
                not docker("exec", DRIVER, "command", "-v", "curl", check=False).stderr
                and "curl" in docker("exec", DRIVER, "which", "curl", check=False).stdout
            ),
            "driver curl",
        )
        wait_for(
            lambda: (
                docker(
                    "exec",
                    DRIVER,
                    "curl",
                    "-sS",
                    "-m",
                    "5",
                    "http://m2close-gateway:8787/health",
                    check=False,
                ).stdout
                != ""
            ),
            "gateway health",
        )

    # ------------------------------------------------------------------ calls
    def _curl(
        self,
        url: str,
        *,
        method: str = "GET",
        token: str | None = None,
        headers: dict[str, str] | None = None,
        body: dict | None = None,
    ) -> tuple[int, Any]:
        # 每次调用一个独立输出文件：并发的两次领取如果共用一个文件，后写的会覆盖先写的，
        # 于是「两个请求各自的结果」变成同一个响应 —— IC-05 的并发证据会因此失真。
        out_path = f"/tmp/out-{uuid.uuid4().hex}.json"
        argv = [
            "exec",
            DRIVER,
            "curl",
            "-sS",
            "-m",
            "90",
            "-o",
            out_path,
            "-w",
            "%{http_code}",
            "-X",
            method,
        ]
        if token:
            argv.extend(["-H", f"Authorization: Bearer {token}"])
        for key, value in (headers or {}).items():
            argv.extend(["-H", f"{key}: {value}"])
        if body is not None:
            argv.extend(["-H", "Content-Type: application/json", "-d", json.dumps(body)])
        argv.append(url)
        status = docker(*argv, check=False).stdout.strip()
        raw = docker("exec", DRIVER, "cat", out_path, check=False).stdout
        try:
            return int(status or 0), json.loads(raw)
        except ValueError:
            return int(status or 0), {"raw": raw[:400]}

    def delegate(
        self, order_no: str, *, token: str | None = LOCAL_TOKEN, body: dict | None = None
    ) -> tuple[int, Any]:
        return self._curl(
            f"http://m2close-gateway:8787/v1/orders/{order_no}/charging-status/delegated",
            method="POST",
            token=token,
            headers={"X-Business-Entry": "consumer"},
            body={} if body is None else body,
        )

    def delegate_as(self, token: str, order_no: str) -> tuple[int, Any]:
        return self.delegate(order_no, token=token)

    def claim(self, run_id: str, *, token: str | None = NODE_TOKEN) -> tuple[int, Any]:
        return self._curl(
            f"http://m2close-gateway:8787/v1/internal/charging-status/claims/{run_id}",
            method="POST",
            token=token,
            body={},
        )

    def counts(self) -> dict[str, int]:
        """CPMS 计数在 Gateway 容器，模型计数在模型桩容器：两个进程各有一份计数器。

        按同名键**求和**，不是后者的字典覆盖前者 —— 后者会把「模型桩没跑过 CPMS」
        的 0 覆盖掉真实读数，让「负例零次取数」看起来成立（实测踩过一次）。
        """
        upstream = self._curl("http://m2close-gateway:9201/control/counts")[1]
        model = self._curl("http://m2close-stub:9201/control/counts")[1]
        merged: dict[str, int] = {}
        for source in (upstream, model):
            for key, value in source.items():
                if key == "max_in_flight":
                    # 峰值取最大值，不能相加：它是「同一时刻最多几个」而不是累计。
                    merged[key] = max(merged.get(key, 0), int(value))
                else:
                    merged[key] = merged.get(key, 0) + int(value)
        return merged

    def model_state(self) -> dict[str, Any]:
        return self._curl("http://m2close-stub:9201/control/state")[1]

    def revoke(self, run_id: str) -> None:
        docker(
            "exec",
            GATEWAY,
            "python",
            "-c",
            "import sqlite3,sys;c=sqlite3.connect('/data/gateway.db');"
            "c.execute(\"update agent_runs set state='revoked', reason='revoked_in_test' where run_id=?\","
            "(sys.argv[1],));c.commit()",
            run_id,
            check=False,
        )

    def deactivate_node(self) -> None:
        """把节点绑定表里的 active 改成 false（模拟节点被停用）。"""
        docker(
            "exec",
            GATEWAY,
            "python",
            "-c",
            "import json;from pathlib import Path;p=Path('/data/agent-runs.json');"
            "d=json.loads(p.read_text());"
            "[n.update(active=False) for n in d['nodes']];"
            "p.write_text(json.dumps(d));p.chmod(0o600)",
            check=False,
        )

    def set_delay(self, seconds: float) -> None:
        """让桩 CPMS 慢下来：并发测试需要两次领取真正**重叠**，不能靠顺序调用假装。"""
        self._curl(
            "http://m2close-gateway:9201/control/delay",
            method="POST",
            body={"seconds": seconds},
        )

    def set_mode(self, mode: str) -> None:
        self._curl("http://m2close-stub:9201/control/mode", method="POST", body={"mode": mode})

    def runs(self) -> dict[str, dict]:
        output = docker(
            "exec",
            GATEWAY,
            "python",
            "-c",
            "import sqlite3,json;c=sqlite3.connect('/data/gateway.db');"
            "c.row_factory=sqlite3.Row;"
            "print(json.dumps({r['run_id']:dict(r) for r in c.execute('select * from agent_runs')}))",
            check=False,
        ).stdout
        try:
            return json.loads(output)
        except ValueError:
            return {}

    def expire(self, run_id: str) -> None:
        """把一条记录的到期时间推到过去（模拟 180 秒窗口走完）。"""
        docker(
            "exec",
            GATEWAY,
            "python",
            "-c",
            "import sqlite3,sys,datetime;c=sqlite3.connect('/data/gateway.db');"
            "c.execute('update agent_runs set expires_at=? where run_id=?',"
            "((datetime.datetime.now(datetime.UTC)-datetime.timedelta(seconds=1)).isoformat(),sys.argv[1]));"
            "c.commit()",
            run_id,
            check=False,
        )

    def set_owner(self, order_no: str, user_id: str) -> None:
        docker(
            "exec",
            GATEWAY,
            "python",
            "-c",
            "import sqlite3,sys;c=sqlite3.connect('/data/gateway-orders.db');"
            "c.execute('update synthetic_orders set user_id=? where order_no=?',(sys.argv[1],sys.argv[2]));"
            "c.commit()",
            user_id,
            order_no,
            check=False,
        )

    def stop(self) -> None:
        for name in (DRIVER, STUB, NODE, GATEWAY, SNAIL, MYSQL):
            docker("rm", "-f", name, check=False)
        docker("network", "rm", NETWORK, check=False)
        shutil.rmtree(self.work, ignore_errors=True)


#: 本轮必须齐的编号。**少一个就不算通过** —— 之前的实现只要 prove 没抛异常就写 PASS，
#: 于是「只跑了 IC-07 且是 UNEXPECTED」也能输出 PASS 与退出码 0（R1-3 的复现）。
REQUIRED_CHECKS = (
    "IC-01",
    "IC-02",
    "IC-03",
    "IC-04",
    "IC-05",
    "IC-06",
    "IC-07",
    "IC-08",
    "IC-09",
    "IC-10",
)


def aggregate(checks: dict[str, str], *, error: str | None = None) -> tuple[str, list[str]]:
    """把逐条结果折成总判定。纯函数：不碰容器，反向测试可以直接驱动它。

    通过的**唯一**条件是：十个编号齐全、每条都以 ``PASS`` 开头、没有 ``UNEXPECTED``。
    任何一条不满足都是 FAIL 并给出原因；已通过的项原样保留，方便看进度。
    """
    reasons: list[str] = []
    # **执行抛出过异常就不可能是 PASS**，即使十个编号都已经被赋成 PASS。
    # 这是 R2-2 的复现形态：最后一个子断言失败时，前九项与第十项的主断言都已经写完了。
    if error:
        reasons.append(f"执行期间发生异常: {error}")
    missing = [name for name in REQUIRED_CHECKS if name not in checks]
    if missing:
        reasons.append("缺少编号: " + ", ".join(missing))
    for name, value in checks.items():
        if not str(value).startswith("PASS"):
            reasons.append(f"{name} 不是 PASS: {value}")
    unexpected = [name for name, value in checks.items() if "UNEXPECTED" in str(value)]
    if unexpected:
        reasons.append("出现 UNEXPECTED: " + ", ".join(sorted(unexpected)))
    unknown = sorted(set(checks) - set(REQUIRED_CHECKS))
    if unknown:
        # 多出来的编号不算失败，但要让读者看见它没有被计入判据。
        reasons.append("未登记的编号（已忽略）: " + ", ".join(unknown))
    return ("PASS" if not reasons else "FAIL"), reasons


def finalize(report: dict[str, Any]) -> dict[str, Any]:
    """写总判定与结束时间。**main 与 --aggregate-only 共用这一处。**

    单独抽出来是为了让反向测试能走真实 CLI、覆盖与 main 完全相同的收尾逻辑；
    两处各写一份的话，测试可以全绿而 main 仍把异常算成 PASS（R2-2 的形态）。
    """
    report["finished_at_utc"] = report.get("finished_at_utc") or datetime.now(UTC).isoformat()
    report["status"], report["status_reasons"] = aggregate(
        report.get("checks", {}), error=report.get("error")
    )
    # 运行期间代码变了：这次结果不能代表任何一个确定版本，即使十项都 PASS。
    drift = report.get("identity_drift") or []
    if drift:
        report["status"] = "FAIL"
        report["status_reasons"].append("运行期间代码身份发生变化: " + ", ".join(drift))
    return report


def prove(stack: Stack, checks: dict[str, str]) -> dict[str, Any]:
    stack.set_mode("NORMAL")

    # IC-01：真平台 + 真节点调用真实 M2 读取代码
    status, body = stack.delegate("ORDER-A")
    assert status == 200, (status, body)
    assert body["actual_node_id"] == "node-a1", body
    assert body["expected_environment"] == "env-a" and body["expected_app_id"] == APP_ID, body
    assert body["agent_version"] is None and body["agent_version_gap"] == "not_wired", body
    run_id = body["run_id"]
    row = stack.runs()[run_id]
    snapshot = json.loads(row["snapshot_json"])
    assert snapshot["source"] == "cpms.chargingInfo", snapshot
    assert snapshot["status_role"] == "charging", snapshot
    assert stack.counts()["charging_info"] >= 1, "首例必须真的打过桩 CPMS"
    # 模型请求里只有受控工具；工具结果的字段范围是 7 + 执行位置（R1-4 / IC-07、IC-08）。
    state = stack.model_state()
    assert state["model_tool_sets"], "模型桩必须看到过请求"
    assert all(name == ["charging_status_snapshot"] for name in state["model_tool_sets"]), state[
        "model_tool_sets"
    ]
    checks["IC-01"] = "PASS：真 Snail 服务端与真节点调用真实 M2 读取代码（业务上游为桩）"

    # IC-02：不建记录、不调平台、不读业务
    before = len(stack.runs())
    before_counts = stack.counts()
    assert stack.delegate("ORDER-A", token=None)[0] == 401, "无凭据必须 401"
    assert stack.delegate("ORDER-A", token="wrong")[0] == 401, "错凭据必须 401"
    assert stack.delegate("ORDER-B")[0] == 404, "他人订单必须 404"
    assert stack.delegate("ORDER-MISSING")[0] == 404, "不存在必须 404"
    assert len(stack.runs()) == before, "负例不得建授权记录"
    assert stack.counts()["charging_info"] == before_counts["charging_info"], "负例不得读业务"
    assert stack.counts()["model_calls"] == before_counts["model_calls"], "负例不得调平台"
    checks["IC-02"] = "PASS：无身份、他人、不存在的订单都不建授权/不调平台/不读业务"

    # IC-03：篡改与错凭据
    for payload in (
        {"appId": "aiops-beta"},
        {"agentId": 9},
        {"environment": "env-b"},
        {"sid": "run_" + "a" * 40},
    ):
        assert stack.delegate("ORDER-A", body=payload)[0] == 422, payload
    assert stack.claim(run_id, token=None)[0] == 401, "无凭据领取必须 401"
    assert stack.claim(run_id, token="not-a-node-token")[0] == 401, "错凭据领取必须 401"
    # 旧票据口（原稿里的推票端口）不在本期契约里：必须是 404，而不是「碰巧没人用」。
    assert stack._curl(f"http://m2close-gateway:8787/v1/internal/charging-status/tickets/{run_id}")[0] == 404
    checks["IC-03"] = "PASS：篡改字段 422；只持 run_id/错凭据 401；旧票据口 404"

    def latest_run(order_no: str) -> str:
        """取某张单最近一条授权记录的 run_id（SILENT 委派失败时响应体里没有它）。"""
        rows = [
            (value["created_at"], key) for key, value in stack.runs().items() if value["order_no"] == order_no
        ]
        assert rows, f"没有 {order_no} 的授权记录"
        return max(rows)[1]

    # IC-04：两个用户各自订单**交错**执行；同一用户的同单多轮
    # 用两个令牌（LOCAL_TOKEN → USER-A，SECOND_TOKEN → USER-B）建立两份记录，
    # 交错领取，核对 run_id / 冻结身份 / 快照 / 回执各归其主。
    stack.set_mode("SILENT")
    try:
        stack.delegate("ORDER-A", token=LOCAL_TOKEN)  # USER-A
        stack.delegate("ORDER-B", token=SECOND_TOKEN)  # USER-B（别人的单，但属于该用户本人）
    finally:
        stack.set_mode("NORMAL")
    run_a = latest_run("ORDER-A")
    run_b = latest_run("ORDER-B")
    assert run_a != run_b
    # 交错领取：先 B 再 A
    claim_b = stack.claim(run_b)
    claim_a = stack.claim(run_a)
    assert claim_a[0] == 200 and claim_b[0] == 200, (claim_a, claim_b)
    assert claim_a[1]["order_no"] == "ORDER-A" and claim_b[1]["order_no"] == "ORDER-B"
    rows = stack.runs()
    assert rows[run_a]["scope_fingerprint"] != rows[run_b]["scope_fingerprint"], (
        "两个用户的冻结身份指纹必须不同"
    )
    assert json.loads(rows[run_a]["snapshot_json"])["order_no"] == "ORDER-A"
    assert json.loads(rows[run_b]["snapshot_json"])["order_no"] == "ORDER-B"
    # 同一用户的同单多轮：两条记录、两个不同的 run_id，但身份指纹相同。
    stack.set_mode("SILENT")
    try:
        stack.delegate("ORDER-A", token=LOCAL_TOKEN)
        stack.delegate("ORDER-A", token=LOCAL_TOKEN)
    finally:
        stack.set_mode("NORMAL")
    same_order = [key for key, value in stack.runs().items() if value["order_no"] == "ORDER-A"]
    assert len(same_order) >= 3, same_order
    fingerprints = {stack.runs()[key]["scope_fingerprint"] for key in same_order}
    assert len(fingerprints) == 1, "同一用户的身份指纹必须一致"
    checks["IC-04"] = (
        "PASS：两个用户各自的订单交错领取，run_id/冻结身份/快照/回执各归其主；"
        "同一用户的同单多轮得到不同 run_id 而身份指纹相同"
    )

    # IC-05：**真正重叠**的两次 HTTP 领取（屏障 + 慢上游），不是顺序调用
    stack.set_mode("SILENT")
    try:
        stack.delegate("ORDER-A")
    finally:
        stack.set_mode("NORMAL")
    pending_run = latest_run("ORDER-A")
    first_before = stack.counts()["charging_info"]
    stack.set_delay(1.5)  # 让赢家读业务的那一段足够长，第二个请求必然落在其中
    results: list[tuple[int, Any]] = []
    barrier = threading.Barrier(2)

    def claim_once() -> None:
        barrier.wait()
        results.append(stack.claim(pending_run))

    workers = [threading.Thread(target=claim_once) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=120)
    stack.set_delay(0)
    assert len(results) == 2, results
    after = stack.counts()["charging_info"]
    statuses = sorted(status for status, _ in results)
    # 重叠成立：一次读业务、一次在它执行中被拒（409），或两次都拿到同一份快照。
    assert after == first_before + 1, f"并发领取必须只读一次业务: {first_before} -> {after}"
    assert statuses[0] == 200 and statuses[1] in (200, 409), results
    # **重叠的证据是那个 409**：它只可能在某次领取仍处于 running 时被拒。
    # 配合 1.5s 的上游延迟与屏障，两个请求必然落在同一窗口里，而不是一前一后。
    # （max_in_flight 不会到 2 —— 单次读取保证让第二个请求根本不进 CPMS，
    #  这是设计而不是缺陷；拿它当重叠证据会把「正确」读成「没并发」。）
    assert statuses == [200, 409], f"需要一次成功 + 一次执行中拒绝，实际 {statuses}: {results}"
    assert "already being executed" in json.dumps(results), results
    counts_now = stack.counts()
    assert counts_now["max_in_flight"] == 1, counts_now
    # 两个桩进程各报一次（Gateway 容器里的 CPMS 桩 + 模型桩容器），求和后至少是版本号本身。
    assert counts_now.get("stub_rev", 0) >= 3, f"桩版本不对：{counts_now}"
    # 丢响应后的重试：整份快照逐字段比较，且不再读一次业务
    winner = next(payload for status, payload in results if status == 200)
    retry = stack.claim(pending_run)
    assert retry[0] == 200 and retry[1] == winner, (retry, winner)
    assert stack.counts()["charging_info"] == after, "成功重试不得再读业务"
    checks["IC-05"] = "PASS：并发领取只读一次；丢响应重试返回逐字段相同的快照"

    # IC-07：对抗模式**逐一点名** shell 与 http_request
    # 一次只点名一个：Spring AI 在第一个未知工具上就中止，把两个塞进同一次响应会让
    # 第二个永远没机会被尝试，「它也被拒绝」就成了空话（实测踩过一次）。
    for attack, tool_name in (
        ("ADVERSARIAL", "shell"),
        ("ADVERSARIAL_HTTP", "http_request"),
        # 候选源码里真实存在的两个入口：`RagSearchTool` 与 `ReadSkillTool`。
        # 用真实名字，才证明「解析层确实没把它们带进来」，而不是我编了个不存在的东西。
        ("ADVERSARIAL_RAG", "rag_search"),
        ("ADVERSARIAL_SKILL", "read_skill"),
    ):
        stack.set_mode(attack)
        before_counts = stack.counts()
        status, payload = stack.delegate("ORDER-A")
        after_counts = stack.counts()
        assert status != 200, f"{attack} 仍返回 200（{payload}）"
        assert after_counts["charging_info"] == before_counts["charging_info"], f"{attack} 不得读业务"
        assert after_counts["adversarial_calls"] == before_counts["adversarial_calls"] + 1, after_counts
        # 拒绝必须由节点自己给出，而且要等这行日志落地：节点是在响应**之后**才打它的，
        # 立刻断言会在慢一点的机器上假红。
        needle = f"No ToolCallback found for tool name: {tool_name}"
        node_log = logs(NODE)
        deadline = time.monotonic() + 30
        while needle not in node_log and time.monotonic() < deadline:
            time.sleep(1)
            node_log = logs(NODE)
        assert needle in node_log, node_log[-1500:]
    stack.set_mode("NORMAL")
    checks["IC-07"] = (
        "PASS：对抗模型逐一点名 shell / http_request / rag_search / read_skill，"
        "均被节点拒绝（No ToolCallback found），每次业务读取次数均不变"
    )

    # IC-08：sid 精确一致；凭据、完整授权记录与 PII 不出现在任何一跳
    node_log = logs(NODE)
    gateway_log = logs(GATEWAY)
    snail_log = logs(SNAIL)
    # 节点日志里必须出现**这一次**的编号（只出现任意 run_ 不算证据）。
    assert run_id in node_log, "节点必须记录本次 run_id（来自 sid）"
    # 完整授权记录的唯一标记是 scope_fingerprint：它只该待在 Gateway 的库里。
    stored = stack.runs()[run_id]
    fingerprint = stored["scope_fingerprint"]
    for name, text in (("node", node_log), ("snail", snail_log)):
        assert fingerprint not in text, f"完整授权记录出现在 {name} 日志里"
    # 三把凭据 + 调用者令牌：扫每一跳的日志与**模型请求**。
    # 四把都扫三跳日志：第二用户令牌也是真实凭据，不能因为它只用于 IC-04 就不扫。
    for name, text in (("gateway", gateway_log), ("node", node_log), ("snail", snail_log)):
        for secret in (LOCAL_TOKEN, SECOND_TOKEN, NODE_TOKEN, APP_TOKEN):
            assert secret not in text, f"凭据出现在 {name} 日志里"
    if stack.model_state()["secret_hits"]:
        raise AssertionError("凭据出现在模型请求里")
    # 工具结果字段：**必须**至少观察到一次，空的列表不能让这条检查空跑通过。
    observed = stack.model_state()["tool_result_fields"]
    assert observed, "模型桩没有观察到任何工具结果 —— 字段范围检查会空跑通过"
    allowed = {
        "order_no",
        "status_code",
        "status_role",
        "observed_at",
        "source",
        "environment",
        "gateway_version",
        "actual_node_id",
    }
    for field_set in observed:
        assert set(field_set) <= allowed, f"工具结果字段超出约定: {field_set}"
    # 节点确实收到的是**本单**的结果：模型桩在工具结果里看到的 order_no 必须是 ORDER-A。
    result_orders = stack.model_state().get("tool_result_orders", [])
    assert "ORDER-A" in result_orders, result_orders
    for text in (json.dumps(snapshot), node_log):
        assert "DO-NOT-LEAK" not in text, "上游 PII 必须在裁剪层挡掉"
    checks["IC-08"] = (
        "PASS：本次 run_id 精确出现在节点日志；授权记录指纹不出现在节点/Snail 日志；"
        "四把凭据（含第二用户令牌）不在 Gateway/Snail/节点三跳日志与模型请求里；"
        "工具结果字段已观察到且不超出 8 个约定字段；上游 PII 未进入快照与节点日志"
    )

    # IC-06：过期、撤销、节点停用 / 期限在授权与读取期间到达
    stack.set_mode("SILENT")
    try:
        stack.delegate("ORDER-C")
    finally:
        stack.set_mode("NORMAL")
    expired_run = latest_run("ORDER-C")
    stack.expire(expired_run)
    expired = stack.claim(expired_run)
    assert expired[0] == 403 and expired[1]["error"]["code"] == "AGENT_RUN_NOT_ACTIVE", expired

    stack.set_mode("SILENT")
    try:
        stack.delegate("ORDER-A")
    finally:
        stack.set_mode("NORMAL")
    revoked_run = latest_run("ORDER-A")
    stack.revoke(revoked_run)
    revoked = stack.claim(revoked_run)
    assert revoked[0] == 403 and "order_no" not in revoked[1], revoked
    # 已有成功快照的记录被撤销后也不能再释放
    revoked_snapshot_run = None
    for key, value in stack.runs().items():
        if value["state"] == "succeeded" and value["order_no"] == "ORDER-A":
            revoked_snapshot_run = key
            break
    assert revoked_snapshot_run, "应有一条成功记录可用于撤销释放测试"
    stack.revoke(revoked_snapshot_run)
    for status_code, payload in (stack.claim(revoked_snapshot_run),):
        assert status_code == 403 and "order_no" not in payload, (status_code, payload)
    checks["IC-06"] = "PASS：到期与撤销（含已有成功快照的记录）都拒绝释放；重启不改写 running 由契约测试覆盖"

    # IC-09：授权变化后不放行（新委派与已有快照两条路都要拒）
    stack.set_owner("ORDER-A", "USER-ELSEWHERE")
    try:
        status, _ = stack.delegate("ORDER-A")
        assert status == 404, status
        already_done = None
        for key, value in stack.runs().items():
            if value["state"] == "succeeded" and value["order_no"] == "ORDER-A":
                already_done = key
        assert already_done, "需要一条已有成功快照的记录"
        code, payload = stack.claim(already_done)
        assert code == 403 and "order_no" not in payload, (code, payload)
    finally:
        stack.set_owner("ORDER-A", C_USER)
    checks["IC-09"] = "PASS：授权变化后新委派 404；已有成功快照的记录也不再释放"

    # IC-10：审计节点来自认证；节点停用后不再放行
    assert row["actual_node_id"] == "node-a1", row
    assert row["node_build"] == "m2close-node-1", row
    checks["IC-10"] = "PASS：实际节点与构建标识来自节点认证与节点声明，不是模型文本"

    # IC-06b：节点在读取之后被停用 —— 放行判定重读配置后必须拒绝
    stack.set_mode("NORMAL")
    status, fresh = stack.delegate("ORDER-A")
    assert status == 200, (status, fresh)
    stack.deactivate_node()
    code, payload = stack.claim(fresh["run_id"])
    assert code in (401, 403) and "order_no" not in payload, (code, payload)
    checks["IC-06"] = checks["IC-06"] + "；读取后节点被停用时也不再释放"

    return {"sample_receipt": body, "counts": stack.counts()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--aggregate-only",
        action="store_true",
        help="不启动任何容器，只对 --out 里已有的 checks 重算总判定（供聚合门的反向测试用）",
    )
    args = parser.parse_args(argv)

    if args.aggregate_only:
        existing = finalize(json.loads(args.out.read_text()))
        args.out.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n")
        print(f"{existing['status']}: {args.out}")
        for reason in existing["status_reasons"]:
            print("  - " + reason, file=sys.stderr)
        return 0 if existing["status"] == "PASS" else 1
    try:
        lock = harness_guard.acquire()
    except harness_guard.ExperimentBusy as exc:
        # 不碰任何容器、网络或数据：另一轮正在用它们。
        print(f"experiment busy（另一个实验持有 {exc}）；未做任何清理，稍后重试。", file=sys.stderr)
        return 3
    try:
        return _run(args, lock)
    finally:
        harness_guard.release(lock)


def _platform_identity(source: Path) -> dict[str, str]:
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    artifact = source / "snail-ai-starter/target/snail-ai-server-exec.jar"
    return {
        "platform_commit": commit,
        "platform_jar_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }


def _run(args: argparse.Namespace, lock: Any) -> int:
    del lock  # 只在 main() 里持有；这里只是把生命周期写清楚。
    repo = Path(__file__).resolve().parents[2]
    snail_src = Path(os.environ.get("SNAIL_SRC", "/tmp/snail-ai")).resolve()
    node_jar = Path(os.environ.get("NODE_JAR", "services/execution-node/target/aiops-execution-node.jar"))
    if not node_jar.is_absolute():
        node_jar = (repo / node_jar).resolve()
    missing = [str(path) for path in (snail_src, node_jar) if not path.exists()]
    if missing:
        print("环境不足：" + "，".join(missing), file=sys.stderr)
        return 2

    platform_identity = _platform_identity(snail_src)
    commit = platform_identity["platform_commit"]
    started = datetime.now(UTC)
    report: dict[str, Any] = {
        "kind": "m2_close_01_isolated_ic",
        "started_at_utc": started.isoformat(),
        "timestamp_utc": started.isoformat(),  # 兼容旧读者：这是**开始**时间，不是结束时间
        "finished_at_utc": None,
        "status": "FAIL",
        "synthetic_data": True,
        "real_model": False,
        "real_company_services": False,
        "platform": {"source": "gitee aizuda/snail-ai", "commit": commit},
        "artifacts": {
            # 仓库相对路径：公开报告里不放开发机的绝对路径。
            "node_jar": str(node_jar.relative_to(repo)) if node_jar.is_relative_to(repo) else node_jar.name,
            "node_jar_sha256": hashlib.sha256(node_jar.read_bytes()).hexdigest(),
        },
        # 每个被替换的外部依赖都写清楚「真身是什么」，不要只写一个笼统的 stub。
        "substituted": [
            "model (scripted stub: NORMAL / SILENT / ADVERSARIAL)",
            "company CPMS (loopback stub with call counting)",
            "caller identity (fixed token -> fixed consumer ScopeContext)",
            "order authorization source (SQLite synthetic_orders, same owner predicate)",
        ],
        "checks": {},
        "code_identity": harness_guard.code_identity(
            repo,
            extra={
                "node_jar_sha256": hashlib.sha256(node_jar.read_bytes()).hexdigest(),
                **platform_identity,
            },
        ),
    }
    stack = Stack(repo, snail_src, node_jar)
    try:
        stack.start()
        report.update(prove(stack, report["checks"]))
    except Exception as exc:  # noqa: BLE001 - 报告记录失败原因
        # 带上出错的那一行：裸的 `AssertionError:` 看不出是哪个判据断的，
        # 回交时等于又得把整轮实验重跑一遍才能定位。
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["error_at"] = f"{Path(frame.filename).name}:{frame.lineno} {frame.line}"
        report["traceback_tail"] = traceback.format_exc().strip().splitlines()[-6:]
    finally:
        if args.keep:
            print(f"保留实验栈；工作目录 {stack.work}", file=sys.stderr)
        else:
            stack.stop()
    # 结束再采一次身份：运行期间代码变了，这次结果就不能当作「该版本的证据」。
    after = harness_guard.code_identity(
        repo,
        extra={
            "node_jar_sha256": hashlib.sha256(node_jar.read_bytes()).hexdigest(),
            **_platform_identity(snail_src),
        },
    )
    report["code_identity_end"] = after
    drift = harness_guard.identity_drift(report["code_identity"], after)
    if drift:
        report["identity_drift"] = drift
    finalize(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"{report['status']}: {args.out}")
    for reason in report["status_reasons"]:
        print("  - " + reason, file=sys.stderr)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
