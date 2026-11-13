#!/usr/bin/env python3
"""由受控环境清单生成nginx入口；不探测服务器，不解析用户JWT。"""

import argparse
import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from aiops_diagnostics.common.private_files import validate_private_file, write_private_text
from aiops_diagnostics.gateway.deployment_environment import canonical_environment
from aiops_diagnostics.gateway.gateway_config import canonical_gateway_url

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "ops/nginx/business-environment-ingress.conf.template"
KEY = re.compile(r"[a-f0-9]{64}")
SOURCE_KEY = re.compile(r"[A-Za-z0-9_-]{16,256}")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")


def render(catalogue, listen, *, trusted_ca=""):
    if (
        not re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", listen)
        or not 1 <= int(listen.rsplit(":", 1)[1]) <= 65535
    ):
        raise ValueError("环境入口须使用已分配的回环端口")
    if not isinstance(catalogue, dict) or set(catalogue) != {"schema", "environments"}:
        raise ValueError("环境接入清单形状错误")
    rows = catalogue["environments"]
    if (
        catalogue["schema"] != "aiops-business-ingress/1"
        or not isinstance(rows, list)
        or not 1 <= len(rows) <= 64
    ):
        raise ValueError("环境接入清单版本或数量错误")
    output = {
        name: []
        for name in (
            "ENVIRONMENT_KEYS",
            "ENVIRONMENT_UPSTREAMS",
            "ENVIRONMENT_SOURCE_KEYS",
            "ENVIRONMENT_HOSTS",
            "UPSTREAMS",
        )
    }
    identifiers, ingress_keys, endpoints = set(), set(), set()
    needs_tls = False
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {
            "id",
            "gateway_url",
            "ingress_key",
            "company_source_key",
        }:
            raise ValueError("环境接入条目形状错误")
        if not all(isinstance(v, str) for v in row.values()):
            raise ValueError("环境接入值必须为字符串")
        identifier = canonical_environment(row["id"])
        if identifier != row["id"] or identifier in identifiers:
            raise ValueError("环境标识重复或不规范")
        keys = (row["ingress_key"], row["company_source_key"])
        if KEY.fullmatch(keys[0]) is None or SOURCE_KEY.fullmatch(keys[1]) is None or keys[0] in ingress_keys:
            raise ValueError("入口凭据须为独立64位hex，上游来源密钥须为16至256位安全字符")
        url = canonical_gateway_url(row["gateway_url"])
        parsed = urlsplit(url)
        if (
            parsed.path not in ("", "/")
            or not HOST.fullmatch(parsed.hostname)
            or any(c in url for c in "\r\n\"'$;{}\\")
        ):
            raise ValueError("Gateway地址须为不带路径的受控地址")
        if parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = False
            if not loopback:
                raise ValueError("HTTP上游仅允许回环IP，远端须HTTPS")
        if url in endpoints:
            raise ValueError("两个业务环境不能共用同一个Gateway入口")
        endpoints.add(url)
        if parsed.netloc == listen:
            raise ValueError("上游不能指向接入面自身")
        identifiers.add(identifier)
        ingress_keys.add(keys[0])
        name = f"aiops_business_{index}"
        needs_tls |= parsed.scheme == "https"
        # nginx map字面量匹配不区分大小写；凭据必须用区分大小写的锚定正则。
        output["ENVIRONMENT_KEYS"].append(f'    ~^{keys[0]}$ "{identifier}";')
        output["ENVIRONMENT_UPSTREAMS"].append(f'    "{identifier}" "{parsed.scheme}://{name}";')
        output["ENVIRONMENT_SOURCE_KEYS"].append(f'    "{identifier}" "{keys[1]}";')
        output["ENVIRONMENT_HOSTS"].append(f'    "{identifier}" "{parsed.hostname}";')
        output["UPSTREAMS"].append(f"upstream {name} {{ server {parsed.netloc}; }}")
    tls = ""
    if needs_tls:
        if not re.fullmatch(r"/[A-Za-z0-9_./-]{1,240}", trusted_ca):
            raise ValueError("HTTPS上游必须声明可信CA文件")
        tls = (
            f"        proxy_ssl_trusted_certificate {trusted_ca};\n"
            "        proxy_ssl_verify on;\n        proxy_ssl_verify_depth 3;\n"
            "        proxy_ssl_server_name on;\n        proxy_ssl_name $aiops_environment_host;"
        )
    text = TEMPLATE.read_text()
    for key, lines in {**output, "LISTEN": listen, "TLS": tls}.items():
        text = text.replace("${" + key + "}", "\n".join(lines) if isinstance(lines, list) else lines)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue", required=True, type=Path)
    parser.add_argument("--listen", required=True)
    parser.add_argument("--trusted-ca", default="")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    validate_private_file(args.catalogue)
    if args.catalogue.stat().st_size > 65536:
        raise ValueError("环境接入清单过大")
    text = render(json.loads(args.catalogue.read_text()), args.listen, trusted_ca=args.trusted_ca)
    write_private_text(args.out, text)
    print("环境接入配置已生成（私有文件）；安装前须以服务身份校验")


if __name__ == "__main__":
    main()
