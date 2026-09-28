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
    assert "旧 diagnostic_readonly MySQL 账号从未在生产库存在，已退役" in text

    # Redis scoped 直连：最小权限 ACL 用户（白名单流只读）
    assert "AIOPS_REDIS_USER=diagnostic_readonly" in _active_lines(text)
    assert "最小权限 ACL 用户 diagnostic_readonly" in text

    # TDengine 受控代理：USER 为代理 client 凭据而非 TDengine 用户
    assert "AIOPS_TDENGINE_URL=http://127.0.0.1:16041" in _active_lines(text)
    assert "AIOPS_TDENGINE_USER=diagnostic_readonly" in _active_lines(text)
    assert "代理 client" in text

    # scoped 直连所需的 SSH 转发目标
    assert any(line.startswith("AIOPS_SSH_MYSQL_HOST=") for line in _active_lines(text))
    assert any(line.startswith("AIOPS_SSH_REDIS_HOST=") for line in _active_lines(text))
    assert any(line.startswith("AIOPS_SSH_TDENGINE_HOST=") for line in _active_lines(text))


def test_env_example_marks_diag_http_as_deprecated() -> None:
    """旧 /diag/* HTTP 方案不再是基线，模板只保留注释化回退参考。"""
    text = _template()

    assert "DEPRECATED（PRD #23 2026-08-31 起 /diag/* HTTP 方案不再是基线" in text
    assert "默认诊断路径已改为受限直连（ScopedSources）" in text
    active = _active_lines(text)
    assert not [line for line in active if line.startswith("AIOPS_HTTP_")]
    assert not [line for line in active if line.startswith("AIOPS_DIAG_API_")]


def test_env_example_documents_upms_permission_context_boundary() -> None:
    text = _template()

    assert "AIOPS_UPMS_BASE_URL=" in text
    assert "AIOPS_UPMS_TIMEOUT_SECONDS=" in text
    assert "调用者平台凭证由运维入口透传，不在本文件配置" in text
    assert not [line for line in _active_lines(text) if "UPMS" in line and "TOKEN" in line]


def test_env_example_documents_the_upms_inside_token_boundary() -> None:
    """#424：服务侧内部凭据只在私有配置取值，模板只声明不落值。"""
    text = _template()

    assert "AIOPS_UPMS_INSIDE_TOKEN" in text
    assert "只在私有 production.env 里取值，模板不落值" in text
    assert not [line for line in text.splitlines() if line.startswith("AIOPS_UPMS_INSIDE_TOKEN=")]
    assert not [line for line in _active_lines(text) if "INSIDE_TOKEN" in line]


def test_env_example_documents_dis_point_scope_boundary() -> None:
    text = _template()

    assert "AIOPS_DIS_BASE_URL=" in text
    assert "AIOPS_DIS_TOKEN=" in text
    assert "AIOPS_DIS_TIMEOUT_SECONDS=" in text
    assert "服务侧静态令牌" in text
