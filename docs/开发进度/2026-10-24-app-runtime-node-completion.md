# 2026-10-24 · APP-RUNTIME-01 续办

## 目标（不变）

公司 App 原聊天入口 → 可信绑定的客户节点 → 同一 run 状态 → 原 GET/blocks。
#1046 已合并，但独立复核证明它只完成了接线的一半；本片补齐并跑通真节点整链。

## 已完成

| 阶段 | 状态 |
|---|---|
| 0. 基线 | ✅ 98 passed 现场复跑；复核时 main `5a45567` |
| 1. 按租户绑定 | ✅ `aiops-chat-execution/1` 登记表 + `agent_runs.chat_binding` |
| 2. 清单必需 | ✅ 加载期 / 签发期各拒一次 |
| 3. 共享最终放行 | ✅ `chat_execution.final_release`，三个调用方共用 |
| 4. 停用后无正文状态可读 | ✅ 只拒绝正文 |
| 5. 失真测试修正 | ✅ patch 目标、`Path` 参数、真事件握手、真断言 |
| 6. 真 Java 节点整链 | ✅ `verify_chat_node_entry.py` 18/18 |
| 7. 反向验证 | ✅ 7 个变异，各自转红 1–3 条 |
| 8. 文档 | ✅ 验收契约、qa-plan、ADR-0104 修订、README、`.env.example` |

## 配置

```dotenv
AIOPS_GATEWAY_CHAT_EXECUTION_BINDINGS_FILE=
```

一张 0600 的登记表（见 `.env.example`）。默认空 = 关闭。

## 断点续跑

分支 `fix/chat-node-binding-manifest-release`，工作树
`.worktrees/AI-Ops-chat-node-binding-manifest-release`。

真节点整链复跑：

```bash
M3NODE_DEPS=… M3NODE_JDK_IMAGE=… \
  uv run python tools/m2_close/m3node/verify_chat_node_entry.py --out /tmp/chat-node.json
```

## 仍未完成

真实外部平台/供应商、真实公司 UPMS 目录、登录 App 验收、SOP 发布绑定（留待 M3）。
生产未启用。
