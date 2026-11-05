"""#973 的机械守卫：「一个 Gateway 设置从哪里读」只有 ``SETTING_SOURCES`` 一个权威。

这里的用例按**整张表**参数化 —— 删一行表、改一个 ``file`` 列、把某个字段改回
手写的 ``_env(...) or _file_value(...)`` 链，都该有对应的红色用例，而不是靠
评审记住 76 行各自读哪里。
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiops_diagnostics.common.private_files import PrivatePathError, write_private_text
from aiops_diagnostics.gateway.gateway_config import (
    DERIVED_SETTING_FIELDS,
    SETTING_SOURCES,
    GatewayServerSettings,
    _parse_source_value,
)

#: 每种 kind 的「文件里写的」哨兵值 —— 必须与该字段的默认值取到**不同**结果，
#: 否则「读到了文件」与「落回默认」分不开。``bool_not_falsy`` 给 ``off``：默认 True
#: 的字段读到它得 False。``bool`` 给 ``1``：默认 False 的字段读到它得 True。
_FILE_SENTINEL = {"str": "sentinel-value", "int": "77", "float": "7.5", "bool": "1", "bool_not_falsy": "off"}
#: 每种 kind 的「进程 env 里写的」哨兵值 —— 与文件哨兵解析结果不同，才能证明 env 赢
#: （``bool_not_falsy`` 给 ``1`` ⇒ True，与文件 ``off`` ⇒ False 区分开）。
_ENV_SENTINEL = {"str": "env-wins", "int": "42", "float": "4.5", "bool": "0", "bool_not_falsy": "1"}

_DERIVED_ENVS = {
    "AIOPS_GATEWAY_DATA_HOME",
    "AIOPS_GATEWAY_DATABASE_FILE",
    "AIOPS_GATEWAY_SERVER_CONFIG_FILE",
}

_FIELD_TYPES = {
    "str": {"str"},
    "int": {"int"},
    "float": {"float"},
    "bool": {"bool"},
    "bool_not_falsy": {"bool"},
}

#: ``file=False`` 是一份**决定名单**，不从表推导（表会跟着变异走 —— 参数化用例读的是
#: 表自己，翻一列它跟着翻）。翻成 True 是把一个进程级旋钮变成文件可读（读取面悄悄
#: 扩大）；翻反方向则让运维写进 ``production.env`` 的键静默不生效。两个方向都钉死。
_PROCESS_ENV_ONLY = frozenset(
    {
        "bind_host",
        "port",
        "allow_fixtures",
        "max_workers",
        "introspection_url",
        "introspection_client_id",
        "introspection_client_secret",
        "standard_api_audience",
        "introspection_timeout_seconds",
        "kb_service_timeout_seconds",
        "media_ttl_seconds",
        "dify_knowledge_autoregister",
        "ragflow_tenant_domain",
        "ragflow_embedding_model",
        "knowledge_hop_ttl_seconds",
        "routing_risk_at_least",
        "routing_confidence_at_least",
        "routing_risk_always_asks",
        "log_level",
    }
)


def _server_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    config = tmp_path / "production.env"
    write_private_text(config, body)
    monkeypatch.setenv("AIOPS_GATEWAY_SERVER_CONFIG_FILE", str(config))
    return config


def test_the_table_covers_every_dataclass_field_exactly_once() -> None:
    """表 ↔ 字段的双向完备性：少登记一行，那个字段就静默只吃默认值 —— 本仓
    「声明了、路由读了、from_env 没赋值」的形状（ragflow_console 漏过一次）。"""
    declared = {f.name for f in fields(GatewayServerSettings)}

    assert set(SETTING_SOURCES) | DERIVED_SETTING_FIELDS == declared
    assert not set(SETTING_SOURCES) & DERIVED_SETTING_FIELDS


def test_every_row_is_faithful_to_the_field_it_declares() -> None:
    """kind 与默认值都对着 dataclass 字段核 —— 表是自己对的，不是抄过来抄错。"""
    declared = {f.name: f for f in fields(GatewayServerSettings)}

    for name, source in SETTING_SOURCES.items():
        field = declared[name]
        assert source.env.startswith("AIOPS_GATEWAY_"), name
        assert field.type in _FIELD_TYPES[source.kind], name
        assert source.default == field.default, name


def test_env_names_are_unique_across_the_table() -> None:
    names = [source.env for source in SETTING_SOURCES.values()]
    assert len(names) == len(set(names))


def test_the_process_env_only_ledger_is_what_the_table_declares() -> None:
    assert {name for name, source in SETTING_SOURCES.items() if not source.file} == _PROCESS_ENV_ONLY


def test_the_irregular_env_names_are_pinned() -> None:
    """两个不按 ``AIOPS_GATEWAY_<FIELD>`` 命名的键 —— 改名是部署破坏，钉死。"""
    assert SETTING_SOURCES["bind_host"].env == "AIOPS_GATEWAY_BIND"
    assert SETTING_SOURCES["trust_company_payload"].env == "AIOPS_GATEWAY_COMPANY_TRUST_PAYLOAD"


def test_from_env_has_no_per_field_reads_left() -> None:
    """AST 守卫：``from_env`` 体里只允许出现三个派生路径的键名。任何一个手写的
    ``_env("AIOPS_GATEWAY_...")`` 回潮都是这条用例变红 —— grep 数得出来，
    但只有 AST 能区分「函数体里的读」与「注释里的字」。"""
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(GatewayServerSettings.from_env)))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("AIOPS_GATEWAY_")
    }
    assert literals == _DERIVED_ENVS


@pytest.mark.parametrize("field_name", sorted(SETTING_SOURCES))
def test_every_field_reads_only_its_declared_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field_name: str
) -> None:
    """整表扫描：``file=True`` 的字段必须读到文件值；``file=False`` 的字段把
    同一行写进文件必须**不生效**（落回默认）—— 后者就是「写进 production.env
    静默不生效」那条陷阱的机械判据。"""
    source = SETTING_SOURCES[field_name]
    _server_config(tmp_path, monkeypatch, f"{source.env}={_FILE_SENTINEL[source.kind]}\n")
    monkeypatch.delenv(source.env, raising=False)

    actual = getattr(GatewayServerSettings.from_env(), field_name)

    if source.file:
        assert actual == _parse_source_value(source, _FILE_SENTINEL[source.kind])
    else:
        assert actual == source.default


@pytest.mark.parametrize("field_name", sorted(SETTING_SOURCES))
def test_a_present_env_wins_over_the_file_on_every_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field_name: str
) -> None:
    source = SETTING_SOURCES[field_name]
    _server_config(tmp_path, monkeypatch, f"{source.env}={_FILE_SENTINEL[source.kind]}\n")
    monkeypatch.setenv(source.env, _ENV_SENTINEL[source.kind])

    actual = getattr(GatewayServerSettings.from_env(), field_name)

    assert actual == _parse_source_value(source, _ENV_SENTINEL[source.kind])


@pytest.mark.parametrize("field_name", sorted(name for name, s in SETTING_SOURCES.items() if s.file))
def test_a_present_but_empty_env_is_the_value_not_a_fallthrough(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field_name: str
) -> None:
    """「缺失才回退」的另一半：``KEY=`` 的空串是**值**，不是「没配」。
    str 字段取到 ``""``；非 str 字段空串没有可辩护的含义，等于默认值 ——
    两种结果都**不是**文件里的那行。"""
    source = SETTING_SOURCES[field_name]
    _server_config(tmp_path, monkeypatch, f"{source.env}={_FILE_SENTINEL[source.kind]}\n")
    monkeypatch.setenv(source.env, "")

    actual = getattr(GatewayServerSettings.from_env(), field_name)

    expected = "" if source.kind == "str" else source.default
    assert actual == expected


def test_missing_everywhere_lands_on_the_declared_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _server_config(tmp_path, monkeypatch, "# empty\n")
    monkeypatch.delenv("AIOPS_GATEWAY_KB_SEARCH_MAX_RESULTS", raising=False)

    assert GatewayServerSettings.from_env().kb_search_max_results == 10


def test_garbage_numeric_values_fail_at_construction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """数值字段的非法值不回落默认 —— 「超时调大了」不许是一句配置支撑不了的话。"""
    _server_config(tmp_path, monkeypatch, "AIOPS_GATEWAY_KB_SEARCH_MAX_RESULTS=ten\n")
    monkeypatch.delenv("AIOPS_GATEWAY_KB_SEARCH_MAX_RESULTS", raising=False)

    with pytest.raises(ValueError):
        GatewayServerSettings.from_env()


def test_an_explicitly_named_missing_file_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Option A（#973）：显式点名一份不存在的文件是写错了路径 —— 启动失败并
    报出路径，不是「什么都没配」地把服务起起来。"""
    missing = tmp_path / "missing.env"
    monkeypatch.setenv("AIOPS_GATEWAY_SERVER_CONFIG_FILE", str(missing))

    with pytest.raises(PrivatePathError, match="missing.env"):
        GatewayServerSettings.from_env()


