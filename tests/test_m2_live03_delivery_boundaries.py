"""从部署命令和挂载表验证凭据边界，不使用公司配置或真实 Docker。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy/m2-live-03"


@pytest.fixture
def prepared(tmp_path):
    config = tmp_path / "config"
    for name in ("gateway", "node", "secrets"):
        (config / name).mkdir(parents=True)
    (config / "gateway/runtime.env").write_text("AIOPS_REDIS_PASSWORD=business-fixture\n")
    (config / "gateway/agent-runs.json").write_text("{}")
    (config / "node/node.env").write_text("AIOPS_NODE_TOKEN=node-fixture\n")
    vendor = tmp_path / "vendor-key"
    vendor.write_text("vendor-fixture\n")
    vendor.chmod(0o600)
    env = {"PATH": os.environ["PATH"], "M2LIVE_CONFIG": str(config), "M2LIVE_SYNTH_MODE": "0"}
    env.update(M2LIVE_MODEL_API_KEY_FILE=str(vendor), M2LIVE_MODEL_ID="vendor-model")
    env["M2LIVE_MODEL_ENDPOINT"] = "https://model-proxy:9101/v1"
    subprocess.run(["bash", str(DEPLOY / "prepare-secrets.sh")], env=env, check=True, capture_output=True)
    return config, env


def _mounted_sources(service, config):
    services = yaml.safe_load((DEPLOY / "compose.yaml").read_text())["services"]
    return [
        Path(re.sub(r"\$\{M2LIVE_CONFIG:-[^}]+\}", str(config), mount.rsplit(":", 2)[0]))
        for mount in services[service].get("volumes", [])
    ]


def test_model_proxy_cannot_read_business_credentials(prepared):
    config, _ = prepared
    secret = config / "gateway/runtime.env"
    assert not any(p == secret or p in secret.parents for p in _mounted_sources("model-proxy", config))


def test_only_model_proxy_mounts_the_supplier_key(prepared):
    config, _ = prepared
    secret = next(config.rglob("upstream_api_key"))
    for service in ("gateway", "node", "platform", "model-proxy"):
        visible = any(p == secret or p in secret.parents for p in _mounted_sources(service, config))
        assert visible == (service == "model-proxy"), service


def test_gateway_cannot_see_a_supplier_key_in_production_keys(prepared, tmp_path):
    config, _ = prepared
    production = tmp_path / "production"
    secret = production / "keys/provider.key"
    secret.parent.mkdir(parents=True)
    secret.write_text("synthetic-supplier-key")
    for service in ("gateway", "node", "platform", "model-proxy"):
        for path in _mounted_sources(service, config):
            source = Path(re.sub(r"\$\{AIOPS41_CONFIG_DIR:-[^}]+\}", str(production), str(path)))
            assert source != secret and source not in secret.parents, service


def test_preflight_rejects_reintroduced_production_mounts(tmp_path):
    production, config, state = (tmp_path / name for name in ("production", "config", "state"))
    for source in (production, production / "keys", production / "production.env"):
        rendered = {
            "services": {"gateway": {"volumes": [{"type": "bind", "source": str(source), "read_only": True}]}}
        }
        result = subprocess.run(
            [sys.executable, str(DEPLOY / "stack_checks.py"), "mounts", str(state), str(config)],
            input=json.dumps(rendered),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, str(source)


def test_bootstrap_never_passes_the_supplier_key_to_the_node(prepared, tmp_path):
    config, env = prepared
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    captured = tmp_path / "captured-model-key"
    docker = bin_dir / "docker"
    docker.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, shutil, sys\n"
        "a = sys.argv[1:]\n"
        "if a[0] == 'cp' and a[2].endswith(':/tmp/model_api_key'):\n"
        "    shutil.copyfile(a[1], os.environ['M2_TEST_CAPTURE'])\n"
        "if a[0] == 'exec' and '-c' in a and a[-1] == '/tmp/model_api_key':\n"
        "    pathlib.Path(os.environ['M2_TEST_CAPTURE']).write_bytes(sys.stdin.buffer.read())\n"
        "if a[-2:] == ['python', '/tmp/bootstrap_platform.py']:\n"
        "    sys.exit(19)\n"
    )
    docker.chmod(0o700)
    env.update(
        PATH=f"{bin_dir}:{env['PATH']}",
        M2_TEST_CAPTURE=str(captured),
        M2LIVE_UID=str(os.getuid()),
        M2LIVE_GID=str(os.getgid()),
    )
    result = subprocess.run(
        ["bash", str(DEPLOY / "bootstrap.sh")], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 19, result.stderr
    proxy_token = next(config.rglob("proxy_token")).read_text().strip()
    assert captured.read_text().strip() == proxy_token
    assert captured.read_text().strip() != "vendor-fixture"
    assert "vendor-fixture" not in result.stdout + result.stderr


def test_live_proxy_rejects_http_before_listening(monkeypatch):
    sys.path.insert(0, str(DEPLOY.parents[1] / "tools"))
    from m2_live import model_proxy

    def unexpected_listener(*args, **kwargs):
        pytest.fail("正式模式接受 HTTP 后开始监听")

    monkeypatch.setattr(model_proxy, "ThreadingHTTPServer", unexpected_listener)
    with pytest.raises(SystemExit, match="HTTPS"):
        model_proxy.serve("127.0.0.1", 0, "http://provider.test", "vendor", "proxy", "c", "k")


def test_ready_requires_all_services_in_the_same_round():
    source = (DEPLOY / "checks.sh").read_text()
    ready = source[source.index("\nready() {") : source.index("\nverify() {")]
    fixture = r"""
