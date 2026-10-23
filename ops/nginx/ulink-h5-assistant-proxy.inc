# 每个 location 共用的代理设置（被 ulink-h5-assistant.conf `include`）。
#
# 文件名用 `.inc` 而不是 `.conf`：vhost 的 `extension/<host>/*.conf` 是按 glob 载入的，
# 而那段 include 在 **server 上下文**里 —— 这份内容必须待在 location 内，不能被单独载入。

include /etc/aiops-41/nginx-aiops-service-token.conf;
proxy_pass http://172.18.0.1:8788;
proxy_http_version 1.1;
proxy_set_header Authorization      $aiops_service_authorization;
proxy_set_header X-Third-Session    $http_third_session;
proxy_set_header X-Business-Entry   consumer;
proxy_set_header X-AIOps-Source-Key "";
proxy_set_header Host               $host;
proxy_set_header X-Real-IP          $remote_addr;
proxy_set_header X-Forwarded-For    $proxy_add_x_forwarded_for;
proxy_connect_timeout 15s;
proxy_send_timeout 120s;
proxy_read_timeout 120s;
# 一次的查询失败不自动重发：POST 到诊断面重发会创建第二个作业。
proxy_next_upstream off;
proxy_buffering off;
proxy_cache off;
add_header Cache-Control "no-store" always;
