"""控制台发布桥 CLI 入口（``python -m dify_console_bridge``）。

将进程入口与库模块 ``bridge.py`` 分离，消除 ``python -m`` 将同一文件同时加载为
``__main__`` 与包模块两份实例的双重加载缺陷。
"""

from __future__ import annotations

from dify_console_bridge.bridge import main

if __name__ == "__main__":  # pragma: no cover - 进程入口
    main()
