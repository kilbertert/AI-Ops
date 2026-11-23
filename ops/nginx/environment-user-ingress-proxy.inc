# 每个 location 共用的代理设置（被 environment-user-ingress.conf `include`）。
#
# 文件名用 `.inc` 而不是 `.conf`：vhost 的扩展目录按 glob 载入 `*.conf`，
# 这份内容只能待在 location 内，不能被单独载入。
# 身份头一律由 server 上下文算好的变量注入 —— 客户端自报的同名头到不了上游。

proxy_pass http://127.0.0.1:8788;
proxy_http_version 1.1;
client_max_body_size 64k;
proxy_set_header Authorization            $aiops_user_authorization;
proxy_set_header X-Business-Entry         $aiops_user_entry;
proxy_set_header X-Third-Session          $aiops_user_third_session;
proxy_set_header third-session            "";
proxy_set_header X-AIOps-Source-Key       $aiops_user_source_key;
proxy_set_header X-AIOps-Environment      "";
proxy_set_header X-AIOps-Environment-Key  "";
proxy_set_header X-AIOps-Gateway-Assertion "";
proxy_set_header X-Forwarded-Host         "";
proxy_set_header X-Forwarded-Proto        "";
# 95 宿主 access_log 记 Cookie 字段；客户端身份走自定义头不走 Cookie，清空不透传。
proxy_set_header Cookie                   "";
proxy_set_header Host                     $host;
proxy_set_header X-Real-IP                $remote_addr;
proxy_set_header X-Forwarded-For          $proxy_add_x_forwarded_for;
proxy_connect_timeout 15s;
proxy_send_timeout 120s;
proxy_read_timeout 120s;
# 一次的查询失败不自动重发：POST 到问答/会话面重发会创建第二个作业。
proxy_next_upstream off;
proxy_buffering off;
proxy_cache off;
add_header Cache-Control "no-store" always;
