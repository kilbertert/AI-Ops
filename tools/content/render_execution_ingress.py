#!/usr/bin/env python3
"""生成既有HTTPS虚拟主机的共享执行入口；不发布平台管理面。"""

import argparse
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit

DISPATCH = "/snail-ai/openapi/v1/agent/chat/sync"


def render(upstream: str, server_name: str, ca_file: str, sources: list[str]) -> str:
    parsed = urlsplit(upstream)
    address = ipaddress.IPv4Address(parsed.hostname or "")
    if (
        not any(
            address in ipaddress.IPv4Network(network)
            for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8")
        )
        or not parsed.port
        or upstream != f"https://{address}:{parsed.port}"
    ):
        raise ValueError("上游须为已登记平台的固定RFC1918或回环IPv4 HTTPS端点")
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?", server_name):
        raise ValueError("需要平台证书的固定DNS名称")
    if not re.fullmatch(r"/[a-zA-Z0-9_./-]+", ca_file) or ".." in Path(ca_file).parts:
        raise ValueError("CA须使用绝对文件路径")
    if not 1 <= len(sources) <= 8 or len(set(sources)) != len(sources):
        raise ValueError("需要1至8个不重复的实际来源IP")
    for source in sources:
        if str(ipaddress.IPv4Address(source)) != source:
            raise ValueError("来源须为规范IPv4，不接受网段")
    allowed = "|".join(source.replace(".", "[.]") for source in sources)
    return f'''# AI-Ops共享执行入口；在获准的既有TLS虚拟主机内安装。
# Nginx规范化路径后才选择location；带应用凭据的穿越必须在server层拒绝。
set $aiops_dispatch_invalid 0;
if ($http_snail_ai_app_id != "") {{ set $aiops_dispatch_invalid 1; }}
if ($http_snail_ai_token != "") {{ set $aiops_dispatch_invalid 1; }}
if ($request_uri ~ "^/snail-ai/") {{ set $aiops_dispatch_invalid 1; }}
if ($request_uri = "{DISPATCH}") {{ set $aiops_dispatch_invalid 0; }}
if ($aiops_dispatch_invalid = 1) {{ return 404; }}
location ^~ /snail-ai/ {{
    access_log off;
    error_log /dev/null;
    location = {DISPATCH} {{
        if ($https != "on") {{ return 403; }}
        if ($realip_remote_addr !~ "^({allowed})$") {{ return 403; }}
        if ($request_method != POST) {{ return 405; }}
        if ($request_uri != "{DISPATCH}") {{ return 404; }}
        if ($http_snail_ai_app_id = "") {{ return 401; }}
        if ($http_snail_ai_token = "") {{ return 401; }}
        client_max_body_size 64k;
        proxy_pass {upstream};
        proxy_http_version 1.1;
        proxy_pass_request_headers off;
        proxy_set_header Host {server_name};
        proxy_set_header Content-Type application/json;
        proxy_set_header Snail-Ai-App-Id $http_snail_ai_app_id;
        proxy_set_header Snail-Ai-Token $http_snail_ai_token;
        proxy_set_header Connection "";
        proxy_ssl_server_name on;
        proxy_ssl_name {server_name};
        proxy_ssl_trusted_certificate {ca_file};
        proxy_ssl_verify on;
        proxy_ssl_verify_depth 3;
        proxy_connect_timeout 5s;
        proxy_read_timeout 310s;
        proxy_send_timeout 15s;
        proxy_next_upstream off;
        proxy_cache off;
        proxy_buffering off;
        add_header Cache-Control "no-store" always;
    }}
    location ~ ^/snail-ai/ {{ return 404; }}
}}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--allow-source", action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.write_text(render(args.upstream, args.server_name, args.ca_file, args.allow_source))


if __name__ == "__main__":
    main()
