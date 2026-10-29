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


def test_gateway_uses_only_the_generated_business_config(compose):
    volumes = compose["services"]["gateway"].get("volumes", [])
    prod = [str(v) for v in volumes if "/etc/aiops-41" in str(v)]
    assert not prod, "整份生产 keys 可能含供应商凭据，不能进入 Gateway"
    assert any("/gateway/runtime.env:/etc/m2live/runtime.env:ro" in str(v) for v in volumes)


def test_node_reaches_only_internal_networks(compose):
    """节点不得接入可出网网络。模型调用经**固定目标的受控出口**，不是通用 egress。"""
    assert compose["networks"]["trial"]["internal"] is True
    assert compose["networks"]["modellink"]["internal"] is True
    assert compose["services"]["node"]["networks"] == ["trial", "modellink"], "节点只能接内部网络"
    egress_members = [
        name for name, spec in compose["services"].items() if "egress" in (spec.get("networks") or [])
    ]
    assert "node" not in egress_members
    # 出口本身是可出网的，且只转发到一个固定目标。
    proxy = compose["services"]["model-proxy"]
    assert proxy["entrypoint"] == ["python", "/opt/aiops-src/services/model-proxy/model_proxy.py"]
    assert "egress" in proxy["networks"]
    command = " ".join(proxy.get("command", []))
    assert "${M2LIVE_MODEL_UPSTREAM" in command, "出口的上游目标没有在部署件里钉住"


def test_the_model_proxy_never_sees_the_business_credentials(compose):
    proxy = compose["services"]["model-proxy"]
    joined = " ".join(str(v) for v in proxy.get("volumes", []))
    assert "/etc/aiops-41" not in joined, "模型出口挂到了生产配置"
    assert "/tls/proxy" in joined
    assert "tls/public" not in joined.split("/tls/proxy")[0]


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


def test_node_build_is_reproducible():
    """节点 jar 必须是**可复现构建**，否则锁文件里那个摘要只能是「某一次的产物」。

    Spring Boot 的 repackage 会把构建时间写进 MANIFEST：不固定 `outputTimestamp` 时，
    同一份源码两次构建得到不同摘要（实测两个干净树：`22e400f0…` / `08d9c4c7…`）。
    于是 `images.lock.json` 里钉的摘要别人永远复现不出来，preflight 会在别人的构建上
    永远失败。固定之后两次构建逐字节相同（实测 `ba1b0abf…` 两次一致）。

    这条不只查标签在不在：时间戳必须是**可解析的 UTC 时刻**（写成 `now` 或空串就红），
    且锁里的节点摘要必须与变更单里记的那个一致 —— 两份记录各说各话时这里就会红。
    """
    import re
    from datetime import UTC, datetime

    pom = (DEPLOY.parents[1] / "services/execution-node/pom.xml").read_text(encoding="utf-8")
    match = re.search(r"<project.build.outputTimestamp>([^<]+)</project.build.outputTimestamp>", pom)
    assert match is not None, "缺可复现构建时间戳，节点 jar 无法按摘要复现"
    stamp = match.group(1).strip()
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(None), (
        f"构建时间戳必须是带时区的 UTC 时刻，实际 {stamp!r}"
    )

    lock = json.loads((DEPLOY / "images.lock.json").read_text(encoding="utf-8"))
    recorded = re.search(
        r"\| `aiops-execution-node\.jar` \| `([0-9a-f]{64})`",
        (DEPLOY / "HOST-CHANGE.md").read_text(encoding="utf-8"),
    )
    assert recorded is not None, "变更单里没有节点 jar 的摘要行"
    assert lock["node"]["sha256"] == recorded.group(1), (
        "锁文件与变更单对节点制品各说各话："
        f"lock={lock['node']['sha256'][:16]}… host-change={recorded.group(1)[:16]}…"
    )


def test_gateway_context_digest_is_reproducible_and_matches_the_lock():
    """构造上下文摘要必须**与环境无关**，且与锁文件一致。

    这条曾经真的错过：`build-image.sh` 里的 `sort -z` 按本机 locale 排序，
    同一批 141 个文件在 `en_US.UTF-8` 与 `C` 下得到不同摘要（`7e6dc9ca…` / `e7c9af0c…`）。
    锁文件里那个值于是只代表**某台机器上**的构建，reviewer 复核永远对不上 ——
    而且看起来像「制品被换过」。
    """
    script = (DEPLOY / "build-image.sh").read_text(encoding="utf-8")
    assert "LC_ALL=C sort" in script, "构造上下文摘要的排序没有固定 locale，换台机器就变"

    import importlib.util

    spec = importlib.util.spec_from_file_location("context_digest_under_test", DEPLOY / "context_digest.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    value, count = module.digest()
    lock = json.loads((DEPLOY / "images.lock.json").read_text(encoding="utf-8"))
    assert count > 0
    assert value == lock["gateway_image"]["source_sha256"], (
        f"上下文摘要与锁文件不一致：实际 {value[:16]}… 锁 {lock['gateway_image']['source_sha256'][:16]}…"
    )


def test_legacy_node_artifact_does_not_change_gateway_context(tmp_path, monkeypatch):
    import importlib.util
    import shutil
    import subprocess

    spec = importlib.util.spec_from_file_location("context_with_legacy", DEPLOY / "context_digest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected, _ = module.digest()
    for context_name, source in module.files():
        target = tmp_path / context_name.removeprefix("./")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for name in module.ROOT_FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(module.REPO / name, target)
    monkeypatch.setattr(module, "REPO", tmp_path)
    legacy = tmp_path / "tools/m2_close/node/target/aiops-execution-node.jar"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"old ignored build output")
    assert module.digest()[0] == expected
    # 执行真实打包命令，确保shell侧也不收旧产物。
    script = (DEPLOY / "build-image.sh").read_text()
    command = next(line for line in script.splitlines() if "tar -cf -" in line and "tools/m2_live" in line)
    command = command.split("&& ", 1)[1].split(")", 1)[0]
    packed = subprocess.run(["bash", "-c", command], cwd=tmp_path, capture_output=True, check=True)
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(packed.stdout)) as archive:
        assert not any("node/target" in item.name for item in archive)
