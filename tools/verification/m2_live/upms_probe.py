"""从 Gateway 所在网络核验 C→B 身份接口；不查真实用户，不创建业务任务。"""

from __future__ import annotations

import os
from pathlib import Path

from aiops_diagnostics.common.config import Settings, UpmsSettings
from aiops_diagnostics.identity.scope_context import ScopeError
from aiops_diagnostics.identity.third_session_auth import UpmsBSubjectDirectory


def check(settings: UpmsSettings) -> bool:
    if not settings.base_url or not settings.inside_token:
        print("  FAIL identity service configuration missing")
        return False
    try:
        # 与实际解析器共用协议和错误映射。空结果只证明接口可读，不证明任何用户获授权。
        UpmsBSubjectDirectory(settings, settings.inside_token).users_by_c_user_id("__aiops_preflight__")
    except (ScopeError, ValueError, OSError) as exc:
        print(f"  FAIL identity service: {type(exc).__name__}")
        return False
    print("  ok   identity mapping endpoint reachable (synthetic lookup; user authorization unverified)")
    return True


def main() -> int:
    try:
        settings = Settings.from_config(Path(os.environ["AIOPS_GATEWAY_SERVER_CONFIG_FILE"]))
    except (KeyError, ValueError, OSError) as exc:
        print(f"  FAIL identity configuration: {type(exc).__name__}")
        return 1
    return 0 if check(settings.upms) else 1


if __name__ == "__main__":
    raise SystemExit(main())
