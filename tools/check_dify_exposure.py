#!/usr/bin/env python3
"""Dify 暴露面的段 2 / 段 3 探测（#583 / PRD #577）。

段 1（`tests/test_dify_exposure_registry.py`）在 CI 里证明"我们声明暴露的 == 路由表里
面向 Dify 的那些"。它证明不了两件事，这正是本脚本要补的两半：

* **段 2 — 运行时探测**：登记表声明的那几个端点，在**真的跑起来的**网关进程上，
  未授权访问会被拒、授权访问按只读语义返回。静态读路由表读不出这个。
* **段 3 — 网络面探测**：从 **Dify 所在宿主**的视角，登记集合**之外**的公司系统面
  不可达（没有数据库、没有 Redis、没有 kb-service / RAGFlow 端口）。

跑法与退出码
------------
```bash
# 段 2：本机 temp root 起真网关，逐个端点发请求
PYTHONPATH=src python tools/check_dify_exposure.py --runtime

# 段 3：在 Dify 宿主上探（给了 --host 才真跑）
PYTHONPATH=src python tools/check_dify_exposure.py --network --host yidong-36

# 什么都不做，只把计划打出来 ⇒ 退出码 2（**未取证**，不是通过）
python tools/check_dify_exposure.py
```

退出码与 `tools/verify_banner_car_lookup.py` 同一纪律：
`0` = 全部通过；`1` = 有检查失败；`2` = **未取证**。
把"只列了计划"读成"验过了"正是本脚本要防的那种假陈述。

纪律
----
段 2 起的是**一次性数据根**（`mktemp`）上的网关，绝不指向真实 `AIOPS_DATA_HOME`；
段 2 用的凭据是当场生成的随机值，**不落盘、不打印**。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "ops" / "dify-exposure-registry.md"

_ROW = re.compile(
    r"^\|\s*(?P<face>[^|]+?)\s*\|\s*`(?P<path>/[^`]*?)`\s*\|"
    r"\s*(?P<method>[A-Z]+)\s*\|\s*(?P<auth>[^|]*?)\s*\|\s*(?P<reason>[^|]*?)\s*\|\s*$",
    re.MULTILINE,
)

#: Dify 的容器名（compose 项目名 `deploy`，见 `deploy/dify-36/docker-compose.yaml`）。
#: `api` 是编排面本身；`ssrf_proxy` 是它真正的出网跳 —— 两个都探，因为"能到哪"这件事
#: 在两个位置上的答案可以不同。
DIFY_PROBE_CONTAINER = "deploy-api-1"
DIFY_EGRESS_CONTAINER = "deploy-ssrf_proxy-1"

#: 集合**之外**不得可达的东西。名字是给人读的，`(kind, target)` 决定怎么探。
#: 只列"从 Dify 那边不该到得了"的**公司数据面内部**，不列它自己的依赖（postgres/redis
#: 是它自己 compose 里的，本来就该在一个网里 —— 段 3 的自检正是探它）。
#:
#: `127.0.0.1` 在这里的含义是**容器自己的回环**（宿主上的 kb-service/RAGFlow 不在这）。
#: 想看宿主上的那几个端口要写宿主 IP —— 由 `--host-ip` 传进来，见 `forbidden_targets()`。
#:
#: **`api.mall.qushiyun.com` 不在这个集合里，这是有意的。** 它是公司**公网**网关
#: （我们网关的 `/v1/*` 挂在它后面），而 Dify 的外部知识库 endpoint 就该填它 ——
#: `deploy/dify-36/README.md` 把"目标是网关域名（走公网）"写成了白名单的**正确**填法。
#: 第一版探测把它误列进禁止集，跑出来两条"REACHABLE ⇒ 失败"——**错的是登记，不是生产**。
#: 要判定的是"那一跳必须落在我们的适配路由上"（路径级，段 1/段 2 的事），
#: 不是"这个域名不可达"（域名级，而且与设计相反）。
FORBIDDEN_LOCAL = (
    ("kb-service（知识检索上游）", "127.0.0.1:9380"),
    ("RAGFlow 内部 API", "127.0.0.1:19380"),
    ("生产库 MySQL", "192.168.1.45:3306"),
)


def forbidden_targets(host_ip: str = "") -> tuple[tuple[str, str, str], ...]:
    """(name, kind, target)。给了宿主 IP 就把它上面那两个内部端口也算进来。

    少了这一步，"从容器能不能到宿主上的 kb-service" 就没被问过 —— 而 `127.0.0.1`
    在容器里指它自己，问题会看起来被回答了（`BLOCKED`）却什么都没答。
    """
    rows = [(name, "tcp", target) for name, target in FORBIDDEN_LOCAL]
    if host_ip:
        rows += [
            ("kb-service（宿主回环，经宿主 IP）", "tcp", f"{host_ip}:9380"),
            ("RAGFlow 内部 API（宿主回环，经宿主 IP）", "tcp", f"{host_ip}:19380"),
        ]
    return tuple(rows)


def registry_rows() -> list[dict[str, str]]:
    """登记表声明的暴露面。与段 1 用**同一张正则**，形状是一处定义。"""
    return [m.groupdict() for m in _ROW.finditer(REGISTRY.read_text(encoding="utf-8"))]


# ── 段 2：真网关上的运行时探测 ──────────────────────────────────────────────


def _http(base: str, path: str, *, method: str = "GET", token: str | None, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{base}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — a probe reports, it does not traceback
        return 0, f"{type(exc).__name__}: {exc}"


def _payload_for(path: str, knowledge_id: str) -> dict:
    """A body the endpoint can actually parse — otherwise a 4xx proves nothing
    about authorization."""
    if path.endswith("/retrieval"):
        return {
            "retrieval_setting": {"top_k": 3},
            "query": "段 2 探测查询",
            "knowledge_id": knowledge_id,
        }
    return {}


def _free_port() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock, int(sock.getsockname()[1])


def run_runtime_segment(out_dir: Path) -> int:
    """段 2：真进程、真 HTTP。未授权必须被拒，授权必须按只读语义返回。"""
    rows = registry_rows()
    if not rows:
        print("FAIL: 登记表里解析不出任何端点 —— 先把段 1 的形状问题解决", file=sys.stderr)
        return 1

    root = Path(tempfile.mkdtemp(prefix="aiops-dify-exposure-"))
    settings_key = "exposure-probe-" + os.urandom(8).hex()  # 当场生成，不落盘、不打印
    knowledge_id = "kb-probe"
    env = {
        **os.environ,
        "AIOPS_HOME": str(root),
        "AIOPS_CONFIG_HOME": str(root / "config"),
        "AIOPS_DATA_HOME": str(root / "data"),
        "AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY": settings_key,
        "AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS": f"{knowledge_id}:T-PROBE:KB-PROBE",
        "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep),
    }
    evidence: dict[str, object] = {"segment": 2, "temp_root": str(root), "endpoints": []}
    failures: list[str] = []
    process = None
    sock, port = _free_port()
    base = f"http://127.0.0.1:{port}"

    try:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["uv", "run", "aiops", "init"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
        )
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            ["uv", "run", "aiops-gateway", "serve", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if _http(base, "/health", token=None)[0] == 200:
                break
            time.sleep(0.5)
        else:
            print("FAIL: 网关未就绪", file=sys.stderr)
            return 1
        del sock  # 端口已交给子进程

        for row in rows:
            record, row_failures = _probe_endpoint(base, row, knowledge_id, settings_key)
            evidence["endpoints"].append(record)
            failures.extend(row_failures)
    finally:
        if process is not None and process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        shutil.rmtree(root, ignore_errors=True)

    return _finish_segment(out_dir, "dify-exposure-runtime.json", "段 2", evidence, failures)


def _probe_endpoint(
    base: str, row: dict[str, str], knowledge_id: str, settings_key: str
) -> tuple[dict[str, object], list[str]]:
    """三次请求一个端点：不带凭据 / 带错凭据 / 带对凭据。返回(记录, 失败项)。"""
    path, method = row["path"], row["method"]
    body = _payload_for(path, knowledge_id)
    anonymous = _http(base, path, method=method, token=None, body=body)
    wrong = _http(base, path, method=method, token="not-the-key", body=body)
    authorized = _http(base, path, method=method, token=settings_key, body=body)
    record: dict[str, object] = {
        "path": path,
        "method": method,
        "anonymous_status": anonymous[0],
        "wrong_credential_status": wrong[0],
        "authorized_status": authorized[0],
        "authorized_body_head": authorized[1][:200],
    }
    failures: list[str] = []

    # 未授权必须被拒。404 也算「拒」的一种（那些依赖没配 ⇒ 路由整体不启用），
    # 但两者含义不同，所以放在同一个可接受集合里而不是混为一谈：
    # 401/403 是"凭据不对"，404 是"这个面没开"。
    if anonymous[0] not in (401, 403, 404):
        failures.append(f"{method} {path} 未授权返回 {anonymous[0]} —— 应当是 401/403/404")
    if wrong[0] not in (401, 403):
        failures.append(f"{method} {path} 错误凭据返回 {wrong[0]} —— 应当是 401/403")

    # 授权之后：200，或**一个明确的不可用**（这些端点在 temp root 上依赖是缺的）。
    # 后者只接受可枚举的两个错误码 —— 一个含糊的 5xx 不构成"鉴权对了"的证据。
    if authorized[0] == 200:
        pass
    elif authorized[0] == 502:
        try:
            code = json.loads(authorized[1])["error"]["code"]
        except Exception:  # noqa: BLE001
            code = ""
        if code != "DIFY_KNOWLEDGE_UNAVAILABLE":
            failures.append(f"{method} {path} 502 的错误码不是 DIFY_KNOWLEDGE_UNAVAILABLE（{code!r}）")
        record["note"] = "授权通过、依赖不可用 ⇒ 502 是设计内答案；本条证明的是鉴权与错误形状"
    elif authorized[0] == 503:
        try:
            code = json.loads(authorized[1])["error"]["code"]
        except Exception:  # noqa: BLE001
            code = ""
        record["note"] = f"授权通过、依赖未配置 ⇒ 503 {code}；本条证明的是鉴权"
    else:
        failures.append(f"{method} {path} 授权后返回 {authorized[0]} —— 应当是 200/502/503 之一")

    # 只读语义：授权后的返回体里不得出现任何"写入"痕迹。
    for marker in ("INSERT ", "UPDATE ", "DELETE "):
        if marker in authorized[1]:
            failures.append(f"{method} {path} 返回体里出现 {marker!r} —— 这个面必须是只读的")
    return record, failures


def _finish_segment(out_dir: Path, filename: str, label: str, evidence: dict, failures: list[str]) -> int:
    evidence["failures"] = failures
    evidence["status"] = "failed" if failures else "passed"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / filename).write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{label} 状态: {evidence['status']}  证据: {out_dir / filename}")
    for item in failures:
        print(f"  FAIL: {item}", file=sys.stderr)
    return 1 if failures else 0


# ── 段 3：从 **Dify 自己的进程** 的视角探网络面 ─────────────────────────────


def run_network_segment(host: str, out_dir: Path, *, host_ip: str = "") -> int:
    """段 3：从 **Dify 的容器里** 探「不该可达」的目标。

    ## 观察点为什么是容器，而不是宿主

    这一段的判据是「**Dify 能触达的公司系统面**」。在宿主上 connect 一次回答的是
    "宿主能到哪"，而 Dify 是**容器**：它的网络位置与宿主不同（`127.0.0.1` 在容器里
    是它自己），而且它的出网还会再经一层 `ssrf_proxy`（squid）。所以观察点必须是
    容器（`api` 与真正出网的 `ssrf_proxy` 两个都探）。

    实测（2026-10-09）这句话不是理论：**宿主**上 `127.0.0.1:9380` 与 `19380` 都是通的
    （kb-service 与 RAGFlow 就在这台机器上），而**容器**里两个都 `BLOCKED`。同一组目标、
    两个观察点、两个相反结论 —— 把宿主当观察点会得出"暴露了"，把容器当观察点才是
    判据要问的那件事。

    ## 探针的有效性也要自证

    `/dev/tcp` 是 bash 特性，而镜像里 `/bin/sh` 是 dash —— 不显式调 `bash -c`
    会得到一堆只报 `BLOCKED` 的假阴性。脚本先探一个**同网络内必然可达**的目标
    （Dify 自己的 redis），dev/ci 之外这一条证明探针本身是活的；它不可达就退 2（未取证），
    不把"探针坏了"当成"封锁成立"。
    """
    targets = forbidden_targets(host_ip)
    probe_body = "\n".join(f"probe {label!r} {kind!r} {target!r}" for label, kind, target in targets)
    remote = f"""
