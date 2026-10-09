import json
import os
from pathlib import Path

from rich.text import Text
from typer.testing import CliRunner

from aiops_diagnostics.cli import app

FIXTURE = Path(__file__).parents[1] / "examples" / "fixtures" / "ocpp_consistent.json"


def test_cli_json_fixture() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "diagnose",
            "订单 TEST-OCPP-0003 金额是否正常",
            "--mode",
            "deterministic",
            "--fixture",
            str(FIXTURE),
            "--json",
        ],
    )
    assert result.exit_code == 0
    assert "TEST-OCPP-0003" in result.stdout
    assert '"confidence": "high"' in result.stdout


def test_cli_malformed_fixture_fails_without_traceback(tmp_path: Path) -> None:
    fixture = tmp_path / "broken.json"
    fixture.write_text("{not-json", encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "diagnose",
            "订单 TEST-OCPP-0003 金额是否正常",
            "--mode",
            "deterministic",
            "--fixture",
            str(fixture),
        ],
    )

    assert result.exit_code == 2
    assert "初始化失败" in result.stdout
    assert "Traceback" not in result.stdout


def test_cli_initializes_portable_home_and_installs_key(tmp_path: Path) -> None:
    runner = CliRunner()
    portable_home = tmp_path / "portable-home"
    config = portable_home / "custom.env"
    environment = {"AIOPS_HOME": str(portable_home)}

    initialized = runner.invoke(
        app,
        ["--config", str(config), "init"],
        env=environment,
    )

    assert initialized.exit_code == 0
    assert config.is_file()
    source = tmp_path / "provider.key"
    source.write_text("provider-secret", encoding="utf-8")
    installed = runner.invoke(
        app,
        ["--config", str(config), "key-install", "primary", "--from-file", str(source)],
        env=environment,
    )

    assert installed.exit_code == 0
    payload = json.loads(installed.stdout)
    assert payload["key_slot"] == "primary"
    assert "provider-secret" not in installed.stdout
    key_file = portable_home / "keys" / "primary.key"
    assert key_file.read_text(encoding="utf-8").strip() == "provider-secret"
    if os.name != "nt":
        assert key_file.stat().st_mode & 0o077 == 0


def test_diagnose_unified_exposes_mode_and_progress() -> None:
    result = CliRunner().invoke(app, ["diagnose", "--help"], color=True)
    output = Text.from_ansi(result.stdout).plain

    assert result.exit_code == 0
    assert "--mode" in output
    assert "deterministic" in output
    assert "--progress" in output
    assert "--no-progress" in output
    assert "--provider" in output


def test_agent_diagnose_command_removed() -> None:
    result = CliRunner().invoke(app, ["agent-diagnose", "--help"])

    # --help exits 0 for any registered command; non-zero proves agent-diagnose
    # was removed (the unknown-command usage error is printed to stderr).
    assert result.exit_code != 0


def test_diagnose_agent_mode_without_key_suggests_deterministic(tmp_path: Path) -> None:
    runner = CliRunner()
    portable_home = tmp_path / "nokey-home"
    config = portable_home / "custom.env"
    environment = {"AIOPS_HOME": str(portable_home), "AIOPS_CODEX_API_KEY": ""}

    initialized = runner.invoke(app, ["--config", str(config), "init"], env=environment)
    assert initialized.exit_code == 0

    result = runner.invoke(
        app,
        ["--config", str(config), "diagnose", "订单 TEST-YKC-0001 金额异常"],
        env=environment,
    )

    assert result.exit_code == 2
    assert "--mode deterministic" in result.stdout


def test_cli_exposes_remote_gateway_commands() -> None:
    result = CliRunner().invoke(app, ["remote", "--help"], terminal_width=120)

    assert result.exit_code == 0
    assert "enroll" in result.stdout
    assert "diagnose" in result.stdout
    assert "events" in result.stdout


def test_admin_reconcile_end_to_end(tmp_path: Path) -> None:
    runner = CliRunner()
    portable_home = tmp_path / "portable-home"
    config = portable_home / "production.env"
    environment = {"AIOPS_HOME": str(portable_home)}
    config.parent.mkdir(parents=True)
    config.write_text("AIOPS_AGENT_MODEL=aiops-api\n", encoding="utf-8")
    if os.name != "nt":
        import stat as _stat

        config.chmod(_stat.S_IRUSR | _stat.S_IWUSR)
    manifest = tmp_path / "env.toml"
    manifest.write_text(
        """
[[agents]]
tenant_id = "tenant-a"
name = "客服助手"
description = "d"
agent_type = "customer"
prompt = "回答必须引用已授权的业务资料。"
knowledge_base_ids = []
model = "aiops-api"
""",
        encoding="utf-8",
    )
    db = tmp_path / "gateway.db"

    first = runner.invoke(
        app,
        ["--config", str(config), "admin", "reconcile", str(manifest), "--db", str(db)],
        env=environment,
    )
    assert first.exit_code == 0, first.output
    payload = json.loads(first.stdout)
    assert [item["action"] for item in payload["reports"]] == ["created"]

    second = runner.invoke(
        app,
        ["--config", str(config), "admin", "reconcile", str(manifest), "--db", str(db)],
        env=environment,
    )
    assert second.exit_code == 0
    assert [item["action"] for item in json.loads(second.stdout)["reports"]] == ["unchanged"]


