from __future__ import annotations

from pathlib import Path


def test_env_example_documents_partial_cutover_classification() -> None:
    text = (Path(__file__).parents[1] / ".env.example").read_text(encoding="utf-8")

    assert "AIOPS_HTTP_BASE_URL=" in text
    assert "AIOPS_HTTP_INTERNAL_TOKEN_SECRET=" in text
    assert "AIOPS_HTTP_INTERNAL_TOKEN_EXPIRE_SECONDS=" in text
    assert (
        "# WARNING: TDENGINE 直连凭据保留,等 git.qushiyun.com/iot/tsdata 补完 token 校验后回收(D 方案)"
        in text
    )
    assert "# REMOVED (Phase 3a): 不再配置 MySQL 直连凭据；/diag/* HTTP 接口已接管" in text


def test_env_example_phase_3a_removes_mysql_and_redis_direct_configuration() -> None:
    text = (Path(__file__).parents[1] / ".env.example").read_text(encoding="utf-8")
    active_lines = [line.split(" #", 1)[0] for line in text.splitlines() if line.strip() and "=" in line]

    for prefix in ("AIOPS_MYSQL_", "AIOPS_REDIS_", "AIOPS_SSH_MYSQL_", "AIOPS_SSH_REDIS_"):
        assert not [line for line in active_lines if line.startswith(prefix)]

    assert any(line.startswith("AIOPS_TDENGINE_URL=") for line in active_lines)
    assert any(line.startswith("AIOPS_SSH_TDENGINE_HOST=") for line in active_lines)
