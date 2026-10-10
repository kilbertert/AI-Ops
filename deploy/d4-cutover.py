#!/usr/bin/env python3
"""D-4 改向：把物业端（operator）那一支改到公司网关（在 41 上以 root 运行）。

用法：
    python3 d4-cutover.py dry-run     # 只打印将要写的内容
    python3 d4-cutover.py apply       # 备份 -> 写 vhost 与 map -> nginx -t -> reload
    python3 d4-cutover.py rollback    # 从最近一次备份恢复（两处），再 reload

改向的形状与理由（都写在生成的配置注释里，见 render_*）：
  · operator  → http://127.0.0.1:30899（宿主端口 → cloud-gateway 容器）
  · consumer  → http://172.18.0.1:8788（直连 AI-Ops，逐字不变）
  · nginx **不再**为 operator 注入来源密钥 —— 由网关那一跳注入（这正是 D-4 的目的）
  · 变量式 proxy_pass 不会自动拼 URI ⇒ location 里必须配 `rewrite … break`
"""

import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

VHOST = Path("/www/server/panel/vhost/rewrite/api.mall.qushiyun.com.conf")
MAPFILE = Path("/www/server/panel/vhost/nginx/0.aiops-entry-map.conf")
TOKENCONF = Path("/etc/aiops-41/nginx-aiops-service-token.conf")

MAP_TEXT = r"""# 2026-09-29：管家端入口分流（#448 / ADR-0009）。
# 本文件必须在 **http 上下文** —— 它被 /www/server/nginx/conf/nginx.conf:101 的
# include /www/server/panel/vhost/nginx/*.conf 载入（vhost 与它同级，都在 http 里）。
# 非法非空入口**原样透传**，由上游 403，不静默落进 consumer。
map $http_x_business_entry $aiops_entry {
    default              "consumer";
    "~*^\s*operator\s*$" "operator";
    "~^.+$"              $http_x_business_entry;
}
# 2026-09-30（D-4）：operator 改向公司网关（cloud-gateway）。
# 为什么用变量 + map：nginx 的 `proxy_pass` 只能有一个上游，按入口分流需要两个目标。
# 变量式 proxy_pass **不会自动带上 URI**，因此 location 里必须配 `rewrite ... break`。
# 不写它，请求会打到网关的 `/`（实测：AI-Ops 收到 `/faq/recommendations`）。
map $aiops_entry $aiops_upstream {
    default    "http://172.18.0.1:8788";    # consumer 及一切非 operator：直连 AI-Ops，逐字不变
    "operator" "http://127.0.0.1:30899";    # operator：先经公司网关（宿主端口 → cloud-gateway）
}
# consumer 分支仍用 AI-Ops 自己的服务令牌（行为与改向前逐字一致）。
map $aiops_entry $aiops_auth {
    default    $aiops_service_authorization;
    "operator" $http_authorization;
}
map $aiops_entry $aiops_srckey {
    default    "";
    # 🔴 operator **不再在这里注入来源密钥** —— 改由网关那一跳注入（D-4 的目的）。
    # 留着它会让同一请求上有两个来源，而 AI-Ops 取第一个（谁先到取决于注入顺序）。
    "operator" "";
}
# 会话头的**两种拼写**都要认（#649）。
#
# nginx 的 `$http_<name>` 按**小写 + 下划线**取头：`$http_third_session` 只匹配
# `third-session`（大小写不敏感），对 `X-Third-Session` 取到**空值** ——
# 于是头被丢掉、网关没有会话、回 401 INVALID_ACCESS_TOKEN，**与「会话过期」同形**，
# 而日志里查不到头名，排查会朝凭据方向走（2026-10-10 实测踩过一次）。
#
# 为什么入口要两种都认、而不是只在文档里写对一种：**两种拼写都已经被写进仓库** ——
# 客户端文档（`docs/gateway.md`、`docs/standard-api-contract.md`、`docs/faq-api.md`）
# 写 `third-session`，而本文件与网关的参数名、以及 `docs/agents/frontend-api-brief.md`
# 那段 nginx 示例写 `X-Third-Session`。照后者接的对接方拿到的就是那个同形的 401。
#
# 一个变量、两种输入：前者为空时取后者，两者都为空时仍是空（不伪造会话）。
map "$http_third_session:$http_x_third_session" $aiops_third_session {
    default    $http_third_session;
    "~^:(.+)$" $1;
}
"""