def test_admin_reconcile_unknown_model_exits_two(tmp_path: Path) -> None:
    runner = CliRunner()
    portable_home = tmp_path / "portable-home"
    config = portable_home / "production.env"
    config.parent.mkdir(parents=True)
    config.write_text("AIOPS_AGENT_MODEL=aiops-api\n", encoding="utf-8")
    if os.name != "nt":
        import stat as _stat

        config.chmod(_stat.S_IRUSR | _stat.S_IWUSR)
    manifest = tmp_path / "bad.toml"
    manifest.write_text(
        """
[[agents]]
tenant_id = "tenant-a"
name = "客服助手"
agent_type = "customer"
prompt = "p"
knowledge_base_ids = []
model = "gpt-4o"
""",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["--config", str(config), "admin", "reconcile", str(manifest), "--db", str(tmp_path / "g.db")],
        env={"AIOPS_HOME": str(portable_home)},
    )
    assert result.exit_code == 2, result.output
    assert "不在白名单" in result.output


def test_admin_group_exposed() -> None:
    result = CliRunner().invoke(app, ["admin", "--help"])
    assert result.exit_code == 0
    assert "reconcile" in result.output


def test_admin_reconcile_requires_explicit_db(tmp_path: Path) -> None:
    """XDG 陷阱守卫：不显式给 --db 时拒绝执行，绝不静默新建空库（M55 事故回归）。"""
    runner = CliRunner()
    portable_home = tmp_path / "portable-home"
    config = portable_home / "production.env"
    config.parent.mkdir(parents=True)
    config.write_text("AIOPS_AGENT_MODEL=aiops-api\n", encoding="utf-8")
    if os.name != "nt":
        import stat as _stat

        config.chmod(_stat.S_IRUSR | _stat.S_IWUSR)
    manifest = tmp_path / "env.toml"
    manifest.write_text(
        """
[[agents]]
tenant_id = "tenant-a"
name = "客服助手"
agent_type = "customer"
prompt = "p"
knowledge_base_ids = []
model = "aiops-api"
""",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["--config", str(config), "admin", "reconcile", str(manifest)],
        env={"AIOPS_HOME": str(portable_home)},
    )
    assert result.exit_code == 2, result.output
    assert "缺少 --db" in result.output
    # 显式环境变量是合法的显式配置：不再拒绝
    result_env = runner.invoke(
        app,
        ["--config", str(config), "admin", "reconcile", str(manifest)],
        env={
            "AIOPS_HOME": str(portable_home),
            "AIOPS_GATEWAY_DATABASE_FILE": str(tmp_path / "explicit.db"),
        },
    )
    assert result_env.exit_code == 0, result_env.output
    # 守卫先于任何 store 写入：XDG 默认路径下没有新建任何 gateway.db
    assert not (portable_home / "gateway").exists() or not list(
        (portable_home / "gateway").glob("gateway.db")
    )


# ── Dify 拉取并发布（#625）──────────────────────────────────────────────────


DSL_FIXTURE = Path(__file__).parent / "fixtures" / "dify-app-chat.dsl.yml"


