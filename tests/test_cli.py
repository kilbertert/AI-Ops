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


def test_pull_dify_reads_the_console_keys_from_the_config_flag(tmp_path: Path, monkeypatch) -> None:
    """`--config <file>` 指的那个文件里的控制台三键必须被读到（#625 实测踩到）。

    踩到的形状：41 上 `--config /etc/aiops-41/production.env` 是既有调用形状
    （runbook §2/§3 全这么写），而 systemd 里 `AIOPS_GATEWAY_SERVER_CONFIG_FILE` 指向
    **另一个**文件（`gateway.env`）。`GatewayServerSettings.from_env()` 只认后者，
    于是配置明明在 `--config` 指定的文件里，命令却报"缺少 Dify 控制台配置"。

    测试里把两个来源**分开**摆好：控制台键只放进 `--config` 的文件，进程环境里没有 ——
    这正是生产上的那种不一致。既有三个命令没暴露它，是因为它们的控制台键只在网关进程里用。
    """
    _serve_dify_export(monkeypatch)
    config = tmp_path / "server.env"
    config.write_text(
        "AIOPS_AGENT_MODEL=deepseek-v4-flash\n"
        "AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL=http://dify.invalid:10008\n"
        "AIOPS_GATEWAY_DIFY_CONSOLE_API_KEY=console-key-not-a-real-value\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        import stat as _stat

        config.chmod(_stat.S_IRUSR | _stat.S_IWUSR)

    # `AIOPS_HOME` 指向一个**没有**控制台键的目录：若实现只读 from_env，它会报缺少配置。
    env = {
        "AIOPS_HOME": str(tmp_path / "empty-home"),
        "AIOPS_GATEWAY_DATABASE_FILE": str(tmp_path / "gateway.db"),
    }
    (tmp_path / "empty-home").mkdir()

    result = CliRunner().invoke(app, ["--config", str(config), *_pull_args(tmp_path)], env=env)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["version_no"] == 1


def test_pull_dify_either_path_binds_the_knowledge_base_the_caller_gave(tmp_path: Path, monkeypatch) -> None:
    """`--knowledge-base-id` 在**两条路径**上都必须生效（干跑看输出，真跑看库）。

    Dify 的 dataset id 不是我们的知识库 id（两个 id 空间），所以绑定只能由调用者给。
    两条路径各写一次同一个参数是很容易漏的：干跑那条改对了、真跑那条没改，命令照样
    退 0，而线上那一版**没有知识库** —— 客服因此到不了 RAG 路径，且从输出上看不出来。
    这条把两条路径钉成同一个答案：同一份输入，干跑报告的 config 与真跑落库的快照
    逐字相同。
    """
    kb = "8701c742b28111f18637d95f7710e3a3"

    # 发布门的知识库活性校验本身已有独立用例（`test_agent_debug`）。这里要钉的是
    # **绑定有没有被传到那一步**，所以把校验这一层换成空实现，不重复测 HTTP 客户端。
    monkeypatch.setattr(
        "aiops_diagnostics.agent_debug.KbBindingResolver.validate",
        lambda self, tenant_id, knowledge_base_ids: None,
    )
    _serve_dify_export(monkeypatch)
    dry = CliRunner().invoke(
        app,
        [*_pull_args(tmp_path), "--knowledge-base-id", kb, "--kb-url", "http://kb.invalid", "--dry-run"],
        env=_pull_env(tmp_path),
    )
    assert dry.exit_code == 0, dry.output
    dry_config = json.loads(dry.stdout)["config"]

    _serve_dify_export(monkeypatch)
    real = CliRunner().invoke(
        app,
        [*_pull_args(tmp_path), "--knowledge-base-id", kb, "--kb-url", "http://kb.invalid"],
        env=_pull_env(tmp_path),
    )
    assert real.exit_code == 0, real.output

    from aiops_diagnostics.agent_lifecycle import AgentManager, AgentStore
    from aiops_diagnostics.agent_manifest import admin_context

    manager = AgentManager(
        AgentStore(tmp_path / "gateway.db"),
        knowledge_resolver=_AllowAll(),
        allowed_models=("deepseek-v4-flash",),
    )
    context = admin_context("T-PULL")
    agent = manager.get(context, json.loads(real.stdout)["agent_id"])
    snapshot = manager.version(context, agent.agent_id, agent.published_version).snapshot

    assert snapshot["knowledge_base_ids"] == [kb]
    assert dry_config["knowledge_base_ids"] == [kb]


def test_pull_dify_refuses_a_dsl_that_declares_a_dataset_when_none_was_given(
    tmp_path: Path, monkeypatch
) -> None:
    """DSL 声明了数据集、调用者没给 ⇒ 拒，且**库里不留痕**（不是静默按字面搬运）。"""
    dsl = DSL_FIXTURE.read_text(encoding="utf-8").replace(
        "  dataset_configs:\n    retrieval_model: multiple\n",
        "  dataset_configs:\n    retrieval_model: multiple\n"
        "    datasets:\n      datasets:\n"
        "      - dataset:\n          enabled: true\n"
        "          id: df0f1d4a-3d6e-404e-9656-c88da6df3d1e\n",
    )
    _serve_dify_export(monkeypatch, dsl_text=dsl)
    result = CliRunner().invoke(app, _pull_args(tmp_path), env=_pull_env(tmp_path))

    assert result.exit_code == 1, result.output
    assert "id 空间" in result.output

    from aiops_diagnostics.agent_lifecycle import AgentStore

    assert AgentStore(tmp_path / "gateway.db").list("T-PULL") == []


def test_pull_dify_dry_run_reports_the_retained_binding_the_real_pull_keeps(
    tmp_path: Path, monkeypatch
) -> None:
    """**保留**的情形也要两条路径一致（评审在 PR #741 上逮到）。

    上一条只覆盖了「显式给了 `--knowledge-base-id`」。而**没给**时真跑会保留库中现值、
    干跑却报空 —— 于是操作员在干跑里看到的与真跑会发布的不是一个配置。这正是
    dataset-free 的 app + 已有 RAG 绑定的 agent 那个组合。

    根因是干跑那条分支**自己映射了一遍**；现在两条都走 `resolve_pulled_config`，
    所以这条断言的是同一份输入下**两条路径给出同一个 config**。
    """
    monkeypatch.setattr(
        "aiops_diagnostics.agent_debug.KbBindingResolver.validate",
        lambda self, tenant_id, knowledge_base_ids: None,
    )
    live_kb = "8701c742b28111f18637d95f7710e3a3"

    # 库里先有一个带知识库的已发布 agent（DSL 是 dataset-free 的那份真固件）。
    from aiops_diagnostics.agent_lifecycle import AgentManager, AgentStore
    from aiops_diagnostics.agent_manifest import admin_context
    from aiops_diagnostics.dify_dsl_pull import map_dsl_to_config

    seeded_declared = DSL_FIXTURE.read_text(encoding="utf-8")
    manager = AgentManager(
        AgentStore(tmp_path / "gateway.db"),
        knowledge_resolver=_AllowAll(),
        allowed_models=("deepseek-v4-flash",),
    )
    context = admin_context("T-PULL")
    seeded = manager.create(
        context,
        name="小趋-客服",
        description="",
        config=map_dsl_to_config(seeded_declared, agent_type="customer", knowledge_base_ids=(live_kb,)),
    )
    manager.publish(context, seeded.agent_id, expected_revision=seeded.revision)

    # 不给 --knowledge-base-id：干跑与真跑都必须报/存**那个**绑定。
    _serve_dify_export(monkeypatch)
    dry = CliRunner().invoke(app, [*_pull_args(tmp_path), "--dry-run"], env=_pull_env(tmp_path))
    assert dry.exit_code == 0, dry.output
    dry_config = json.loads(dry.stdout)["config"]
    assert dry_config["knowledge_base_ids"] == [live_kb], "干跑把保留的绑定报成了空"

    _serve_dify_export(monkeypatch)
    real = CliRunner().invoke(
        app, [*_pull_args(tmp_path), "--kb-url", "http://kb.invalid"], env=_pull_env(tmp_path)
    )
    assert real.exit_code == 0, real.output
    agent = manager.get(context, json.loads(real.stdout)["agent_id"])
    snapshot = manager.version(context, agent.agent_id, agent.published_version).snapshot
    assert snapshot["knowledge_base_ids"] == dry_config["knowledge_base_ids"]


# --- sync-from-dify: 运营改完 Dify 之后的那一步 --------------------------------


def _seed_registry_and_agent(tmp_path: Path, *, prompt: str, tenant: str = "T-SYNC"):
    """建一个已发布 agent，并让注册表点名它 —— 与 41 上的形状一致。"""
    from aiops_diagnostics.agent_lifecycle import AgentManager, AgentStore
    from aiops_diagnostics.agent_manifest import admin_context
    from aiops_diagnostics.dify_app_registry import DifyAppBinding, DifyAppRegistry
    from aiops_diagnostics.dify_dsl_pull import map_dsl_to_config

    manager = AgentManager(
        AgentStore(tmp_path / "gateway.db"),
        knowledge_resolver=_AllowAll(),
        allowed_models=("deepseek-v4-flash",),
    )
    context = admin_context(tenant)
    agent = manager.create(
        context,
        name="小趋-客服",
        description="同步用",
        config=map_dsl_to_config(
            DSL_FIXTURE.read_text(encoding="utf-8"),
            agent_type="customer",
            knowledge_base_ids=("kb-live",),
        ),
    )
    # 让库里的正文与 Dify 侧可区分：直接把 prompt 设成调用者给的。
    from dataclasses import replace as _replace

    agent = manager.update(
        context,
        agent.agent_id,
        expected_revision=agent.revision,
        name="小趋-客服",
        description="同步用",
        config=_replace(agent.config, prompt=prompt),
    )
    manager.publish(context, agent.agent_id, expected_revision=agent.revision)
    DifyAppRegistry(tmp_path / "gateway.db").put(
        DifyAppBinding(
            tenant_id=tenant,
            business_entry="consumer",
            app_id="a975c8e5-ad9e-425c-84c9-77581a0a2bed",
            agent_name="小趋-客服",
        )
    )
    return manager, context


def _sync_args(tmp_path: Path, *extra: str) -> list[str]:
    return ["admin", "sync-from-dify", "--db", str(tmp_path / "gateway.db"), *extra]


def test_sync_freezes_a_new_version_when_dify_changed(tmp_path: Path, monkeypatch) -> None:
    """Dify 里正文变了 ⇒ 冻结一个新版本，且**类型/知识库沿用库里的**（不由 DSL 决定）。"""
    monkeypatch.setattr(
        "aiops_diagnostics.agent_debug.KbBindingResolver.validate",
        lambda self, tenant_id, knowledge_base_ids: None,
    )
    manager, context = _seed_registry_and_agent(tmp_path, prompt="旧正文")
    _serve_dify_export(monkeypatch)  # 固件里的正文与「旧正文」不同

    result = CliRunner().invoke(
        app, [*_sync_args(tmp_path), "--kb-url", "http://kb.invalid"], env=_pull_env(tmp_path)
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert [o["action"] for o in body["outcomes"]] == ["synced"]

    agent = manager.get(context, manager.list(context)[0].agent_id)
    snap = manager.version(context, agent.agent_id, agent.published_version).snapshot
    assert snap["prompt"] != "旧正文"
    assert snap["knowledge_base_ids"] == ["kb-live"], "知识库必须沿用库里的，不能来自 DSL"
    assert snap["agent_type"] == "customer"


def test_sync_is_idempotent_and_never_publishes_a_duplicate(tmp_path: Path, monkeypatch) -> None:
    """**同一个 Dify 内容跑两次 ⇒ 只有第一个版本**。

    这条是本命令能安全重复跑的全部理由。没有「先比较」这一步，每次都会 fork 出一个
    内容相同的新版本，而线上正在服务的那一版会在跑的一刻被换掉 —— Dify 侧什么都没改。
    """
    monkeypatch.setattr(
        "aiops_diagnostics.agent_debug.KbBindingResolver.validate",
        lambda self, tenant_id, knowledge_base_ids: None,
    )
    manager, context = _seed_registry_and_agent(tmp_path, prompt="旧正文")

    _serve_dify_export(monkeypatch)
    first = CliRunner().invoke(
        app, [*_sync_args(tmp_path), "--kb-url", "http://kb.invalid"], env=_pull_env(tmp_path)
    )
    assert first.exit_code == 0, first.output
    assert json.loads(first.stdout)["outcomes"][0]["action"] == "synced"

    _serve_dify_export(monkeypatch)
    second = CliRunner().invoke(
        app, [*_sync_args(tmp_path), "--kb-url", "http://kb.invalid"], env=_pull_env(tmp_path)
    )
    assert second.exit_code == 0, second.output
    assert json.loads(second.stdout)["outcomes"][0]["action"] == "unchanged", "内容没变却又发布了一版"

    agent = manager.get(context, manager.list(context)[0].agent_id)
    assert agent.published_version == 2, f"只应有 v1（初始）与 v2（首次同步），实得 {agent.published_version}"


def test_sync_dry_run_reports_without_writing(tmp_path: Path, monkeypatch) -> None:
    """干跑报 `synced` 但**一行都不写**（看库，不看输出）。"""
    monkeypatch.setattr(
        "aiops_diagnostics.agent_debug.KbBindingResolver.validate",
        lambda self, tenant_id, knowledge_base_ids: None,
    )
    manager, context = _seed_registry_and_agent(tmp_path, prompt="旧正文")
    before = manager.get(context, manager.list(context)[0].agent_id).revision

    _serve_dify_export(monkeypatch)
    result = CliRunner().invoke(
        app,
        [*_sync_args(tmp_path, "--dry-run"), "--kb-url", "http://kb.invalid"],
        env=_pull_env(tmp_path),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["outcomes"][0]["action"] == "synced"
    agent = manager.get(context, manager.list(context)[0].agent_id)
    assert agent.revision == before, "干跑写了库"
    assert agent.published_version == 1


def test_sync_refuses_when_the_named_agent_is_missing(tmp_path: Path, monkeypatch) -> None:
    """注册表点名了一个库里没有的 agent ⇒ 报 refused，不是崩、也不是悄悄建一个。"""
    from aiops_diagnostics.dify_app_registry import DifyAppBinding, DifyAppRegistry

    DifyAppRegistry(tmp_path / "gateway.db").put(
        DifyAppBinding(
            tenant_id="T-SYNC",
            business_entry="consumer",
            app_id="a975c8e5-ad9e-425c-84c9-77581a0a2bed",
            agent_name="库里没有这个",
        )
    )
    _serve_dify_export(monkeypatch)
    result = CliRunner().invoke(
        app, [*_sync_args(tmp_path), "--kb-url", "http://kb.invalid"], env=_pull_env(tmp_path)
    )
    assert result.exit_code == 0, result.output
    outcomes = json.loads(result.stdout)["outcomes"]
    assert outcomes[0]["action"] == "refused"
    assert "库里没有这个 agent" in outcomes[0]["note"]


def test_sync_reports_a_dify_failure_per_binding_without_stopping(tmp_path: Path, monkeypatch) -> None:
    """Dify 不可达 ⇒ 如实转述 + 非零退出，且**不写库**。"""
    import urllib.error

    monkeypatch.setattr(
        "aiops_diagnostics.agent_debug.KbBindingResolver.validate",
        lambda self, tenant_id, knowledge_base_ids: None,
    )
    manager, context = _seed_registry_and_agent(tmp_path, prompt="旧正文")
    _serve_dify_export(monkeypatch, error=urllib.error.URLError("connection refused"))

    result = CliRunner().invoke(
        app, [*_sync_args(tmp_path), "--kb-url", "http://kb.invalid"], env=_pull_env(tmp_path)
    )
    assert result.exit_code == 1, result.output
    assert "DIFY_DSL_UNREACHABLE" in result.stdout
    agent = manager.get(context, manager.list(context)[0].agent_id)
    assert agent.published_version == 1


def test_sync_refuses_a_draft_agent_rather_than_publishing_it(tmp_path: Path, monkeypatch) -> None:
    """agent 不是 `published`（比如被停用/留在草稿）⇒ **拒**，不替人发布。

    这条守的是「同步只更新正文」那句话：草稿状态是一个**没有已发布版本可比**的状态，
    替它发布等于把一个人为的中间态推上线。用 `disable` 造这个状态。
    """
    monkeypatch.setattr(
        "aiops_diagnostics.agent_debug.KbBindingResolver.validate",
        lambda self, tenant_id, knowledge_base_ids: None,
    )
    manager, context = _seed_registry_and_agent(tmp_path, prompt="旧正文")
    agent = manager.list(context)[0]
    manager.disable(context, agent.agent_id, expected_revision=agent.revision)

    _serve_dify_export(monkeypatch)
    result = CliRunner().invoke(
        app, [*_sync_args(tmp_path), "--kb-url", "http://kb.invalid"], env=_pull_env(tmp_path)
    )
    assert result.exit_code == 0, result.output
    outcomes = json.loads(result.stdout)["outcomes"]
    assert outcomes[0]["action"] == "refused", outcomes
    assert "disabled" in outcomes[0]["note"]
    assert manager.get(context, agent.agent_id).status == "disabled"


def test_a_change_to_a_field_other_than_the_prompt_is_synced(tmp_path: Path, monkeypatch) -> None:
    """**除正文/模型以外的字段变了，也必须发布**（PR-1 的核心判据）。

    幂等比较曾经只比 `prompt` 与 `model`：任何别的字段变了都会被报成 `unchanged`、
    什么都不发布 —— 报告说"没什么可做的"，而变更**静默丢失**。

    ⚠️ **据实**：今天从 DSL 流入的只有 `prompt` 与 `model` 两个字段，所以这条缺陷
    在**当前**字段集下是**潜伏的**，不是线上已发。它会在 PR-2/3/5 往 config 里加字段的
    那一刻变成真缺陷 —— 本用例**先把库状态造成那一刻的形状**（已发布快照与 live 行在
    一个非正文字段上不同），从而现在就把它钉住。

    构造：直接改库里的**已发布快照**（模拟"这一版的知识库与现在不同"），
    然后跑 sync —— 正文与模型都不变，只有快照的 `knowledge_base_ids` 不同。
    """
    import sqlite3

    from aiops_diagnostics.agent_lifecycle import AgentManager, AgentStore
    from aiops_diagnostics.agent_manifest import admin_context
    from aiops_diagnostics.dify_app_registry import DifyAppBinding, DifyAppRegistry
    from aiops_diagnostics.dify_dsl_pull import map_dsl_to_config

    monkeypatch.setattr(
        "aiops_diagnostics.agent_debug.KbBindingResolver.validate",
        lambda self, tenant_id, knowledge_base_ids: None,
    )
    db = tmp_path / "gateway.db"
    manager = AgentManager(
        AgentStore(db), knowledge_resolver=_AllowAll(), allowed_models=("deepseek-v4-flash",)
    )
    context = admin_context("T-SYNC")
    agent = manager.create(
        context,
        name="小趋-客服",
        description="同步用",
        config=map_dsl_to_config(
            DSL_FIXTURE.read_text(encoding="utf-8"), agent_type="customer", knowledge_base_ids=("kb-live",)
        ),
    )
    manager.publish(context, agent.agent_id, expected_revision=agent.revision)
    DifyAppRegistry(db).put(
        DifyAppBinding(
            tenant_id="T-SYNC",
            business_entry="consumer",
            app_id="a975c8e5-ad9e-425c-84c9-77581a0a2bed",
            agent_name="小趋-客服",
        )
    )

    # 把**已发布快照**的知识库改成别的（正文与模型逐字不变）。
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT snapshot_json FROM agent_versions WHERE agent_id = ?", (agent.agent_id,)
    ).fetchone()
    snapshot = json.loads(row[0])
    assert snapshot["knowledge_base_ids"] == ["kb-live"]
    snapshot["knowledge_base_ids"] = ["kb-from-an-older-publish"]
    conn.execute(
        "UPDATE agent_versions SET snapshot_json = ? WHERE agent_id = ?",
        (json.dumps(snapshot, ensure_ascii=False), agent.agent_id),
    )
    conn.commit()
    conn.close()

    # 拉回时不给 KB ⇒ 从 live 行取（kb-live）。正文/模型与快照相同，只有快照的 KB 不同。
    _serve_dify_export(monkeypatch)
    result = CliRunner().invoke(
        app, [*_sync_args(tmp_path), "--kb-url", "http://kb.invalid"], env=_pull_env(tmp_path)
    )
    assert result.exit_code == 0, result.output
    action = json.loads(result.stdout)["outcomes"][0]["action"]
    assert action == "synced", (
        f"只有（快照的）knowledge_base_ids 不同，却报 {action!r} —— 变化被静默丢弃（比较漏了字段）"
    )


def test_a_snapshot_predating_a_field_is_not_read_as_a_change(tmp_path: Path) -> None:
    """旧快照缺某个 key ⇒ 取该字段的**默认值**，不是「缺 = 不同」。

    不加这条，第一次加入一个新字段之后，**每一个存量已发布版本**都会被读成「变了」，
    于是下一次 sync/reconcile 把全部绑定 agent 重发一遍 —— 一次静默的全量发布。
    这条直接打在 `config_from_snapshot` 上（比较的读法就是它）。
    """
    from aiops_diagnostics.agent_lifecycle import AgentConfig, config_from_snapshot

    full = AgentConfig(
        agent_type="customer",
        prompt="p",
        knowledge_base_ids=("kb",),
        model="m",
        output_contract="blocks-v1",
        opening_questions=("q",),
        quick_commands=("c",),
    )
    # 一个**未来**的字段（模拟"发布这份快照时还没有它"）：整键缺失。
    snapshot = full.to_dict()
    snapshot.pop("quick_commands")
    snapshot.pop("opening_questions")
    assert config_from_snapshot(snapshot) == AgentConfig(
        agent_type="customer",
        prompt="p",
        knowledge_base_ids=("kb",),
        model="m",
        output_contract="blocks-v1",
        opening_questions=(),
        quick_commands=(),
    )


def test_sync_translates_a_dataset_declared_in_dify(tmp_path: Path, monkeypatch) -> None:
    """**sync 这条路也要走知识库桥**（评审自查逮到：原先没走）。

    这条打在 `sync-from-dify` 上，不是打在解析器上 —— 因为缺陷恰恰是
    「解析器写好了，但 sync 传了 `knowledge_base_ids=live...`，于是它根本没被调用」。
    单测解析器会全绿，而运营真正走的那条路**桥是死的**。

    构造：Dify 的 app 声明一个外接 dataset（句柄 `kb-canary`），登记表把它映射到
    `kb-registry`；库里的 agent 目前绑的是 `kb-live`。sync 之后**必须是 `kb-registry`**。
    """

    from aiops_diagnostics.agent_lifecycle import AgentManager, AgentStore
    from aiops_diagnostics.agent_manifest import admin_context
    from aiops_diagnostics.dify_app_registry import DifyAppBinding, DifyAppRegistry
    from aiops_diagnostics.dify_dsl_pull import map_dsl_to_config

    monkeypatch.setattr(
        "aiops_diagnostics.agent_debug.KbBindingResolver.validate",
        lambda self, tenant_id, knowledge_base_ids: None,
    )
    # Dify 声明一个 dataset；控制台的 dataset 路由回答句柄 `kb-canary`。
    dsl = DSL_FIXTURE.read_text(encoding="utf-8").replace(
        "  dataset_configs:\n    retrieval_model: multiple\n",
        "  dataset_configs:\n    retrieval_model: multiple\n"
        "    datasets:\n      datasets:\n"
        "      - dataset:\n          enabled: true\n"
        "          id: df0f1d4a-3d6e-404e-9656-c88da6df3d1e\n",
    )
    _serve_dify_export(monkeypatch, dsl_text=dsl)
    # 控制台的 dataset 导出：回答句柄（这一跳由 DifyDslClient 发出）。
    monkeypatch.setattr(
        "aiops_diagnostics.dify_dsl_pull.DifyDslClient.dataset_knowledge_id",
        lambda self, dataset_id: "kb-canary",
    )
    env = _pull_env(tmp_path)
    env["AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS"] = "kb-canary:T-SYNC:kb-registry"

    db = tmp_path / "gateway.db"
    manager = AgentManager(
        AgentStore(db), knowledge_resolver=_AllowAll(), allowed_models=("deepseek-v4-flash",)
    )
    context = admin_context("T-SYNC")
    agent = manager.create(
        context,
        name="小趋-客服",
        description="同步用",
        config=map_dsl_to_config(dsl, agent_type="customer", knowledge_base_ids=("kb-live",)),
    )
    manager.publish(context, agent.agent_id, expected_revision=agent.revision)
    DifyAppRegistry(db).put(
        DifyAppBinding(
            tenant_id="T-SYNC",
            business_entry="consumer",
            app_id="a975c8e5-ad9e-425c-84c9-77581a0a2bed",
            agent_name="小趋-客服",
        )
    )

    _serve_dify_export(monkeypatch, dsl_text=dsl)
    result = CliRunner().invoke(app, [*_sync_args(tmp_path), "--kb-url", "http://kb.invalid"], env=env)
    assert result.exit_code == 0, result.output
    live = manager.list(context)[0]
    snapshot = manager.version(context, live.agent_id, live.published_version).snapshot
    assert snapshot["knowledge_base_ids"] == ["kb-registry"], (
        "sync 没有走知识库桥 —— Dify 声明的 dataset 被忽略了（运营换的知识库不生效）"
    )


def test_sync_refuses_a_dataset_registered_to_another_tenant(tmp_path: Path, monkeypatch) -> None:
    """sync 路上，**别的租户**的句柄 ⇒ 拒（跨租户守卫必须在真跑路径上生效）。

    与上一条同理：守卫写在解析器里、单测覆盖过，但若 sync 没把 `tenant_id` 传下去，
    它在运营真正走的那条路上就是死的。
    """
    from aiops_diagnostics.agent_lifecycle import AgentManager, AgentStore
    from aiops_diagnostics.agent_manifest import admin_context
    from aiops_diagnostics.dify_app_registry import DifyAppBinding, DifyAppRegistry
    from aiops_diagnostics.dify_dsl_pull import map_dsl_to_config

    monkeypatch.setattr(
        "aiops_diagnostics.agent_debug.KbBindingResolver.validate",
        lambda self, tenant_id, knowledge_base_ids: None,
    )
    dsl = DSL_FIXTURE.read_text(encoding="utf-8").replace(
        "  dataset_configs:\n    retrieval_model: multiple\n",
        "  dataset_configs:\n    retrieval_model: multiple\n"
        "    datasets:\n      datasets:\n"
        "      - dataset:\n          enabled: true\n"
        "          id: df0f1d4a-3d6e-404e-9656-c88da6df3d1e\n",
    )
    _serve_dify_export(monkeypatch, dsl_text=dsl)
    monkeypatch.setattr(
        "aiops_diagnostics.dify_dsl_pull.DifyDslClient.dataset_knowledge_id",
        lambda self, dataset_id: "kb-canary",
    )
    env = _pull_env(tmp_path)
    # 句柄登记在**另一个租户**名下。
    env["AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS"] = "kb-canary:T-OTHER:kb-not-ours"

    db = tmp_path / "gateway.db"
    manager = AgentManager(
        AgentStore(db), knowledge_resolver=_AllowAll(), allowed_models=("deepseek-v4-flash",)
    )
    context = admin_context("T-SYNC")
    agent = manager.create(
        context,
        name="小趋-客服",
        description="同步用",
        config=map_dsl_to_config(dsl, agent_type="customer", knowledge_base_ids=("kb-live",)),
    )
    manager.publish(context, agent.agent_id, expected_revision=agent.revision)
    DifyAppRegistry(db).put(
        DifyAppBinding(
            tenant_id="T-SYNC",
            business_entry="consumer",
            app_id="a975c8e5-ad9e-425c-84c9-77581a0a2bed",
            agent_name="小趋-客服",
        )
    )

    _serve_dify_export(monkeypatch, dsl_text=dsl)
    result = CliRunner().invoke(app, [*_sync_args(tmp_path), "--kb-url", "http://kb.invalid"], env=env)
    assert result.exit_code == 1, result.output
    assert "另一个租户" in result.stdout or "租户" in result.stdout
    live = manager.list(context)[0]
    assert live.published_version == 1, "被拒的同步不该发布任何版本"
