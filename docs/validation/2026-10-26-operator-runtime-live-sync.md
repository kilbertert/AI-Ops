# 2026-10-26 · 管家工具制品同步与 TLS 回调切换（现役执行）

基线 `ec62108b`（#1095 合并后）。本片只做**制品同步与回调协议切换**；管家业务目标
**未登记**，入口不开放，无业务 POST、无模型消费。工具开发与隔离验收见
[operator-cpms-tools](2026-10-26-operator-cpms-tools.md)，两片不互相代替。

## 执行

合并树 `ec62108b` 的制品已同步到两台现役主机，并独立复核（不只信脚本自述）：

| 主机 | 改动 | 复核 |
| --- | --- | --- |
| 41 | 节点落到 `releases/ec62108b`、`current` 指向它；回调 `listen 172.30.41.1:8788 ssl` 并反代 `172.18.0.1:8788`；`AIOPS_NODE_GATEWAY_URL` 改 HTTPS | 容器内 JAR `96eb3428…703e` 与锁一致；节点容器内真实 Java 用现役 truststore 握手得 `403`（无凭据被拒）；明文进 TLS 端口得 `400`；网关本体 `200` / `0.2.0+0179347e8ba0` |
| 36 | 发布桥与登录代理更新到同一修订 | 桥：`dify_console_publish.py` 摘要 `44980ee1…f533` 与合并树逐字相同。登录代理：`dify_login_server.py` `7b64bcad…ed8f`、`dify_login_service.py` `fb253a62…b8c7` 与合并树逐字相同；文件写入 09:23:55 CST、单元进程 10:04:23 CST 启动 —— **进程晚于文件**，跑的是新代码。两个单元 active |

**未动**：平台、store、model-proxy 容器（`Up 29h`）、Gateway 环境、业务目标表（仍只有
consumer，零处 `OPERATOR_TRIAL`）、公司库。没有本轮业务 POST 或模型消费。

切换过程中失败两次，均由脚本自动回滚并核到原摘要（`runtime.env` `4d411c21…`、
回调 `dd622be1…`、`current` → `releases/b7617324`）：第一次是回调重启后**立即**探活
（重启返回 ≠ 已监听），第二次是交互式 Java 探针**无自身超时**。改为显式 `timeout`
包裹的轻量探针后才通过。备份在 `/var/backups/aiops-41/operator-delivery-20261026-r{2,3}`。

**这不构成业务验收**：真实管家账号、真实 UPMS/CPMS 权限、baoyun 实际调用、原管家页面
逐条验收、旧工具退役、工程知识接入都仍未做；operator 目标刻意未登记，管家入口暂不开放。
