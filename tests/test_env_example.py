from __future__ import annotations

from pathlib import Path


def _template() -> str:
    return (Path(__file__).parents[1] / ".env.example").read_text(encoding="utf-8")


def _active_lines(text: str) -> list[str]:
    return [line.split(" #", 1)[0] for line in text.splitlines() if line.strip() and "=" in line]


def test_env_example_documents_scoped_direct_datasources() -> None:
    """PRD #23：MySQL/Redis 受限直连为默认路径，凭据为最小只读账号。"""
    text = _template()

    # MySQL scoped 直连：最小只读账号 aiops_ro（旧 diagnostic_readonly MySQL 账号已退役）
    assert "AIOPS_MYSQL_USER=aiops_ro" in _active_lines(text)
    assert any(line.startswith("AIOPS_MYSQL_DATABASE=cloud_charging_pile") for line in _active_lines(text))
    # 注释必须说明旧账号**已退役**（防止有人照着历史文档去建它）。只钉关键词，不钉整句。
    assert "已退役" in text and "diagnostic_readonly" in text

    # Redis scoped 直连：最小权限 ACL 用户（白名单流只读）
    assert "AIOPS_REDIS_USER=diagnostic_readonly" in _active_lines(text)
    assert "ACL" in text and "最小权限" in text

    # TDengine 受控代理：USER 为代理 client 凭据而非 TDengine 用户
    assert "AIOPS_TDENGINE_URL=http://127.0.0.1:16041" in _active_lines(text)
    assert "AIOPS_TDENGINE_USER=diagnostic_readonly" in _active_lines(text)
    assert "代理 client" in text

    # scoped 直连所需的 SSH 转发目标
    assert any(line.startswith("AIOPS_SSH_MYSQL_HOST=") for line in _active_lines(text))
    assert any(line.startswith("AIOPS_SSH_REDIS_HOST=") for line in _active_lines(text))
    assert any(line.startswith("AIOPS_SSH_TDENGINE_HOST=") for line in _active_lines(text))


def test_env_example_has_no_retired_runtime_configuration() -> None:
    active = _active_lines(_template())
    retired = (
        "AIOPS_HTTP_",
        "AIOPS_DIAG_API_",
        "AIOPS_CODEX_BIN=",
        "AIOPS_CODEX_RUNTIME_HOME=",
        "AIOPS_AGENT_MAX_",
        "AIOPS_RESPONSES_ADAPTER_",
    )
    assert not [line for line in active if line.startswith(retired)]


def test_env_example_documents_upms_permission_context_boundary() -> None:
    text = _template()

    assert "AIOPS_UPMS_BASE_URL=" in text
    assert "AIOPS_UPMS_TIMEOUT_SECONDS=" in text
    assert "运维入口透传" in text and "不在本文件配置" in text
    assert not [line for line in _active_lines(text) if "UPMS" in line and "TOKEN" in line]


def test_env_example_documents_the_upms_inside_token_boundary() -> None:
    """#424：服务侧内部凭据只在私有配置取值，模板只声明不落值。"""
    text = _template()

    assert "AIOPS_UPMS_INSIDE_TOKEN" in text
    assert "production.env" in text and "模板不落值" in text
    assert not [line for line in text.splitlines() if line.startswith("AIOPS_UPMS_INSIDE_TOKEN=")]
    assert not [line for line in _active_lines(text) if "INSIDE_TOKEN" in line]


def test_env_example_keeps_the_company_token_path_off_by_default() -> None:
    """#443：三个键必须**声明的存在、且缺省为空**——「先合不启用」的机制保证，模板不落值。"""
    text = _template()
    active = _active_lines(text)

    for name in (
        "AIOPS_GATEWAY_COMPANY_CHECK_TOKEN_URL",
        "AIOPS_GATEWAY_COMPANY_TOKEN_CLIENT_ID",
        "AIOPS_GATEWAY_COMPANY_TOKEN_CLIENT_SECRET",
    ):
        assert f"{name}=" in active, f"{name} 必须在模板里声明为空值"
    # 客户端密钥与服务侧凭据一样：只在私有 production.env 里取值，模板不落值。
    assert "LEAVE ALL THREE EMPTY" in text


def test_env_example_documents_the_company_source_key_gate() -> None:
    """#444：来源密钥必须声明、缺省为空，且模板要说清它是「可信那一跳注入」而非前端持有。"""
    text = _template()
    active = _active_lines(text)

    assert "AIOPS_GATEWAY_COMPANY_SOURCE_KEY=" in active
    # 头名写在模板里：它是那条信任链的对外契约（网关注入方也要按这个名字发）。
    assert "X-AIOps-Source-Key" in text
    # fail closed 那句与「覆盖式注入」的要求都写在说明里（被换行拆开，逐段断言）。
    assert "fail closed" in text and "不是放行" in text
    assert "覆盖式注入" in text


def test_env_example_documents_dis_point_scope_boundary() -> None:
    text = _template()

    assert "AIOPS_DIS_BASE_URL=" in text
    assert "AIOPS_DIS_TOKEN=" in text
    assert "AIOPS_DIS_TIMEOUT_SECONDS=" in text
    assert "服务侧静态令牌" in text


def test_env_example_mirrors_the_setting_source_table() -> None:
    """#973：模板键集 = ``SETTING_SOURCES`` 键名 ∪ 三个派生路径键，逐键恰好一次；
    只读进程环境的键（``file=False`` 与派生项）行尾必须带 `# 仅进程环境`，
    其余键不得带 —— 标记错向两个方向都会让运维把键写进不读它的那份文件。"""
    import re

    from aiops_diagnostics.gateway.gateway_config import SETTING_SOURCES

    key_lines: dict[str, list[str]] = {}
    for line in _template().splitlines():
        match = re.match(r"^(AIOPS_GATEWAY_[A-Z0-9_]+)=", line)
        if match:
            key_lines.setdefault(match.group(1), []).append(line)

    derived = {
        "AIOPS_GATEWAY_DATA_HOME",
        "AIOPS_GATEWAY_DATABASE_FILE",
        "AIOPS_GATEWAY_SERVER_CONFIG_FILE",
    }
    assert set(key_lines) == {s.env for s in SETTING_SOURCES.values()} | derived
    assert all(len(lines) == 1 for lines in key_lines.values())

    process_only = {s.env for s in SETTING_SOURCES.values() if not s.file} | derived
    for env, (line,) in key_lines.items():
        assert ("仅进程环境" in line) == (env in process_only), env
