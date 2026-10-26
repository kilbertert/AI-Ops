# 管家工具制品同步与 TLS 回调切换

日期：2026-10-26。基线 `ec62108b`。工具本体的开发与隔离验收在
[2026-10-26-operator-cpms-tools](2026-10-26-operator-cpms-tools.md)，本片只记现役接线。

现役接线（2026-10-26）：合并树 `ec62108b` 的节点、HTTPS 内部回调、36 发布桥与登录代理已同步到 41/36 并独立复核（节点 JAR `96eb3428…` 与锁一致；真实 Java 用现役 truststore 握手得 403；明文 400；网关 200）。平台、store、model-proxy、Gateway 环境、业务目标与公司库未动，无业务 POST 或模型消费。两次失败均自动回滚并核到原摘要（`runtime.env` `4d411c21…`、回调 `dd622be1…`、`current` → `releases/b7617324`），备份在 `/var/backups/aiops-41/operator-delivery-20261026-r{2,3}`。平台库 `sai_client_node` 里节点仍在心跳续期（`expire_dt` ≈ NOW+8h），说明重建后真的重新注册，不是残留行。