round=0
say() { :; }; grn() { :; }; red() { :; }; sleep() { :; }; containers() { :; }
curl() { round=$((round+1)); [ "$round" -ne 2 ]; }
docker() {
  if [ "$1" = inspect ]; then echo healthy; return 0; fi
  if [ "$1" = stats ]; then return 0; fi
  case "$*" in
    *m2live03-gateway*) [ "$round" -ge 2 ]; return $? ;;
    *m2live03-store*) if [ "$round" -eq 2 ]; then echo 0; else echo 1; fi; return 0 ;;
  esac
  return 1
}
ready || exit 1
printf '%s' "$round"
"""
    result = subprocess.run(["bash", "-c", ready + fixture], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "3", "第二轮 Gateway 和节点失败，不能复用第一轮的成功"


# ---------------------------------------------------------------- 复跑覆盖层
def test_identity_overlay_only_replaces_the_gateway_entrypoint():
    """复跑用的覆盖层**只**换入口进程，不动授权器、不动挂载、不带凭据。

    判据不是「看起来像模板」，而是：(1) 只声明 `gateway` 一个服务；(2) 只改
    `env_file` 与 `command`；(3) 入口是 `identity_launcher.py`（它不传
    order_authorizer，ADR-0058）；(4) 文件里没有任何身份值。
    """
    overlay = yaml.safe_load((DEPLOY / "compose.identity.yaml").read_text(encoding="utf-8"))
    assert list(overlay["services"]) == ["gateway"], "覆盖层不得改其它服务"
    gateway = overlay["services"]["gateway"]
    assert set(gateway) <= {"env_file", "command"}, f"覆盖层改了不该改的键: {sorted(gateway)}"
    entrypoint = "\n".join(gateway["command"])
    assert "tools/m2_close/m3live/identity_launcher.py" in entrypoint
    # 反面对照：合成验收的启动器会替换授权器，复跑用的这个不行。
    assert "gateway_launcher.py" not in entrypoint
    text = (DEPLOY / "compose.identity.yaml").read_text(encoding="utf-8")
    # 注释里可以**提到**键名（说明这份文件怎么用），但任何一行真的赋值都不行 ——
    # 判据是「有值」，不是「出现过这个词」。
    assignments = [line for line in text.splitlines() if re.match(r"^\s*AIOPS_M2_[A-Z_]+=\S+", line)]
    assert not assignments, f"覆盖层里写了身份值：{assignments}"
    assert (DEPLOY / "identity.env.template").is_file(), "身份三件套缺模板"


def test_checks_accepts_the_identity_overlay_without_touching_down():
    """`M2LIVE_COMPOSE_EXTRA` 已接到 `COMPOSE` 数组上，`down` 也走同一个数组。

    覆盖层因此既能参与起栈，也会在停栈时被记得 —— 「down 需要手动再加一次参数」
    会让清理漏掉覆盖层定义的东西。
    """
    script = (DEPLOY / "checks.sh").read_text(encoding="utf-8")
    assert 'COMPOSE+=(-f "$M2LIVE_COMPOSE_EXTRA")' in script
    down = script[script.index("\ndown() {") : script.index("\nclaim_state_root() {")]
    assert '"${COMPOSE[@]}" down' in down, "down 没有走同一份 COMPOSE 数组"
