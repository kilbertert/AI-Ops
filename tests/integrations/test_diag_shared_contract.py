"""`DiagQueryController.java` 的跨端点约定 —— 四个 `/diag/*` 端点共用的一层。

这些断言曾经逐字复制在四个 `test_diag_*_contract.py` 里（同一份 controller、
同一组 header 自校验、同一个 401 封套）。四份拷贝不会同时被改，于是"其中三份
还对"变成没人能一眼看出的状态；改一次 controller 就要改四处。

端点各自的语义（列清单、SQL 占位符、注入字符、封套类型）仍留在各自文件里 ——
那是不同的契约，不是重复。
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
JAVA_ROOT = (
    PROJECT_ROOT
    / "integrations"
    / "cpms-java"
    / "cloud-charging-pile-web"
    / "src"
    / "main"
    / "java"
    / "com"
    / "qushiyun"
    / "cloud"
    / "charging"
    / "pile"
    / "web"
)
CONTROLLER = JAVA_ROOT / "controller" / "DiagQueryController.java"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_controller_self_validates_internal_token_headers() -> None:
    """每个端点自己校验内部令牌，而不是靠上一层拦截器。

    控制器是团队仓里的独立补丁；它不能假设调用方一定经过某个 filter，
    所以两个 header 都以 `required = false` 显式接住、再由控制器自己判。
    """
    source = _read(CONTROLLER)
    assert '@RequestHeader(value = "X-Internal-Token", required = false)' in source
    assert '@RequestHeader(value = "X-Request-Timestamp", required = false)' in source
    assert "internalTokenManager.validateToken(" in source


def test_invalid_token_returns_http_401_and_r_envelope() -> None:
    """令牌不对必须是 401 + `R` 封套，而不是 200 带一句错误话。"""
    source = _read(CONTROLLER)
    assert "HttpStatus.UNAUTHORIZED" in source
    assert 'R.failed(401, "令牌无效或过期")' in source


def test_java_artifact_does_not_hardcode_internal_token_secret() -> None:
    """源码里不能出现内部令牌的**明文密钥**；密钥由部署注入。"""
    source = _read(CONTROLLER)
    assert "qushiyun-internal-secret-2024" not in source
    assert "InternalTokenManager" in source
