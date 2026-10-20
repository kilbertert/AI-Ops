"""清单预检的正面/反面判据：驱动**真实**的 `checks.sh preflight`。

为什么这份文件存在
------------------
旧判定只比两个路径的 `basename`，于是两种状态都印 PASS（Codex 实测，见
`preflight-edge-cases.json`）：

1. 节点开关 `AIOPS_EXEC_MANIFEST` **缺失** —— 节点根本没在读清单；
2. 节点路径是 `/not-mounted/exec-manifest.json` —— 没有任何挂载覆盖它。

两种都不是「两侧同一份」。本文件把这两条原样写成回归：在旧写法下它们必须转红。

判据怎么落到「同一份」
----------------------
不是看文件名，而是把**渲染后的 compose** 里两侧的容器内路径解析成宿主路径，再要求
指向同一个真实普通文件（同一 inode）。所以「两个目录里各放一份同名文件」也会被拒 ——
那是漂移的种子，而漂移要到领取时才以 `AGENT_RUN_EXEC_CONFIG_DRIFT` 的形式暴露。

环境
----
假 docker + 假 ss + 一份复制出来的部署目录（只为了把镜像/制品摘要改成测试能造出的值）。
没有真实 daemon、没有网络、没有生产路径。`preflight` 顶层的退出码被断言；
其余条款（内存余量）在本机不成立时，只允许它是唯一失败项，见 `_memory_available_mib`。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy/m2-live-03"
BASE_REFS = ("mysql:8.0", "eclipse-temurin:21-jdk")
GATEWAY_IMAGE = "aiops-m2-live-03-gateway:local"
IMAGE_ID = "sha256:" + "a" * 64


def _memory_available_mib() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def _fake_bin(tmp_path: Path) -> Path:
    """docker 与 ss 的替身。所有真实判定逻辑仍在 checks.sh / stack_checks.py 里。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['M2_TEST_DOCKER_LOG'], 'a', encoding='utf-8') as fh:\n"
        "    fh.write(' '.join(args) + '\\n')\n"
        "if args == ['version']:\n"
        "    sys.exit(0)\n"
        "if args[:2] == ['compose', 'version']:\n"
        "    print('Docker Compose version v2.27.0'); sys.exit(0)\n"
        "if 'compose' in args and 'config' in args:\n"
        "    if '--images' in args:\n"
        "        print(os.environ['M2_TEST_IMAGES']); sys.exit(0)\n"
        "    if '--format' in args and 'json' in args:\n"
        "        sys.stdout.write(open(os.environ['M2_TEST_COMPOSE_JSON'], encoding='utf-8').read())\n"
        "        sys.exit(0)\n"
        "    sys.exit(0)\n"
        "if args[:2] == ['image', 'inspect']:\n"
        "    fmt = ' '.join(args[2:])\n"
        "    key = 'M2_TEST_SOURCE' if 'm2live.source' in fmt else 'M2_TEST_IMAGE_ID'\n"
        "    print(os.environ[key])\n"
        "    sys.exit(0)\n"
        "sys.exit(0)\n"
    )
    docker.chmod(0o700)
    # `ss` 只说「没有监听」：端口占用与否不是本文件要验的东西，而宿主上是否恰好有
    # 8794 监听不该决定判据的结论。
    (bin_dir / "ss").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "ss").chmod(0o700)
    return bin_dir


def _compose_json(config: Path, state: Path, *, node_source: Path, gateway_source: Path) -> dict:
    """渲染后的 compose 投影：只保留本判据读的字段（volumes / user）。

    gateway 挂**目录**（`/etc/m2live`），node 挂**单个文件** —— 这与真实 compose.yaml
    的形状一致；判据因此必须经过「容器路径 → 挂载 → 宿主文件」这一步，而不是比文件名。
    """
    uid, gid = os.getuid(), os.getgid()
    users = {
        name: f"{uid}:{gid}" for name in ("platform", "node", "gateway", "gateway-host-relay", "model-proxy")
    }
    services: dict[str, dict] = {}
    for name, user in users.items():
        services[name] = {"user": user, "volumes": []}
    services["gateway"]["volumes"] = [
        {"type": "bind", "source": str(gateway_source), "target": "/etc/m2live", "read_only": True},
        {"type": "bind", "source": str(state / "gateway"), "target": "/data", "read_only": False},
    ]
    services["node"]["volumes"] = [
        {
            "type": "bind",
            "source": str(node_source),
            "target": "/etc/m2live/exec-manifest.json",
            "read_only": True,
        },
    ]
    services["platform"]["volumes"] = [
        {"type": "bind", "source": str(state / "platform"), "target": "/var/lib/snail-ai", "read_only": False}
    ]
    services["store"] = {"user": "root", "volumes": []}
    return {"services": services}