#: `/v1/` location 上方那段说明。单独拎出来是因为 `replace_location` 要认它，
#: 而注释里出现了两处（替换范围的开头、以及生成的正文本身）——写一次就不会漂。
V1_UMBRELLA = """# AI-Ops 入口：按内容域分流（#448 / ADR-0009；D-4 于 2026-09-30 改向）。
#   consumer  → 直连 AI-Ops（172.18.0.1:8788）：服务令牌 + 会话，行为逐字不变
#   operator  → 先经公司网关（127.0.0.1:30899 → cloud-gateway），由它注入来源密钥
# 会话头两种拼写都归一到一个变量（#649，map 见 0.aiops-entry-map.conf）。
# 变量式 proxy_pass 不会自动拼 URI，`rewrite … break` 是必需的。
"""

LOCATION_TEXT = (
    V1_UMBRELLA
    + r"""location ^~ /v1/ {
    include /etc/aiops-41/nginx-aiops-service-token.conf;   # 只 set 变量，不直接设头
    proxy_pass $aiops_upstream;
    rewrite ^/v1/(.*)$ /v1/$1 break;
    proxy_http_version 1.1;
    proxy_set_header Authorization      $aiops_auth;
    proxy_set_header X-AIOps-Source-Key $aiops_srckey;
    proxy_set_header X-Business-Entry   $aiops_entry;
    # 两种拼写归一到一个变量（见 0.aiops-entry-map.conf 里的 map，#649）。
    proxy_set_header X-Third-Session    $aiops_third_session;
    proxy_set_header Range              $http_range;
    proxy_set_header Host               $host;
    proxy_connect_timeout 15s;
    proxy_send_timeout  120s;
    proxy_read_timeout  120s;
    proxy_buffering off;
}
"""
)


# Dify 的 External Knowledge API 那一跳（#614）。
#
# 为什么必须单独一条 location：上面那条 `location ^~ /v1/` 用 AI-Ops 自己的服务令牌
# **覆盖** Authorization（`proxy_set_header Authorization $aiops_auth;`），而 Dify 的
# External Knowledge API **只支持 Bearer 自带 key** —— 它的 key 被覆盖掉，适配路由收到
# 的不是它要的那把，回 403 DIFY_CREDENTIAL_REJECTED（2026-10-09 公网实测）。
#
# nginx 的 location 选择规则：**前缀最长者优先**，与书写顺序无关；正则 location 只在
# 没有更长的前缀 location 命中时才参与。`/v1/dify/`（9 字符）比 `/v1/`（4 字符）长，
# 所以这条会赢 —— 它不依赖出现在文件里的位置。
#
# 这一条**刻意做得比 `/v1/` 那条窄**，因为 Dify 不是我们的用户：它没有会话、没有 UPMS
# 主体，拿的是 Dify 控制台里配置的共享凭据。所以：
#   · 不 include 服务令牌 conf（那是调用者链的凭据）
#   · 不注入 X-AIOps-Source-Key / X-Business-Entry / X-Third-Session（它一个都不该有）
#   · **保留客户端带来的 Authorization**（就是 Dify 的 API Key）
#   · 上游固定直连 AI-Ops —— 与入口无关，Dify 没有"入口"可言
#
# 端点仍然**只读**、租户与知识库集合仍由我们的 `AIOPS_GATEWAY_DIFY_KNOWLEDGE_BINDINGS`
# 决定，集合之外一律不可达（见 ops/dify-exposure-registry.md）。
DIFY_LOCATION_TEXT = r"""# Dify External Knowledge API 那一跳（#614）：保留 Dify 自带的 Authorization。
# 前缀比 `/v1/` 长 ⇒ 按 nginx 的"最长前缀优先"规则命中这里，不依赖书写顺序。
# 不 include 服务令牌 conf、不注入来源密钥/入口/会话 —— Dify 不是我们的用户。
location ^~ /v1/dify/ {
    proxy_pass http://172.18.0.1:8788;
    rewrite ^/v1/dify/(.*)$ /v1/dify/$1 break;
    proxy_http_version 1.1;
    proxy_set_header Authorization      $http_authorization;
    proxy_set_header Host               $host;
    proxy_set_header X-Real-IP          $remote_addr;
    proxy_set_header X-Forwarded-For    $proxy_add_x_forwarded_for;
    proxy_connect_timeout 15s;
    proxy_send_timeout  120s;
    proxy_read_timeout  120s;
    proxy_buffering off;
}
"""