def _serve_dify_export(monkeypatch, *, dsl_text: str | None = None, error: Exception | None = None):
    """桩住 Dify 的导出端点，并记录我们发出去的请求。

    同 `tests/test_dify_dsl_pull.py` 的 `_serve`：**真 DSL**（从 36 上真实实例拉的
    固件），所以映射那一段跑的是生产代码路径而不是样例数据。
    """
    import json as _json
    from unittest.mock import MagicMock

    text = dsl_text if dsl_text is not None else DSL_FIXTURE.read_text(encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = {key.lower(): value for key, value in request.header_items()}
        if error is not None:
            raise error
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        resp.read.return_value = _json.dumps({"data": text}).encode("utf-8")
        return resp

    monkeypatch.setattr("aiops_diagnostics.bounded_http.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("aiops_diagnostics.bounded_http.time.sleep", lambda seconds: None)
    monkeypatch.setattr("aiops_diagnostics.bounded_http.random.uniform", lambda a, b: 0.0)
    return seen


def _pull_env(tmp_path: Path, *, body: str | None = None) -> dict:
    """控制台凭据走**服务端配置**（不是命令行参数：那会进 shell 历史与进程表）。

    写的是 `AIOPS_HOME/production.env` —— `selected_config_file()` 与
    `GatewayServerSettings.from_env()` 在没显式覆盖时都解析到它，所以这一个文件同时
    喂给 `Settings.from_config` 与网关设置，与 41 上的形状一致（那里是
    `/etc/aiops-41/production.env`，两个读取方指向同一个文件）。
    """
    config = tmp_path / "production.env"
    config.write_text(
        body
        if body is not None
        else (
            "AIOPS_AGENT_MODEL=deepseek-v4-flash\n"
            "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL=http://dify.invalid:10008\n"
            "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY=console-key-not-a-real-value\n"
            "AIOPS_GATEWAY_DIFY_CONSOLE_WORKSPACE_ID=ws-1\n"
        ),
        encoding="utf-8",
    )
    if os.name != "nt":
        import stat as _stat

        config.chmod(_stat.S_IRUSR | _stat.S_IWUSR)
    return {
        "AIOPS_HOME": str(tmp_path),
        "AIOPS_GATEWAY_DATABASE_FILE": str(tmp_path / "gateway.db"),
    }


def _pull_args(tmp_path: Path) -> list[str]:
    return [
        "admin",
        "pull-dify",
        "--app-id",
        "a975c8e5-ad9e-425c-84c9-77581a0a2bed",
        "--tenant",
        "T-PULL",
        "--name",
        "小趋-客服",
        "--db",
        str(tmp_path / "gateway.db"),
    ]


def test_pull_dify_fetches_and_publishes_through_the_real_path(tmp_path: Path, monkeypatch) -> None:
    """一次动作走完「拉取 → 映射 → 落草稿 → 发布」，且**发布的是真版本**。

    关键的断言是最后那条：用 `AgentManager`（也就是 `/v1/agents` 与 `admin reconcile`
    走过的那条路）读回来，确认版本号存在、快照与拉到的提示词一致 —— 而不是只看命令
    退出码为 0。退出码为 0 也可以是一次什么都没发布的空跑。
    """
    seen = _serve_dify_export(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(app, _pull_args(tmp_path), env=_pull_env(tmp_path))

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["tenant_id"] == "T-PULL" and body["version_no"] == 1
    # 请求真的打到了控制台导出端点，且带的是配置里那把凭据。
    assert "/console/api/apps/a975c8e5-ad9e-425c-84c9-77581a0a2bed/export" in str(seen["url"])
    assert str(seen["headers"]).find("console-key-not-a-real-value") >= 0

    # 经生产代码路径读回：版本已冻结，快照里的提示词来自那份真 DSL。
    from aiops_diagnostics.agent_lifecycle import AgentManager, AgentStore
    from aiops_diagnostics.agent_manifest import admin_context

    manager = AgentManager(
        AgentStore(tmp_path / "gateway.db"),
        knowledge_resolver=_AllowAll(),
        allowed_models=("deepseek-v4-flash",),
    )
    context = admin_context("T-PULL")
    agent = manager.get(context, body["agent_id"])
    assert agent.status == "published" and agent.published_version == 1
    snapshot = manager.version(context, agent.agent_id, 1).snapshot
    assert "小趋" in snapshot["prompt"]


class _AllowAll:
    def validate(self, tenant_id: str, knowledge_base_ids: tuple[str, ...]) -> None:
        del tenant_id, knowledge_base_ids


def test_pull_dify_dry_run_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    """`--dry-run` 零写入：库文件里**一行 agent 都不该有**。

    这条判据只能这么测：看库，不看输出。一份"打印了计划但顺手落了草稿"的实现
    在输出上与正确实现完全一样。
    """
    _serve_dify_export(monkeypatch)
    result = CliRunner().invoke(app, [*_pull_args(tmp_path), "--dry-run"], env=_pull_env(tmp_path))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["dry_run"] is True

    from aiops_diagnostics.agent_lifecycle import AgentStore

    store = AgentStore(tmp_path / "gateway.db")
    assert store.list("T-PULL") == []


def test_pull_dify_reports_the_pull_failure_and_leaves_the_store_alone(tmp_path: Path, monkeypatch) -> None:
    """Dify 不可达 ⇒ 如实转述 + 非零退出 + 库里没有任何痕迹。

    四类错误码（不可达 / 凭据被拒 / 版本不符 / 字段不可映射）是 #580 建的；
    入口不得把它们糊成一句"失败" —— 后三类在输出里必须能看出来。
    """
    import urllib.error

    _serve_dify_export(monkeypatch, error=urllib.error.URLError("connection refused"))
    result = CliRunner().invoke(app, _pull_args(tmp_path), env=_pull_env(tmp_path))

    assert result.exit_code == 1, result.output
    assert "DIFY_DSL_UNREACHABLE" in result.output

    from aiops_diagnostics.agent_lifecycle import AgentStore

    assert AgentStore(tmp_path / "gateway.db").list("T-PULL") == []


def test_pull_dify_needs_the_console_credential(tmp_path: Path) -> None:
    """没配控制台凭据 ⇒ 明确拒绝并说清缺哪一个，不是静默地拉了一个空 DSL。"""
    env = _pull_env(tmp_path, body="AIOPS_AGENT_MODEL=deepseek-v4-flash\n")
    result = CliRunner().invoke(app, _pull_args(tmp_path), env=env)
    assert result.exit_code == 2, result.output
    assert "DIFY_CONSOLE_API_KEY" in result.output