def _write_env_files(
    config: Path, *, gateway_manifest: str | None, node_manifest: str | None, manifest_text: str = "{}\n"
) -> None:
    for name in ("gateway", "node", "platform", "secrets", "model-proxy"):
        (config / name).mkdir(parents=True, exist_ok=True)
    gateway_lines = [
        "AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL=https://cpms.invalid",
        "AIOPS_GATEWAY_EXECUTION_ENVIRONMENT=live-trial-test",
        "AIOPS_GATEWAY_AGENT_RUN_CONFIG_FILE=/etc/m2live/agent-runs.json",
    ]
    if gateway_manifest is not None:
        gateway_lines.append(f"AIOPS_GATEWAY_EXECUTION_MANIFEST_FILE={gateway_manifest}")
    (config / "gateway/gateway.env").write_text("\n".join(gateway_lines) + "\n")
    node_lines = ["SNAIL_AI_APP_ID=app", "SNAIL_AI_TOKEN=t", "AIOPS_NODE_TOKEN=n"]
    if node_manifest is not None:
        node_lines.append(f"AIOPS_EXEC_MANIFEST={node_manifest}")
    (config / "node/node.env").write_text("\n".join(node_lines) + "\n")
    # 其余私有文件：只要求存在且 0600（本文件不验它们的内容）。
    for path in (
        config / "gateway/agent-runs.json",
        config / "platform/application.properties",
        config / "secrets/mysql_root",
    ):
        path.write_text(manifest_text)
        path.chmod(0o600)
    (config / "gateway/gateway.env").chmod(0o600)
    (config / "node/node.env").chmod(0o600)


@pytest.fixture
def deployment(tmp_path):
    """一份可以起整条 preflight 的最小部署：假 docker、复制出的部署目录、造得出的制品。"""
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    for name in ("checks.sh", "compose.yaml", "stack_checks.py"):
        shutil.copy2(DEPLOY / name, deploy / name)
    (deploy / "schema").mkdir()

    config = tmp_path / "etc"
    state = tmp_path / "state"
    dist = tmp_path / "dist"
    for path in (
        state / "store",
        state / "gateway",
        state / "node",
        state / "platform",
        dist / "platform",
        dist / "node",
    ):
        path.mkdir(parents=True, exist_ok=True)
    platform_jar = dist / "platform/snail-ai-server-exec.jar"
    node_jar = dist / "node/aiops-execution-node.jar"
    platform_jar.write_bytes(b"platform-jar")
    node_jar.write_bytes(b"node-jar")
    import hashlib

    lock = {
        "platform": {"sha256": hashlib.sha256(platform_jar.read_bytes()).hexdigest()},
        "node": {"sha256": hashlib.sha256(node_jar.read_bytes()).hexdigest()},
        "base_images": {"refs": {ref: {"image_id": IMAGE_ID} for ref in BASE_REFS}},
        "gateway_image": {
            "image": "aiops-m2-live-03-gateway",
            "source_sha256": "b" * 64,
            "uv_lock_sha256": "c" * 64,
        },
    }
    (deploy / "images.lock.json").write_text(json.dumps(lock))
    identity = dist / "gateway-image"
    identity.mkdir()
    (identity / "identity.json").write_text(json.dumps({"image_id": IMAGE_ID}))

    bin_dir = _fake_bin(tmp_path)
    compose_json = tmp_path / "compose.json"
    return {
        "tmp": tmp_path,
        "deploy": deploy,
        "config": config,
        "state": state,
        "dist": dist,
        "bin": bin_dir,
        "compose_json": compose_json,
        "lock": lock,
    }