set -u
probe() {{
  label="$1"; kind="$2"; target="$3"
  thost="${{target%%:*}}"; port="${{target##*:}}"
  if timeout 4 bash -c "echo > /dev/tcp/$thost/$port" 2>/dev/null; then
    echo "REACHABLE|$label|$target"
  else
    echo "BLOCKED|$label|$target"
  fi
}}
echo "SELFTEST|$(bash -c "echo > /dev/tcp/redis/6379" 2>/dev/null && echo healthy || echo broken)"
{probe_body}
"""
    evidence: dict[str, object] = {
        "segment": 3,
        "vantage": f"Dify containers on {host} ({DIFY_PROBE_CONTAINER}, {DIFY_EGRESS_CONTAINER})",
        "forbidden": [{"name": name, "target": target} for name, _, target in targets],
        "observations": [],
        "containers": {},
        "self_test": {},
    }
    failures: list[str] = []

    for container in (DIFY_PROBE_CONTAINER, DIFY_EGRESS_CONTAINER):
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["ssh", host, f"docker exec -i {container} bash -s"],
            input=remote,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        if not result.stdout.strip():
            print(
                f"UNTAKEN: 无法在 {host} 的 {container} 里执行探测：{result.stderr.strip()[:200]}",
                file=sys.stderr,
            )
            return 2
        rows: list[dict[str, str]] = []
        self_test = "unknown"
        for line in result.stdout.splitlines():
            verdict, _, rest = line.partition("|")
            if verdict == "SELFTEST":
                self_test = rest.strip()
                continue
            if verdict not in {"REACHABLE", "BLOCKED"}:
                continue
            label, _, target = rest.partition("|")
            rows.append({"verdict": verdict, "name": label, "target": target})
        evidence["self_test"][container] = self_test  # type: ignore[index]
        evidence["containers"][container] = rows  # type: ignore[index]

        if self_test != "healthy":
            # 探针自己可能坏了（例如镜像换掉 bash）。那时 BLOCKED 全是假的。
            print(
                f"UNTAKEN: {container} 里的探针自检为 {self_test!r} —— 探不出去，不能报封锁",
                file=sys.stderr,
            )
            return 2
        for row in rows:
            if row["verdict"] == "REACHABLE":
                failures.append(
                    f"{row['name']}（{row['target']}）从 {container} 可达 —— 它在登记集合之外，不该可达"
                )

    return _finish_segment(out_dir, "dify-exposure-network.json", "段 3", evidence, failures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", default="var/dify-exposure", help="证据输出目录")
    parser.add_argument("--runtime", action="store_true", help="跑段 2（本机真网关）")
    parser.add_argument("--network", action="store_true", help="跑段 3（需 --host）")
    parser.add_argument("--host", default="", help="段 3 的入口：Dify 所在宿主")
    parser.add_argument("--host-ip", default="", help="段 3 额外探：宿主自身 IP 上的内部端口")
    args = parser.parse_args(argv)

    out_dir = (ROOT / args.out).resolve()
    if not (args.runtime or args.network):
        # 只列计划 —— **这不是通过**。
        rows = registry_rows()
        print(
            json.dumps(
                {
                    "status": "untaken",
                    "declared_endpoints": [{"path": r["path"], "method": r["method"]} for r in rows],
                    "segment_2": "python tools/check_dify_exposure.py --runtime",
                    "segment_3": "python tools/check_dify_exposure.py --network --host <dify-host>",
                    "forbidden_targets": [
                        {"name": name, "target": target} for name, _, target in forbidden_targets()
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print("UNTAKEN: 什么都没探 —— 这不是通过（退出码 2）", file=sys.stderr)
        return 2

    codes: list[int] = []
    if args.runtime:
        codes.append(run_runtime_segment(out_dir))
    if args.network:
        if not args.host:
            print("UNTAKEN: --network 需要 --host（Dify 所在的宿主）", file=sys.stderr)
            codes.append(2)
        else:
            codes.append(run_network_segment(args.host, out_dir, host_ip=args.host_ip))
    return max(codes) if codes else 2


if __name__ == "__main__":
    raise SystemExit(main())
