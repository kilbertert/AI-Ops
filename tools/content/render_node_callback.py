#!/usr/bin/env python3
"""把 `gateway-callback.conf.template` 渲染成可在宿主上直接用的配置。

模板里的**具名部署占位符**（`AIOPS_NODE_BRIDGE` / `AIOPS_NODE_STATE` /
`AIOPS_NODE_CONFIG` / `AIOPS_PRODUCTION_GATEWAY`）仍由部署方给值；本脚本只负责
两个**从声明推导**的占位符：

| 占位符 | 内容 |
|---|---|
| `AIOPS_NODE_TOOL_NAMES` | `operator_tools.json` 里全部工具名，`\\|` 连接 |
| `AIOPS_NODE_TOOL_PATHS` | 同上，路径模板去掉请求参数、按 `\\|` 连接 |

**为什么必须推导**：这两处白名单曾经手写，2026-11-02 站点工具就是这样漏掉的 ——
声明里加了 `cpms_site_search`，模板没跟，节点调用被 `location / { return 404; }`
吃掉，运行期只看到 `cpms_unavailable`（不是 403：请求根本没到网关）。

    python tools/content/render_node_callback.py \\
        --bridge 172.30.41.1 --state /var/lib/aiops-node-runtime \\
        --config /etc/aiops-node-runtime \\
        --gateway http://172.18.0.1:8788 --out /tmp/gateway-callback.conf

用途是**校验与生成**：宿主上的 `/etc/aiops-node-runtime/gateway-callback.conf`
由它渲染，改声明后重跑一次即可，不要手改那份配置。

**不能无参数 envsubst**：模板里有 nginx 自身的 `$http_authorization` 等变量，
无参数替换会一起抹掉（README 早有这条警告）。本脚本只替换 `${AIOPS_*}`。
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy/node-runtime/gateway-callback.conf.template"
LOCATIONS = TEMPLATE.with_name("gateway-callback-locations.conf.template")
PLACEHOLDER = re.compile(r"\$\{(AIOPS_[A-Z0-9_]+)\}")


def tool_names() -> str:
    from aiops_diagnostics.operator import operator_tools

    return "|".join(tool.name for tool in operator_tools.tools())


def tool_paths() -> str:
    """取数路径的**完整模板**，每条都逐字正则转义。

    `/chOrderInfo/getOrderInfoDetail/{order_no}` → `chOrderInfo/getOrderInfoDetail/[A-Za-z0-9_-]{1,80}`
    `/chSite/page`                              → `chSite/page`

    **不统一加可选尾段**：那样会放行 `chSite/page/ORDER-1`（站点分页没有尾段）和
    `chOrderInfo/getOrderInfoDetail`（订单路径必须有单号），而这两条都不是声明里写过的
    形状。白名单在这里是「每条 path_template 各自成立」，不是「像某个前缀就行」。
    """
    from aiops_diagnostics.operator import operator_tools

    paths = []
    for tool in operator_tools.tools():
        path = re.escape(tool.path_template.lstrip("/"))
        paths.append(re.sub(r"\\\{[a-z_]{1,32}\\\}", "[A-Za-z0-9_-]{1,80}", path))
    return "|".join(paths)


def render(bridge: str, state: str, config: str, gateway: str, lua_directives: str = "") -> str:
    values = {
        "AIOPS_NODE_BRIDGE": bridge,
        "AIOPS_NODE_STATE": state,
        "AIOPS_NODE_CONFIG": config,
        "AIOPS_PRODUCTION_GATEWAY": gateway,
        "AIOPS_NODE_TOOL_NAMES": tool_names(),
        "AIOPS_NODE_TOOL_PATHS": tool_paths(),
        # 空串是**正确值**：普通 nginx 不支持 Lua 指令，模板里那两项留空即可
        # （整行变成一个空行）。带 lua 模块的宿主把自己的两行参数传进来。
        "AIOPS_NODE_LUA_DIRECTIVES": lua_directives,
    }
    text = TEMPLATE.read_text(encoding="utf-8").replace(
        "${AIOPS_NODE_LOCATIONS}", LOCATIONS.read_text(encoding="utf-8")
    )
    missing = {m.group(1) for m in PLACEHOLDER.finditer(text)} - set(values)
    if missing:
        raise SystemExit(f"模板里有未提供的占位符：{sorted(missing)}")
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


def render_ingress(gateway: str, sources: list[str]) -> str:
    """生成既有TLS虚拟主机的server扩展；部署授权仍由运维单独核对。"""
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", gateway):
        raise ValueError("接入上游必须为本机固定IPv4回环端口")
    if not 1 <= int(gateway.rsplit(":", 1)[1]) <= 65535:
        raise ValueError("上游端口无效")
    if not 1 <= len(sources) <= 8 or len(set(sources)) != len(sources):
        raise ValueError("需要1至8个不重复的来源IP")
    for source in sources:
        if str(ipaddress.IPv4Address(source)) != source:
            raise ValueError("来源必须为规范IPv4地址，不接受网段或转发头")
    allowed = "|".join(source.replace(".", "[.]") for source in sources)
    values = {
        "AIOPS_PRODUCTION_GATEWAY": gateway,
        "AIOPS_NODE_TOOL_NAMES": tool_names(),
        "AIOPS_NODE_TOOL_PATHS": tool_paths(),
    }
    node = PLACEHOLDER.sub(lambda m: values[m.group(1)], LOCATIONS.read_text())
    consumer = (
        (ROOT / "ops/nginx/dify-consumer-callback.conf")
        .read_text()
        .replace("http://172.18.0.1:8788", gateway)
        .replace(
            "proxy_set_header Authorization $http_authorization;",
            "proxy_set_header Authorization $http_authorization;\n"
            "    proxy_set_header Content-Type $http_content_type;",
        )
    )
    guard = (
        '    if ($https != "on") { return 403; }\n'
        f'    if ($realip_remote_addr !~ "^({allowed})$") {{ return 403; }}\n'
    )
    # 子location有自己的rewrite指令时不继承父层if，故每个允许路径独立校验。
    routes = re.sub(r"(^\s*location [^\n]+\{\n)", lambda m: m[1] + guard, node + consumer, flags=re.M)
    return (
        "# AI-Ops工具入口；只安装于已批准的TLS虚拟主机，不修改公司应用。\n"
        "location ^~ /v1/internal/ {\n"
        "    access_log off;\n    error_log /dev/null;\n"
        "    client_max_body_size 256k;\n    proxy_pass_request_headers off;\n"
        "    proxy_next_upstream off;\n    proxy_cache off;\n"
        "    proxy_buffering off;\n    proxy_connect_timeout 5s;\n"
        "    proxy_send_timeout 60s;\n    proxy_read_timeout 60s;\n"
        + routes
        + "\n    location ~ ^/v1/internal/ { return 404; }\n}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", help="节点内部网桥地址，例如 172.30.41.1")
    parser.add_argument("--state", help="状态目录，例如 /var/lib/aiops-node-runtime")
    parser.add_argument("--config", help="配置目录，例如 /etc/aiops-node-runtime")
    parser.add_argument("--gateway", required=True, help="生产网关回调地址")
    parser.add_argument("--ingress", action="store_true", help="生成既有HTTPS虚拟主机的扩展片段")
    parser.add_argument("--allow-source", action="append", default=[], help="获准的实际连接来源IPv4")
    parser.add_argument(
        "--lua-directives",
        default="",
        help="带 lua 模块的 nginx 构建才传（例如 41 的两行 lua_package_*）；普通 nginx 留空",
    )
    parser.add_argument("--out", type=Path, help="写到哪里；省略则打印到 stdout")
    args = parser.parse_args()
    if args.ingress:
        if any((args.bridge, args.state, args.config, args.lua_directives)):
            parser.error("扩展片段不接受独立server配置")
        text = render_ingress(args.gateway, args.allow_source)
    else:
        if not all((args.bridge, args.state, args.config)) or args.allow_source:
            parser.error("独立server需要bridge/state/config且不接受allow-source")
        text = render(args.bridge, args.state, args.config, args.gateway, args.lua_directives)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