def _block_end(src: str, start: int) -> int:
    """End offset of the `location` block whose header starts at ``start``.

    Bracket-aware rather than "up to the next `\\nlocation `": the earlier
    revision of this function walked to the next location, so a vhost that had
    the Dify block *already present* on disk (i.e. after a first `apply`) got
    that block swallowed and re-emitted — and, worse, anything between the two
    blocks went with it. Counting braces cannot do that: it stops at the block's
    own closing brace wherever the neighbours are.
    """
    depth = 0
    index = src.index("{", start)
    while index < len(src):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise ValueError("未闭合的 location 块")


def replace_location(src: str) -> str:
    """Write the `/v1/` location, and Dify's narrower one in front of it.

    Idempotent by construction: what an `apply` leaves behind is exactly
    ``DIFY_LOCATION_TEXT + LOCATION_TEXT`` in that spot, so if that concatenation
    is already there the function returns the input unchanged. Anything else —
    the original file, or the state a first `apply` left behind — is matched by
    the umbrella comment and the two `location` lines.

    The umbrella comment is part of the extent because the original file has two
    comment lines above `location ^~ /v1/` that this file's text supersedes;
    locating by `location` alone would leave the stale disclaimer behind. The
    Dify block sits *before* that comment so it is not swallowed by it.

    **Dify's own location, if it is already there, is kept**: its extent is
    measured and re-emitted verbatim rather than being absorbed. That matters
    because this script's output is *reconciled*, not appended — the first
    version moved the block (visually harmless, but a diff that does not match
    what the operator wrote is a diff nobody can review).
    """
    written = DIFY_LOCATION_TEXT + LOCATION_TEXT
    if written in src:
        return src

    # The umbrella's FIRST line, from the one place it is written: a prefix of
    # `V1_UMBRELLA` so the two cannot drift apart when the comment is reworded.
    umbrella = V1_UMBRELLA.splitlines()[0]
    start = src.index(umbrella) if umbrella in src else src.index("location ^~ /v1/ {")

    dify_marker = "location ^~ /v1/dify/ {"
    kept_dify = ""
    if start > 0 and dify_marker in src[:start]:
        # A first `apply` already put it above the umbrella. Keep that block,
        # byte for byte, and only rewrite what is below it.
        dify_start = src.rindex(dify_marker, 0, start)
        kept_dify = src[dify_start : _block_end(src, dify_start)] + "\n"
        start = dify_start

    after = src.index("location ^~ /v1/ {", start)
    end = _block_end(src, after)
    return src[:start] + kept_dify + written + src[end:]


def run(*cmd: str) -> None:
    subprocess.run(cmd, check=True)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"dry-run", "apply", "rollback"}:
        print(__doc__)
        return 2
    action = sys.argv[1]

    if action == "dry-run":
        print(f"=== 将写入 {MAPFILE} ===")
        print(MAP_TEXT)
        print(f"=== 将写入 {VHOST} 的 location ^~ /v1/dify/（#614，比 /v1/ 更靠前） ===")
        print(DIFY_LOCATION_TEXT)
        print(f"=== 将写入 {VHOST} 的 location ^~ /v1/ ===")
        print(LOCATION_TEXT)
        print("（dry-run 不写任何文件）")
        return 0

    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    if sys.platform != "linux" or uid != "0":
        print("必须 root 运行", file=sys.stderr)
        return 1

    if action == "apply":
        backup = Path(f"/var/backups/aiops-41/d4-cutover-{datetime.now():%Y%m%d-%H%M%S}")
        backup.mkdir(parents=True)
        for f in (VHOST, MAPFILE, TOKENCONF):
            shutil.copy2(f, backup / f.name)
        print(f"备份 -> {backup}")
        MAPFILE.write_text(MAP_TEXT)
        VHOST.write_text(replace_location(VHOST.read_text()))
        try:
            run("nginx", "-t")
        except subprocess.CalledProcessError:
            print("nginx -t 失败 —— 正在回滚", file=sys.stderr)
            shutil.copy2(backup / VHOST.name, VHOST)
            shutil.copy2(backup / MAPFILE.name, MAPFILE)
            return 1
        run("nginx", "-s", "reload")
        print(f"已 reload。回滚： {sys.argv[0]} rollback")
        return 0

    backups = sorted(Path("/var/backups/aiops-41").glob("d4-cutover-*"))
    if not backups:
        print("找不到 d4-cutover 备份", file=sys.stderr)
        return 1
    latest = backups[-1]
    shutil.copy2(latest / VHOST.name, VHOST)
    shutil.copy2(latest / MAPFILE.name, MAPFILE)
    run("nginx", "-t")
    run("nginx", "-s", "reload")
    print(f"已从 {latest} 回滚（vhost + map 两处都已恢复）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
