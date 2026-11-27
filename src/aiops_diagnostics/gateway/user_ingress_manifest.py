"""以 `public_openapi.OPERATIONS` 为单一真源生成并校验客户环境用户 Nginx 入口（ADR-0362）。

约束：
- 只收录面向客户端/管家端的公开接口（`public_openapi.OPERATIONS`）及显式登记的兼容会话创建入口；
- 任何未声明正则形状的路径占位符直接 fail-closed 报错，杜绝生成宽松前缀转发；
- 与 `tests/operations/test_environment_user_ingress.py` 共用同一判定，保证 CI 双向强对齐。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

from aiops_diagnostics.gateway.public_openapi import OPERATIONS
from aiops_diagnostics.identity.order_identity import ORDER_ID_ATOM

#: 已放行但未列入 OpenAPI 测试指南的既有兼容用户入口（ADR-0319 原样保留）。
COMPAT_USER_INGRESS_OPERATIONS: tuple[tuple[str, str], ...] = (("post", "/v1/conversations"),)

#: 每个路径占位符对应的精确 URI 片段正则（禁止宽松匹配 `/`）。
PLACEHOLDER_PATTERNS: dict[str, str] = {
    "qa_id": r"qa_[0-9a-f]{32}",
    "file_id": r"file_[0-9a-f]{32}",
    "conversation_id": r"conv_[0-9a-f]{32}",
    "job_id": r"hrj_[0-9a-f]{32}",
    "analysis_id": r"an_[0-9a-f]{32}",
    "signed_id": r"media_[A-Za-z0-9_-]{32}\.[0-9a-f]{24}",
    "order_no": ORDER_ID_ATOM,
}

#: 需要独立超时/流式配置的后缀路由（目前仅 SSE 事件流）。
_SSE_STREAM_PATHS = frozenset({"/v1/assistant/questions/{qa_id}/events"})

#: 需要大于默认 64k 请求体的静态路由（附件上传上限 20 MiB + multipart 开销）。
_STATIC_BODY_LIMITS: dict[str, str] = {
    "/v1/assistant/files": "25m",
}

_PARAM_PREFIX_SLUG: dict[str, str] = {
    "/v1/assistant/questions/": "qa",
    "/v1/assistant/files/": "file",
    "/v1/conversations/": "conv",
    "/v1/media/": "media",
    "/v1/orders/": "order",
    "/v1/health-report-jobs/": "hrj",
    "/v1/standard/analyses/": "analysis",
}

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_NGINX_PROXY_INC = "/etc/aiops-access-__ENV__/environment-user-ingress-proxy.inc"


def expected_user_ingress_routes() -> list[tuple[str, str]]:
    """返回客户环境用户入口应当放行的 `(METHOD, path_template)` 有序清单。"""
    combined: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for method, path in [*OPERATIONS.keys(), *COMPAT_USER_INGRESS_OPERATIONS]:
        key = (method.upper(), path)
        if not path.startswith("/v1/"):
            raise ValueError(f"用户入口仅允许 /v1/ 路径：{method.upper()} {path}")
        for match in _PLACEHOLDER_RE.finditer(path):
            name = match.group(1)
            if name not in PLACEHOLDER_PATTERNS:
                raise ValueError(f"未在 PLACEHOLDER_PATTERNS 声明形状正则的占位符: {{{name}}} ({path})")
        if key not in seen:
            seen.add(key)
            combined.append(key)
    return combined


def _escape_pcre_literal(literal: str) -> str:
    """仅转义 PCRE 元字符，保留 `/` 与 `-` 可读性，避免 `nginx` 报冗余转义。"""
    return re.sub(r"([\\.^$*+?()\[\]{}|])", r"\\\1", literal)


def _template_to_uri_regex(template: str) -> str:
    """将 FastAPI 路径模板转成不含锚点的 PCRE URI 正则。"""
    parts: list[str] = []
    pos = 0
    for match in _PLACEHOLDER_RE.finditer(template):
        literal = template[pos : match.start()]
        if literal:
            parts.append(_escape_pcre_literal(literal))
        parts.append(PLACEHOLDER_PATTERNS[match.group(1)])
        pos = match.end()
    tail = template[pos:]
    if tail:
        parts.append(_escape_pcre_literal(tail))
    return "".join(parts)


def _first_param_prefix(template: str) -> str:
    idx = template.index("{")
    slash = template.rfind("/", 0, idx)
    if slash < 0:
        raise ValueError(f"参数路径缺少前缀斜杠：{template}")
    return template[: slash + 1]


def _method_guard_line(methods: list[str]) -> str:
    unique = sorted(set(methods))
    if len(unique) == 1:
        return f"    if ($request_method != {unique[0]}) {{ return 405; }}"
    joined = "|".join(unique)
    return f'    if ($request_method !~ "^(?:{joined})$") {{ return 405; }}'


def render_routes_txt() -> str:
    """生成 `ops/nginx/environment-user-ingress-routes.txt` 内容。"""
    routes = expected_user_ingress_routes()
    lines = [
        "# 环境用户助手入口实际转发的网关路径 —— 由公开 OpenAPI 契约单一真源生成（ADR-0362）。",
        "#",
        "# 真源：`src/aiops_diagnostics/gateway/public_openapi.py`（OPERATIONS）+ 显式兼容路由。",
        "# 请勿手工增删本文件；改动公开接口后运行：",
        "#   uv run python -m aiops_diagnostics.gateway.user_ingress_manifest --write",
        "# `tests/operations/test_environment_user_ingress.py` 断言本清单 == 公开契约 == 实际 nginx 转发面。",
        "# 管理面（/v1/dify/*、/v1/agents、POST /v1/shortcuts）、/v1/internal/* 严禁进入本清单。",
        "",
    ]
    for method, path in routes:
        lines.append(f"{method:<6} {path}")
    return "\n".join(lines) + "\n"


def _render_static_location(path: str, methods: list[str]) -> list[str]:
    body_limit = _STATIC_BODY_LIMITS.get(path, "64k")
    lines = [f"location = {path} {{"]
    lines.append("    if ($aiops_user_entry_bad = 1) { return 403; }")
    lines.append(_method_guard_line(methods))
    lines.append(f"    client_max_body_size {body_limit};")
    lines.append("    proxy_read_timeout 120s;")
    lines.append(f"    include {_NGINX_PROXY_INC};")
    lines.append("}")
    return lines


def _render_media_location(entries: list[tuple[str, str]]) -> list[str]:
    if entries != [("GET", "/v1/media/{signed_id}")]:
        raise ValueError(f"/v1/media/ 只允许签名媒体 GET 路由，收到：{entries}")
    uri_regex = _template_to_uri_regex("/v1/media/{signed_id}")
    return [
        "# MediaResourceSigner：签名即授权 —— 不检查 X-Business-Entry，也不透传任何身份头。",
        "location ^~ /v1/media/ {",
        f'    if ($uri !~ "^{uri_regex}$") {{ return 404; }}',
        "    if ($request_method != GET) { return 405; }",
        "    proxy_pass http://127.0.0.1:8788;",
        "    proxy_http_version 1.1;",
        "    client_max_body_size 64k;",
        '    proxy_set_header Authorization            "";',
        '    proxy_set_header X-Business-Entry         "";',
        '    proxy_set_header X-Third-Session          "";',
        '    proxy_set_header third-session            "";',
        '    proxy_set_header X-AIOps-Source-Key       "";',
        '    proxy_set_header X-AIOps-Environment      "";',
        '    proxy_set_header X-AIOps-Environment-Key  "";',
        '    proxy_set_header X-AIOps-Gateway-Assertion "";',
        '    proxy_set_header X-Forwarded-Host         "";',
        '    proxy_set_header X-Forwarded-Proto        "";',
        '    proxy_set_header Cookie                   "";',
        "    proxy_set_header Host                     $host;",
        "    proxy_set_header X-Real-IP                $remote_addr;",
        "    proxy_set_header X-Forwarded-For          $proxy_add_x_forwarded_for;",
        "    proxy_connect_timeout 15s;",
        "    proxy_send_timeout 120s;",
        "    proxy_read_timeout 120s;",
        "    proxy_next_upstream off;",
        "    proxy_buffering off;",
        "    proxy_cache off;",
        '    add_header Cache-Control "no-store" always;',
        "    # 签名 URL 本身就是凭据：URI 不能进 access_log（与 .inc 同款逐 location 关）。",
        "    access_log off;",
        "}",
    ]


def _render_prefix_location(prefix: str, entries: list[tuple[str, str]]) -> list[str]:
    if prefix == "/v1/media/":
        return _render_media_location(entries)

    slug = _PARAM_PREFIX_SLUG.get(prefix)
    if slug is None:
        slug = re.sub(r"[^a-z0-9]+", "_", prefix.strip("/")).strip("_") or "param"

    stream_entries: list[tuple[str, str]] = []
    regular_by_path: dict[str, list[str]] = defaultdict(list)
    for method, path in entries:
        if path in _SSE_STREAM_PATHS:
            stream_entries.append((method, path))
        else:
            regular_by_path[path].append(method)

    lines = [f"location ^~ {prefix} {{"]
    # 为流式 SSE 子端点生成嵌套精确正则 location，获得更长超时与 Last-Event-ID 透传。
    for stream_path in sorted({p for _, p in stream_entries}):
        s_methods = [m for m, p in stream_entries if p == stream_path]
        s_regex = _template_to_uri_regex(stream_path)
        lines.append(f'    location ~ "^{s_regex}$" {{')
        lines.append("        if ($aiops_user_entry_bad = 1) { return 403; }")
        lines.append("    " + _method_guard_line(s_methods))
        lines.append("        client_max_body_size 64k;")
        lines.append("        proxy_read_timeout 300s;")
        lines.append("        chunked_transfer_encoding on;")
        lines.append("        proxy_set_header Last-Event-ID $http_last_event_id;")
        lines.append(f"        include {_NGINX_PROXY_INC};")
        lines.append("    }")

    # 如果占位符字符集包含 `.`（如 order_no），先在前缀入口拒绝 `..` 路径遍历片段。
    if any("{order_no}" in path for path in regular_by_path):
        lines.append('    if ($uri ~ "\\.\\.") { return 404; }')

    regular_paths = list(regular_by_path.keys())
    shape_parts = [_template_to_uri_regex(p) for p in regular_paths]
    if len(shape_parts) == 1:
        lines.append(f'    if ($uri !~ "^{shape_parts[0]}$") {{ return 404; }}')
    else:
        lines.append('    if ($uri !~ "^(?:' + "|".join(shape_parts) + ')$") { return 404; }')

    lines.append("    if ($aiops_user_entry_bad = 1) { return 403; }")

    method_sets = {tuple(sorted(set(ms))) for ms in regular_by_path.values()}
    if len(method_sets) == 1:
        lines.append(_method_guard_line(next(iter(method_sets))))
    else:
        call_alts: list[str] = []
        for path, methods in regular_by_path.items():
            uri_regex = _template_to_uri_regex(path)
            for method in sorted(set(methods)):
                call_alts.append(f"{method} {uri_regex}")
        var_name = f"$aiops_{slug}_call"
        lines.append(f'    set {var_name} "${{request_method}} ${{uri}}";')
        lines.append(f'    if ({var_name} !~ "^(?:' + "|".join(call_alts) + ')$") {')
        lines.append("        return 405;")
        lines.append("    }")

    lines.append("    client_max_body_size 64k;")
    lines.append("    proxy_read_timeout 120s;")
    lines.append(f"    include {_NGINX_PROXY_INC};")
    lines.append("}")
    return lines


def render_nginx_conf() -> str:
    """生成 `ops/nginx/environment-user-ingress.conf` 内容。"""
    routes = expected_user_ingress_routes()
    static_methods: dict[str, list[str]] = defaultdict(list)
    param_entries: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for method, path in routes:
        if _PLACEHOLDER_RE.search(path):
            param_entries[_first_param_prefix(path)].append((method, path))
        else:
            static_methods[path].append(method)

    header = [
        "# 环境既有 HTTPS 域名的**用户助手精确入口**（环境 __ENV__）—— 由单一真源自动生成（ADR-0362）。",
        "#",
        "# 真源：`src/aiops_diagnostics/gateway/public_openapi.py`（OPERATIONS）+ 兼容入口；",
        "# 同步命令：`uv run python -m aiops_diagnostics.gateway.user_ingress_manifest --write`。",
        "# 与 dify-consumer-callback 同一个公司既有 vhost 的 server 块 include 本片，",
        "# 连同 environment-user-ingress-proxy.inc 与 root 现场生成的凭据片",
        "# /etc/aiops-access-__ENV__/nginx-user-ingress-auth.conf（0600，本仓库不存任何值）。",
        "#",
        "# 安全与共存边界（ADR-0319 / ADR-0362）：",
        "# - 仅放行公开用户/管家入口清单（`environment-user-ingress-routes.txt`）；",
        "# - 管理面（/v1/dify/*、/v1/agents、POST /v1/shortcuts 等）与 /v1/internal/* 一律挡在外面；",
        "# - location = / ^~ + $uri 形状闸 + 方法白名单，不抢占客户自有业务网关路由；",
        "# - 全部静态与参数路由分别独立声明 client_max_body_size（附件上传 25m，其余 64k）",
        "#   与 proxy_read_timeout（SSE 事件流 300s，其余 120s）。",
        "",
        "# 凭据片必须最先 include：缺失时 nginx -t 直接失败（fail closed）。",
        "include /etc/aiops-access-__ENV__/nginx-user-ingress-auth.conf;",
        "",
        "# server 上下文分流：缺失 X-Business-Entry 默认走 consumer（服务 Bearer + third-session）；",
        "# operator 透传个人 Bearer 并注入本环境来源密钥；其他非空入口值一律 403。",
        'set $aiops_user_entry "consumer";',
        "set $aiops_user_authorization $aiops_user_service_authorization;",
        "set $aiops_user_third_session $http_third_session;",
        'set $aiops_user_source_key "";',
        'if ($http_x_business_entry ~* "^\\s*operator\\s*$") {',
        '    set $aiops_user_entry "operator";',
        "    set $aiops_user_authorization $http_authorization;",
        '    set $aiops_user_third_session "";',
        "    set $aiops_user_source_key $aiops_user_operator_source_key;",
        "}",
        "set $aiops_user_entry_bad 1;",
        'if ($http_x_business_entry = "") { set $aiops_user_entry_bad 0; }',
        'if ($http_x_business_entry ~* "^\\s*(?:consumer|operator)\\s*$") { set $aiops_user_entry_bad 0; }',
        "",
        "# ── 精确静态路径 ──────────────────────────────────────────────────────",
    ]

    blocks: list[str] = ["\n".join(header)]
    for path, methods in static_methods.items():
        blocks.append("\n".join(_render_static_location(path, methods)))

    blocks.append("# ── 参数化路径（prefix ^~ + $uri 形状闸 + 方法白名单）───────────────")
    for prefix, entries in param_entries.items():
        blocks.append("\n".join(_render_prefix_location(prefix, entries)))

    return "\n\n".join(blocks) + "\n"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成或校验客户环境用户 Nginx 入口配置")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="将生成结果写回 ops/nginx/")
    group.add_argument("--check", action="store_true", help="校验 ops/nginx/ 是否与真源完全一致")
    args = parser.parse_args(argv)

    root = _repo_root()
    routes_file = root / "ops" / "nginx" / "environment-user-ingress-routes.txt"
    conf_file = root / "ops" / "nginx" / "environment-user-ingress.conf"

    expected_routes = render_routes_txt()
    expected_conf = render_nginx_conf()

    if args.write:
        routes_file.write_text(expected_routes, encoding="utf-8")
        conf_file.write_text(expected_conf, encoding="utf-8")
        print(f"已写入 {routes_file.relative_to(root)} 与 {conf_file.relative_to(root)}")
        return 0

    drifted: list[str] = []
    if not routes_file.is_file() or routes_file.read_text(encoding="utf-8") != expected_routes:
        drifted.append(str(routes_file.relative_to(root)))
    if not conf_file.is_file() or conf_file.read_text(encoding="utf-8") != expected_conf:
        drifted.append(str(conf_file.relative_to(root)))
    if drifted:
        print(
            "以下客户入口文件已与 public_openapi.OPERATIONS 漂移，请运行 "
            "`uv run python -m aiops_diagnostics.gateway.user_ingress_manifest --write`：\n"
            + "\n".join(f"  - {item}" for item in drifted),
            file=sys.stderr,
        )
        return 1
    print("客户环境用户入口配置与 public_openapi.OPERATIONS 完全一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
