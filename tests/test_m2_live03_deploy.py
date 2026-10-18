"""M2-LIVE-03 部署件的形状约束。

这些不是「跑起来了吗」的测试（那要宿主），而是**部署件本身**的约束：
资源上限之和、端口只绑回环、凭据边界、镜像/制品固定、运行身份、正式启动门禁。
任何一条被改坏，都会在起容器之前或启动那一刻暴露。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/m2-live-03"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))


def _memory_bytes(limit: str) -> int:
    text = str(limit).lower()
    if text[-1] in "kmg":
        return int(float(text[:-1]) * {"k": 1024, "m": 1024**2, "g": 1024**3}[text[-1]])
    return int(text)


def test_total_budget_is_within_the_agreed_ceiling(compose):
    """整栈上限必须在 3 GiB / 1.5 CPU 之内（按指令，这是总额不是每进程）。

    辅助中继（宿主回环依赖）也算在里面 —— 「新增进程不计预算」是漏项的常见形式。
    """
    memory = sum(_memory_bytes(spec["mem_limit"]) for spec in compose["services"].values())
    cpu = sum(float(spec["cpus"]) for spec in compose["services"].values())
    assert memory <= 3 * 1024**3, f"内存上限合计 {memory / 1024**3:.2f} GiB 超过 3 GiB"
    assert cpu <= 1.5, f"CPU 上限合计 {cpu} 超过 1.5"


def test_only_the_gateway_publishes_a_port_and_only_on_loopback(compose):
    published = []
    for name, spec in compose["services"].items():
        for port in spec.get("ports", []) or []:
            text = str(port)
            assert text.startswith("127.0.0.1:"), f"{name} 把端口暴露到非回环: {text}"
            published.append((name, text))
    assert [name for name, _ in published] == ["gateway"]


def test_platform_and_node_never_mount_the_production_config(compose):
    """平台与节点不得看到生产配置、生产 key 或生产状态库。"""
    for name in ("platform", "node"):
        joined = " ".join(str(v) for v in compose["services"][name].get("volumes", []))
        assert "/etc/aiops-41" not in joined, f"{name} 挂载了生产配置目录"
        assert "/var/lib/aiops-41" not in joined, f"{name} 挂载了生产状态库"


def test_only_the_gateway_mounts_production_read_only(compose):
    volumes = compose["services"]["gateway"].get("volumes", [])
    prod = [str(v) for v in volumes if "/etc/aiops-41" in str(v)]
    assert prod, "隔离 Gateway 应只读挂载既有业务依赖"
    for entry in prod:
        assert entry.endswith(":ro"), f"生产挂载必须是只读: {entry}"


def test_the_internal_network_has_no_egress_and_node_uses_only_it(compose):
    assert compose["networks"]["trial"]["internal"] is True
    assert compose["services"]["node"]["networks"] == ["trial"], "节点不得接入可出网网络"


def test_gateway_state_lives_under_the_stack_root(compose):
    volumes = compose["services"]["gateway"]["volumes"]
    state = [str(v) for v in volumes if str(v).startswith("${M2LIVE_STATE") and v.endswith(":/data")]
    assert state, "隔离 Gateway 的数据目录必须来自 M2LIVE_STATE"
    assert "/var/lib/aiops-41" not in state[0]


def test_every_service_declares_a_numeric_runtime_user(compose):
    """基础 JVM/Python 镜像没有设 User；不显式指定就会以 root 跑。

    允许 `${M2LIVE_UID:-999}` 这种带数字默认值的占位：渲染后必须是数字（preflight
    在 `compose config` 的输出上再查一遍），解引用失败时不会回退到 root。
    """
    for name, spec in compose["services"].items():
        if name == "store":
            # MySQL 官方入口点要短暂以 root 初始化数据目录，之后以 mysql 用户运行；
            # 这是数据目录所有权的既定模型，不在本测试里强行改身份。
            continue
        user = str(spec.get("user") or "")
        assert user, f"{name} 没有指定运行用户"
        # 占位符里自身带冒号（`${M2LIVE_UID:-999}`），所以整串匹配而不是切分。
        numeric = re.fullmatch(r"\d+:\d+", user)
        placeholder = re.fullmatch(r"\$\{\w+:-\d+\}:\$\{\w+:-\d+\}", user)
        assert numeric or placeholder, f"{name} 的 UID/GID 没有数字默认值: {user}"


def test_gateway_starts_from_a_built_image_and_never_installs_at_startup(compose):
    """启动不下载依赖：镜像里有锁定的依赖，启动命令里没有 pip。"""
    gateway = compose["services"]["gateway"]
    assert "${M2LIVE_GATEWAY_IMAGE" in str(gateway["image"])
    command = " ".join(gateway.get("command", []))
    assert "pip install" not in command, "启动时仍在安装依赖"
    assert "gateway_server" in command, "正式启动路径必须是 gateway_server serve"


def test_gateway_real_startup_uses_the_isolation_guard(compose):
    """真实启动路径必须声明隔离根；没有它，门禁在启动时是空操作。"""
    environment = compose["services"]["gateway"].get("environment") or {}
    assert "M2LIVE_ISOLATION_ROOT" in environment
    assert "M2LIVE_READONLY_PATHS" in environment
    source = (Path(__file__).resolve().parents[1] / "src/aiops_diagnostics/gateway_server.py").read_text()
    assert "enforce_from_env" in source, "gateway_server 没有调用隔离门禁"


def test_artifacts_are_pinned_by_digest():
    lock = json.loads((DEPLOY / "images.lock.json").read_text(encoding="utf-8"))
    for name in ("platform", "node"):
        digest = lock[name]["sha256"]
        assert isinstance(digest, str) and len(digest) == 64, f"{name} 缺 64 位摘要"
    assert lock["platform"]["commit"], "平台必须锁 commit"
    gateway = lock["gateway_image"]
    # image_id 每次构建都不同（时间戳），所以仓库里只锁可复现的内容摘要；
    # 实际加载的 ID 由宿主上的 identity.json 核对。
    for key in ("source_sha256", "uv_lock_sha256", "base_digest"):
        assert gateway.get(key), f"gateway_image 缺 {key}"
    requirements = (DEPLOY / "gateway-requirements.txt").read_text(encoding="utf-8")
    assert "--hash=sha256:" in requirements, "依赖清单没有哈希：启动时仍可漂移"


def test_base_images_are_parameterized_and_locked_by_ref():
    """基础镜像名可被宿主覆盖，但覆盖值必须在锁里且有 image ID。

    41 上 docker.io 直连不可达；同时「tag 名相同」不能当作身份 —— 锁里存的是 image ID。
    """
    text = (DEPLOY / "compose.yaml").read_text(encoding="utf-8")
    for variable in ("M2LIVE_MYSQL_IMAGE", "M2LIVE_JAVA_IMAGE", "M2LIVE_GATEWAY_IMAGE"):
        assert variable in text, f"compose.yaml 没有引用 {variable}"
    lock = json.loads((DEPLOY / "images.lock.json").read_text(encoding="utf-8"))
    refs = lock["base_images"]["refs"]
    assert refs, "锁文件没有 base_images.refs"
    for ref, entry in refs.items():
        assert entry.get("image_id"), f"{ref} 缺 image_id"
        assert entry["image_id"].startswith("sha256:"), f"{ref} 的 image_id 不是摘要"
    development = {ref for ref, entry in refs.items() if entry.get("host") == "development"}
    assert development, "锁里没有开发机实跑的镜像身份（合成报告不能借用 41 的）"


def test_preflight_compares_image_identity_not_existence():
    script = (DEPLOY / "checks.sh").read_text(encoding="utf-8")
    assert "docker image inspect -f '{{.Id}}'" in script, "preflight 没有读取 image ID"
    assert "不在锁文件里" in script, "preflight 没有拒绝未登记的镜像名"
    assert "gateway_image" in script, "preflight 没有核对 Gateway 构建身份"
    assert "m2live.source" in script, "preflight 没有比对内容摘要标签"


def test_checks_script_is_syntactically_valid():
    import subprocess

    subprocess.run(["bash", "-n", str(DEPLOY / "checks.sh")], check=True)
    subprocess.run(["bash", "-n", str(DEPLOY / "bootstrap.sh")], check=True)


def test_readme_and_host_change_do_not_claim_authorization():
    host_change = (DEPLOY / "HOST-CHANGE.md").read_text(encoding="utf-8")
    assert "不是授权" in host_change
    readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
    assert "HOST-CHANGE.md" in readme
