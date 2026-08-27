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
    assert "# DEPRECATED: 改由 /diag/* HTTP 接口访问,本仓 HybridSources 仍能 fallback,但生产应禁用" in text
