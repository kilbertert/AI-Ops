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
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy/node-runtime/gateway-callback.conf.template"
PLACEHOLDER = re.compile(r"\$\{(AIOPS_[A-Z0-9_]+)\}")


def tool_names() -> str:
    from aiops_diagnostics.operator import operator_tools

    return "|".join(tool.name for tool in operator_tools.tools())


def tool_paths() -> str:
    """取数路径（不含请求参数、不含占位符）。

    `/chOrderInfo/getOrderInfoDetail/{order_no}` → `chOrderInfo/getOrderInfoDetail`
    `/chSite/page`                              → `chSite/page`
    渲染时整段做正则转义，避免路径里的 `.` 之类被当成通配。
    """
    from aiops_diagnostics.operator import operator_tools

    paths = []
    for tool in operator_tools.tools():
        path = tool.path_template.lstrip("/")
        path = re.sub(r"/\{[a-z_]{1,32}\}", "", path)
        paths.append(re.escape(path))
    return "|".join(paths)


def render(bridge: str, state: str, config: str, gateway: str) -> str:
    values = {
        "AIOPS_NODE_BRIDGE": bridge,
        "AIOPS_NODE_STATE": state,
        "AIOPS_NODE_CONFIG": config,
        "AIOPS_PRODUCTION_GATEWAY": gateway,
        "AIOPS_NODE_TOOL_NAMES": tool_names(),
        "AIOPS_NODE_TOOL_PATHS": tool_paths(),
    }
    text = TEMPLATE.read_text(encoding="utf-8")
    missing = {m.group(1) for m in PLACEHOLDER.finditer(text)} - set(values)
    if missing:
        raise SystemExit(f"模板里有未提供的占位符：{sorted(missing)}")
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", required=True, help="节点内部网桥地址，例如 172.30.41.1")
    parser.add_argument("--state", required=True, help="状态目录，例如 /var/lib/aiops-node-runtime")
    parser.add_argument("--config", required=True, help="配置目录，例如 /etc/aiops-node-runtime")
    parser.add_argument("--gateway", required=True, help="生产网关回调地址")
    parser.add_argument("--out", type=Path, help="写到哪里；省略则打印到 stdout")
    args = parser.parse_args()
    text = render(args.bridge, args.state, args.config, args.gateway)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
