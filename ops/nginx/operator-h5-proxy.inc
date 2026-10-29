# 复用公司网关的来源密钥注入；不覆盖用户身份，不接受客户端来源密钥。
proxy_pass http://127.0.0.1:30899;
proxy_http_version 1.1;
proxy_set_header Authorization $http_authorization;
proxy_set_header X-Business-Entry operator;
proxy_set_header X-AIOps-Source-Key "";
proxy_set_header X-Third-Session "";
proxy_set_header third-session "";
proxy_set_header Host $host;
proxy_connect_timeout 15s;
proxy_send_timeout 120s;
proxy_read_timeout 120s;
proxy_next_upstream off;
proxy_buffering off;
proxy_cache off;
add_header Cache-Control "no-store" always;