def _run_preflight(ctx, *, node_source=None, gateway_source=None, extra_env=None):
    config, state = ctx["config"], ctx["state"]
    gateway_source = gateway_source or (config / "gateway")
    node_source = node_source or (config / "gateway/exec-manifest.json")
    ctx["compose_json"].write_text(
        json.dumps(_compose_json(config, state, node_source=node_source, gateway_source=gateway_source))
    )
    uid = str(os.getuid())
    env = {
        "PATH": f"{ctx['bin']}:/usr/bin:/bin",
        "M2LIVE_CONFIG": str(config),
        "M2LIVE_STATE": str(state),
        "M2LIVE_DIST": str(ctx["dist"]),
        "M2LIVE_UID": uid,
        "M2LIVE_GID": str(os.getgid()),
        "M2LIVE_MODEL_UPSTREAM": "https://supplier.invalid",
        "M2LIVE_MODEL_ID": "vendor-model",
        # 合成模式：不读生产配置。本文件验的是清单判据，不是生产开关。
        "M2LIVE_SYNTH_MODE": "1",
        "M2_TEST_DOCKER_LOG": str(ctx["tmp"] / "docker.log"),
        "M2_TEST_COMPOSE_JSON": str(ctx["compose_json"]),
        "M2_TEST_IMAGES": "\n".join((GATEWAY_IMAGE, *BASE_REFS)),
        "M2_TEST_IMAGE_ID": IMAGE_ID,
        "M2_TEST_SOURCE": "b" * 64,
    }
    env.update(extra_env or {})
    import subprocess

    # FAIL 行走 stderr（`bad()` 用红色打印），`== … ==` 节标题走 stdout。合并成一条流
    # 才能按出现顺序切节 —— 分开捕获会丢掉相对顺序。
    return subprocess.run(
        ["bash", str(ctx["deploy"] / "checks.sh"), "preflight"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120,
    )


def _manifest_section(output: str) -> list[str]:
    """输出里「M3 执行配置清单」那一节的各行（含 ok/FAIL）。"""
    lines = output.splitlines()
    start = next(index for index, line in enumerate(lines) if "M3 执行配置清单" in line)
    section: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("== "):
            break
        section.append(line)
    return section


def test_a_legal_deployment_passes_the_whole_preflight(deployment):
    """正对照：合法状态必须让 preflight 整体返回 0。

    唯一的例外是宿主余量（栈预算 2992 + 余量 900）。那一条与本片无关，且是**环境**
    事实而不是判据：余量不足时只允许它是唯一的失败项，并且明确记在断言信息里。
    """
    ctx = deployment
    _write_env_files(
        ctx["config"],
        gateway_manifest="/etc/m2live/exec-manifest.json",
        node_manifest="/etc/m2live/exec-manifest.json",
    )
    (ctx["config"] / "gateway/exec-manifest.json").write_text('{"schema": "m3-exec-config/1"}\n')
    (ctx["config"] / "gateway/exec-manifest.json").chmod(0o600)

    result = _run_preflight(ctx)
    section = _manifest_section(result.stdout)
    failed = [line for line in section if "FAIL" in line]
    assert not failed, "\n".join(section)
    assert any("同一个文件" in line for line in section), section

    if result.returncode != 0:
        failures = [line for line in result.stdout.splitlines() if "FAIL" in line]
        assert _memory_available_mib() < 3892, f"preflight 返回 {result.returncode}：\n" + "\n".join(failures)
        assert len(failures) == 1 and ("余量" in failures[0] or "预算" in failures[0]), failures


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_both_private_modes_are_accepted(deployment, mode):
    """契约允许 0400 与 0600。只认 0600 会把一个合法部署判成坏的。"""
    ctx = deployment
    _write_env_files(
        ctx["config"],
        gateway_manifest="/etc/m2live/exec-manifest.json",
        node_manifest="/etc/m2live/exec-manifest.json",
    )
    manifest = ctx["config"] / "gateway/exec-manifest.json"
    manifest.write_text('{"schema": "m3-exec-config/1"}\n')
    manifest.chmod(mode)

    result = _run_preflight(ctx)
    section = _manifest_section(result.stdout)
    assert not [line for line in section if "FAIL" in line], section
    assert any("同一个文件" in line for line in section), section


def test_a_missing_node_switch_is_rejected(deployment):
    """反例 1（旧写法印 PASS）：节点开关缺失。"""
    ctx = deployment
    _write_env_files(
        ctx["config"],
        gateway_manifest="/etc/m2live/exec-manifest.json",
        node_manifest=None,
    )
    (ctx["config"] / "gateway/exec-manifest.json").write_text("{}\n")
    (ctx["config"] / "gateway/exec-manifest.json").chmod(0o600)

    result = _run_preflight(ctx)
    section = _manifest_section(result.stdout)
    assert result.returncode != 0, "节点开关缺失却通过"
    assert any("FAIL" in line and "两侧必须同时启用" in line for line in section), section


def test_a_node_path_no_mount_provides_is_rejected(deployment):
    """反例 2（旧写法印 PASS）：路径指向没人挂载的地方。"""
    ctx = deployment
    _write_env_files(
        ctx["config"],
        gateway_manifest="/etc/m2live/exec-manifest.json",
        node_manifest="/not-mounted/exec-manifest.json",
    )
    (ctx["config"] / "gateway/exec-manifest.json").write_text("{}\n")
    (ctx["config"] / "gateway/exec-manifest.json").chmod(0o600)

    result = _run_preflight(ctx)
    section = _manifest_section(result.stdout)
    assert result.returncode != 0, "节点路径没有挂载却通过"
    assert any("FAIL" in line and "没有对应的 bind 挂载" in line for line in section), section


def test_two_copies_with_the_same_basename_are_rejected(deployment):
    """同名不同物：两个目录里各放一份 `exec-manifest.json` 不算「同一份」。"""
    ctx = deployment
    _write_env_files(
        ctx["config"],
        gateway_manifest="/etc/m2live/exec-manifest.json",
        node_manifest="/etc/m2live/exec-manifest.json",
    )
    gateway_copy = ctx["config"] / "gateway/exec-manifest.json"
    gateway_copy.write_text('{"schema": "m3-exec-config/1"}\n')
    gateway_copy.chmod(0o600)
    elsewhere = ctx["tmp"] / "elsewhere"
    elsewhere.mkdir()
    node_copy = elsewhere / "exec-manifest.json"
    node_copy.write_text('{"schema": "m3-exec-config/1"}\n')
    node_copy.chmod(0o600)

    result = _run_preflight(ctx, node_source=node_copy)
    section = _manifest_section(result.stdout)
    assert result.returncode != 0, "两份同名副本被当成同一份"
    assert any("FAIL" in line and "不是同一个文件" in line for line in section), section


def test_a_configured_but_absent_manifest_is_rejected(deployment):
    ctx = deployment
    _write_env_files(
        ctx["config"],
        gateway_manifest="/etc/m2live/exec-manifest.json",
        node_manifest="/etc/m2live/exec-manifest.json",
    )

    result = _run_preflight(ctx)
    section = _manifest_section(result.stdout)
    assert result.returncode != 0
    assert any("FAIL" in line and "不存在" in line for line in section), section


def test_a_symlinked_manifest_is_rejected(deployment):
    """符号链接：两侧加载器都拒（读的是链接目标，而声称校验的是这个路径）。"""
    ctx = deployment
    _write_env_files(
        ctx["config"],
        gateway_manifest="/etc/m2live/exec-manifest.json",
        node_manifest="/etc/m2live/exec-manifest.json",
    )
    real = ctx["config"] / "gateway/real-manifest.json"
    real.write_text("{}\n")
    real.chmod(0o600)
    (ctx["config"] / "gateway/exec-manifest.json").symlink_to(real)

    result = _run_preflight(ctx)
    section = _manifest_section(result.stdout)
    assert result.returncode != 0
    assert any("FAIL" in line and "符号链接" in line for line in section), section


@pytest.mark.parametrize("mode", [0o640, 0o644])
def test_a_group_or_world_readable_manifest_is_rejected(deployment, mode):
    ctx = deployment
    _write_env_files(
        ctx["config"],
        gateway_manifest="/etc/m2live/exec-manifest.json",
        node_manifest="/etc/m2live/exec-manifest.json",
    )
    manifest = ctx["config"] / "gateway/exec-manifest.json"
    manifest.write_text("{}\n")
    manifest.chmod(mode)

    result = _run_preflight(ctx)
    section = _manifest_section(result.stdout)
    assert result.returncode != 0
    assert any("FAIL" in line and "过宽" in line for line in section), section


def test_a_directory_is_not_a_manifest(deployment):
    ctx = deployment
    _write_env_files(
        ctx["config"],
        gateway_manifest="/etc/m2live/exec-manifest.json",
        node_manifest="/etc/m2live/exec-manifest.json",
    )
    (ctx["config"] / "gateway/exec-manifest.json").mkdir()

    result = _run_preflight(ctx)
    section = _manifest_section(result.stdout)
    assert result.returncode != 0
    assert any("FAIL" in line and "不是普通文件" in line for line in section), section


def test_up_refuses_to_start_any_container_when_the_manifest_is_broken(deployment):
    """坏态非零，且 `up` 根本没有调到 `docker compose up`。"""
    ctx = deployment
    _write_env_files(
        ctx["config"],
        gateway_manifest="/etc/m2live/exec-manifest.json",
        node_manifest=None,
    )
    (ctx["config"] / "gateway/exec-manifest.json").write_text("{}\n")
    (ctx["config"] / "gateway/exec-manifest.json").chmod(0o600)
    compose_json = ctx["compose_json"]
    compose_json.write_text(
        json.dumps(
            _compose_json(
                ctx["config"],
                ctx["state"],
                node_source=ctx["config"] / "gateway/exec-manifest.json",
                gateway_source=ctx["config"] / "gateway",
            )
        )
    )
    import subprocess

    log = ctx["tmp"] / "docker.log"
    env = {
        "PATH": f"{ctx['bin']}:/usr/bin:/bin",
        "M2LIVE_CONFIG": str(ctx["config"]),
        "M2LIVE_STATE": str(ctx["state"]),
        "M2LIVE_DIST": str(ctx["dist"]),
        "M2LIVE_UID": str(os.getuid()),
        "M2LIVE_GID": str(os.getgid()),
        "M2LIVE_MODEL_UPSTREAM": "https://supplier.invalid",
        "M2LIVE_MODEL_ID": "vendor-model",
        "M2LIVE_SYNTH_MODE": "1",
        "M2_TEST_DOCKER_LOG": str(log),
        "M2_TEST_COMPOSE_JSON": str(compose_json),
        "M2_TEST_IMAGES": "\n".join((GATEWAY_IMAGE, *BASE_REFS)),
        "M2_TEST_IMAGE_ID": IMAGE_ID,
        "M2_TEST_SOURCE": "b" * 64,
    }
    result = subprocess.run(
        ["bash", str(ctx["deploy"] / "checks.sh"), "up"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    calls = log.read_text().splitlines()
    assert not [call for call in calls if call.endswith("up -d") or " up -d" in call], calls
    assert "未启动任何容器" in result.stderr


def test_checks_script_no_longer_compares_basenames():
    """源码级钉子：判据必须经过 compose 解析，不能退回比文件名。

    这条不是风格检查 —— 旧写法正是 `basename(gateway) != basename(node)`，它对
    「开关缺失」和「路径没挂载」都印 PASS。退回它必须让本文件的两条反例转红，
    这里再从源码层面把那条捷径钉住。
    """
    script = (DEPLOY / "checks.sh").read_text(encoding="utf-8")
    assert 'stack_checks.py" manifest' in script, "preflight 没有调用清单判据"
    assert 'basename "$node_manifest"' not in script and 'basename "$manifest"' not in script
