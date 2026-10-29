"""把取证时间渲染成读者能核对的写法。

为什么单独一个模块：`consumer_order_chat`、`consumer_billing`、`consumer_language`
三条渲染路都要用它，而 `consumer_language` 被 `consumer_order_chat` 导入 —— 让它们互相
import 会在包内形成一个循环边（`tests/test_readme_architecture.py` 会红）。一个只依赖
标准库的函数放在这里，三条路都能用，且不引入方向相反的依赖。

微秒保留在原始数据里（快照与证据需要它），只是不摆给用户看：`2026-10-29T12:34:45.123456+00:00`
对读报告的人没有信息量，而 `2026-10-29 12:34:45 UTC` 能直接对上手机时钟。

解析失败原样返回 —— 把一个读不出来的时间戳硬凑成人类格式，比显示原值更容易误导。
"""

from __future__ import annotations

from datetime import UTC, datetime


def readable_time(iso: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return str(iso)
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") + " UTC"