def test_an_unreadable_config_file_is_an_error_not_an_empty_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0644 的私有配置过去被静默当空表 —— 服务照起、每个请求收误导性 401/503。
    Option A 直出 ``PrivatePathError``（点名文件与 mode），与 ``Settings.from_config``
    同一个答案。"""
    config = tmp_path / "production.env"
    write_private_text(config, "AIOPS_GATEWAY_KB_SEARCH_MAX_RESULTS=15\n")
    config.chmod(0o644)
    monkeypatch.setenv("AIOPS_GATEWAY_SERVER_CONFIG_FILE", str(config))
    monkeypatch.delenv("AIOPS_GATEWAY_KB_SEARCH_MAX_RESULTS", raising=False)

    with pytest.raises(PrivatePathError, match="644"):
        GatewayServerSettings.from_env()


def test_a_missing_default_path_file_is_a_legal_empty_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认路径下没有配置是合法形态（新装、单测）：``from_env`` 不炸，
    字段全落默认。``validate()`` 才负责「起监听必须有文件」那一半。"""
    monkeypatch.setenv("AIOPS_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("AIOPS_CONFIG_FILE", raising=False)
    monkeypatch.delenv("AIOPS_GATEWAY_SERVER_CONFIG_FILE", raising=False)

    settings = GatewayServerSettings.from_env()

    assert settings.server_config_file == tmp_path / "production.env"
    assert settings.kb_search_max_results == 10


def test_the_two_readers_give_the_same_answer_on_empty_and_on_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#973 的另一半：同一进程里两套读取器（``Settings.from_config`` 与
    ``GatewayServerSettings.from_env``）对同一份文件不能再给相反答案 ——
    空串两边都按「显式覆盖为空」，文件不可读两边都是启动失败。"""
    from aiops_diagnostics.common.config import Settings

    config = tmp_path / "production.env"
    write_private_text(
        config,
        "AIOPS_MYSQL_HOST=from-file\nAIOPS_GATEWAY_KB_SERVICE_BASE_URL=http://kb.example\n",
    )
    monkeypatch.setenv("AIOPS_GATEWAY_SERVER_CONFIG_FILE", str(config))
    monkeypatch.setenv("AIOPS_MYSQL_HOST", "")
    monkeypatch.setenv("AIOPS_GATEWAY_KB_SERVICE_BASE_URL", "")

    assert Settings.from_config(config).mysql.host == ""
    assert GatewayServerSettings.from_env().kb_service_base_url == ""

    config.chmod(0o640)
    with pytest.raises(ValueError):  # `from_config` 把 PrivatePathError 包成 ValueError
        Settings.from_config(config)
    with pytest.raises(PrivatePathError):
        GatewayServerSettings.from_env()


def test_the_admin_config_merge_vocabulary_is_derived_not_handwritten() -> None:
    """``--config`` 能合并哪些键，词表由表派生 —— 手抄一份就是「同一条规则
    两份实现」的起点（上一次就是 10 行手抄词表漏掉新字段）。"""
    from aiops_diagnostics.admin_cli import _SETTING_FOR

    assert {s.env: n for n, s in SETTING_SOURCES.items() if s.file} == _SETTING_FOR


def test_the_admin_config_merge_honours_presence_and_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """合并的判据是「进程 env 有没有这个键」：``KEY=`` 的空串也是一个值、不输；
    合并进来的值按字段的 ``kind`` 解析，不写裸字符串进 int 字段。"""
    from aiops_diagnostics.admin_cli import _settings_with_config_file

    _server_config(tmp_path, monkeypatch, "# empty\n")
    merge = tmp_path / "merge.env"
    write_private_text(
        merge,
        "AIOPS_GATEWAY_KB_SEARCH_MAX_RESULTS=15\nAIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN=file-token\n",
    )
    monkeypatch.setenv("AIOPS_GATEWAY_THIRD_SESSION_SERVICE_TOKEN", "")
    monkeypatch.delenv("AIOPS_GATEWAY_KB_SEARCH_MAX_RESULTS", raising=False)
    ctx = SimpleNamespace(obj={"config_file": merge})

    settings = _settings_with_config_file(ctx)  # type: ignore[arg-type]

    assert settings.kb_search_max_results == 15
    assert settings.third_session_service_token == ""


def test_the_admin_config_merge_refuses_a_missing_named_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--config`` 是显式点名：不存在的路径按 Option A 拒掉，不静默合个空表。"""
    from aiops_diagnostics.admin_cli import _settings_with_config_file

    _server_config(tmp_path, monkeypatch, "# empty\n")
    ctx = SimpleNamespace(obj={"config_file": tmp_path / "typo.env"})

    with pytest.raises(PrivatePathError, match="typo.env"):
        _settings_with_config_file(ctx)  # type: ignore[arg-type]
