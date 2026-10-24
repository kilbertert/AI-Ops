# M3-02 节点 SOP 发布与执行

默认关闭。公司 App 继续原聊天 POST/GET/blocks；运营使用既有发布、试运行、撤回和历史恢复按钮。

部署先预置私有 `AIOPS_GATEWAY_CHAT_NODE_TARGETS_FILE`（0600）：

```json
{"schema":"aiops-node-targets/1","targets":[{"app_id":"<编辑应用UUID>","tenant_id":"<租户>","environment":"<逻辑环境>","config_file":"<节点平台登记文件>","manifest_file":"<受控模型与工具模板>"}]}
```

同时配置已有 `CHAT_ROUTE_BINDINGS_FILE`，指向 Gateway 数据库（版本与路由同库）；相同路由不能同时由旧文件绑定和管理表声明。
节点启用 `aiops.node.managed-publication=true`（Spring 参数）。未启用时仍走原文件清单。
模板没有模型密钥；模型凭据仍由既有平台注入，节点不会从发布接口获取它。

源应用必须为 chat/agent-chat/completion，取其 `model_config.pre_prompt` 作为唯一 SOP system 消息。
保存只改草稿；试用另建 run；发布才推进正式版本。恢复从不可变版本选清单，不重新读取当前草稿。
模型与选项变更本片不通过编辑器交付；平台实际值不匹配模板时拒绝，不自动修正或回落。

节点在原派发收到 sid 后，调用 `POST /v1/internal/charging-status/execution-config/{run_id}`：
节点令牌认证 → 当前绑定/订单授权 → 授权后复核节点与run → 返回清单原字节与摘要。
该端点不返回订单状态。模型工具继续用原 claims/completions 端点，且携带同一清单摘要。

复跑（依赖沿用 m3node 手册，JDK 镜像固定到 digest）：

```bash
M3NODE_DEPS=<锁定jar目录> M3NODE_JDK_IMAGE=<JDK镜像digest> \
  uv run python tools/m2_close/m3node/verify_node_publication.py --out <私有报告路径>
```

真实 Gateway + Java Spring 节点，编辑器/平台/模型/业务和身份均为合成。另运行原
`verify_chat_node_entry.py` 及 `run.sh` 保证旧节点路线和模型守卫兼容。
生产启用、真实公司目录/供应商/业务验收另行进行，不把本地合成成功当作真实业务闭环。

## 真实调度平台与打包节点

以下验收使用部署锁中的 Snail 平台与节点 JAR，不由 Python 替身派发节点方法：

```bash
uv run python tools/m2_close/m3node/verify_platform_publication.py \
  --platform <平台jar> --node <节点jar> --image <已有Gateway镜像> --out <私有报告.json>
```

预先核对 JAR 和基础镜像摘要；复用原平台初始化，在 Docker 内部网络启动真实平台、
gRPC 节点与 Gateway，不发布宿主端口。编辑器、模型、CPMS、调用者身份和目录仍为合成。
覆盖保存/试用/发布/撤回/恢复及上游失败，留同 run 快照、模型收到的 SOP 和节点摘要。
与旧 M2 验收共用互斥锁；发现已有实验容器/网络即拒绝，不清理别人实例。
结束清理自己的容器，保留私有临时根及日志。报告的代码身份前后不一致时判失败。
