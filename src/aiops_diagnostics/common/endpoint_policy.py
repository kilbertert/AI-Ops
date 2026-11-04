"""出站端点 URL 的安全规则 —— `is_loopback_host` 与 `require_secure_endpoint` 的**唯一**定义。

「https 哪里都行，http 只给回环」这句话此前被逐字抄了五份、各带一条错误文案；另有
五类**携带服务凭据**的端点整条不检查它。而「什么算回环」还有第二种渲染
（`ipaddress.ip_address(host).is_loopback`），对同一个输入给出相反答案 ——
字面量集合放行 `localhost` 但拒绝 `127.0.0.2`，`is_loopback` 正好相反。

本模块只含纯函数与冻结值（沿用 `rules.py` / `order_visibility.py` 的形态）。

## 「回环」的定义

取**并集**，两者任一为真即回环：

1. `localhost`（不区分大小写，含 `localhost.`）；
2. `ipaddress.ip_address(host).is_loopback`（覆盖整个 `127.0.0.0/8` 与 `::1`）。

并集而不是二选一：两种渲染各自都能正确识别对方漏掉的一类，取交集会把今天能用的
配置变成拒绝，取并集只把「两种渲染本来就都该放行」的输入放行。**不允许再有第三种渲染**。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

#: 允许明文 http 的主机名。判据是 `is_loopback_host`，这里只是它的一个真值来源。
LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost."})


def is_loopback_host(host: str | None) -> bool:
    """这个主机名/地址是不是「本机」。**唯一判据。**"""
    if not host:
        return False
    if host.lower() in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_secure_endpoint(url: str, *, what: str) -> str:
    """绝对 http(s) URL、无 userinfo/query/fragment，且 **https 除非回环**。

    ``what`` 只用于错误文案（各调用点自己的领域措辞）；判据只有这一份。
    返回去掉尾部斜杠的地址（沿用既有调用点的约定）。
    """
    try:
        parsed = urlsplit(url.strip())
        _ = parsed.port  # 非法端口会在这里抛 ValueError
    except ValueError as exc:
        raise ValueError(f"{what}: invalid URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{what}: must be a complete HTTP or HTTPS URL")
    if parsed.scheme == "http" and not is_loopback_host(parsed.hostname):
        raise ValueError(f"{what}: remote URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{what}: must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{what}: must not contain query or fragment")
    return url.strip().rstrip("/")
