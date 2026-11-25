"""Dify 控制台登录代理 CLI 入口（``python -m dify_login_proxy``）。"""

from __future__ import annotations

from dify_login_proxy.server import main

if __name__ == "__main__":  # pragma: no cover - 进程入口
    main()
