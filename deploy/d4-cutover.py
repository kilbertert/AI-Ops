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
"""

LOCATION_TEXT = r"""# AI-Ops 入口：按内容域分流（#448 / ADR-0009；D-4 于 2026-09-30 改向）。
#   consumer  → 直连 AI-Ops（172.18.0.1:8788）：服务令牌 + 会话，行为逐字不变
#   operator  → 先经公司网关（127.0.0.1:30899 → cloud-gateway），由它注入来源密钥
# 变量式 proxy_pass 不会自动拼 URI，`rewrite … break` 是必需的。
location ^~ /v1/ {
    include /etc/aiops-41/nginx-aiops-service-token.conf;   # 只 set 变量，不直接设头
    proxy_pass $aiops_upstream;
    rewrite ^/v1/(.*)$ /v1/$1 break;
    proxy_http_version 1.1;
    proxy_set_header Authorization      $aiops_auth;
    proxy_set_header X-AIOps-Source-Key $aiops_srckey;
    proxy_set_header X-Business-Entry   $aiops_entry;
    proxy_set_header X-Third-Session    $http_third_session;
    proxy_set_header Range              $http_range;
    proxy_set_header Host               $host;
    proxy_connect_timeout 15s;
    proxy_send_timeout  120s;
    proxy_read_timeout  120s;
    proxy_buffering off;
}
"""


def replace_location(src: str) -> str:
    start = src.index("location ^~ /v1/ {")
    end = src.index("\nlocation ", start + 1)
    return src[:start] + LOCATION_TEXT + src[end:]


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
