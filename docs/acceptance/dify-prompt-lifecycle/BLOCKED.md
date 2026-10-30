# BLOCKED — dify-prompt-lifecycle

**有一个真实阻塞项：页面级与真实模型验收未执行。**

## 阻塞项

1. **本机没有可用的隔离 Dify 实例。** `127.0.0.1:10018`（Dify 控制台）不可达，
   本地 `docker ps` 里也没有 Dify 栈；`deploy/dify-chat/docker-compose.acceptance.yaml`
   需要拉取镜像与模型凭据。因此"真实编辑指定文本框 → 预览 → 发布 → 恢复"与
   "真实模型的风格观感"**无法在本机执行**，按 NOT_RUN 记录，不计入通过数量。
   可复跑脚本：`tools/verification/verify_prompt_lifecycle.py`（本地部分已全绿）。
   页面验收步骤见 `qa-plan.md` 第五节，前置条件由 Codex 在合并制品后提供。
2. **原应用正式验收**（41 上的真实 Dify／Java 节点／原 H5）依赖合并制品，本分支不部署。

## 已解决（第二轮）

- 独立复核的 10 条反例：全部转绿（`probe.py` 退出 0）。
- documents 契约的 clarify/not_found/human reply、归一化丢 reply、金额标签互换、
  状态说反、编造站名、英文可用性断言、带单位账单被误拒、正常指引被误拒。
- `verify_prompt_lifecycle.py` 的写死 pass 与 qa-plan 虚构参数。
- 正常发布后旧 QA 变 410（现按签发版本交付；撤回/换凭据/内容被换仍拒）。

## 上一轮的绕过记录（保留）

`dev-worktree start` 曾因另一会话的 dirty worktree 被挡，本片按同约定手工建 worktree；
收尾时 `dev-worktree audit` 已通过。未清理他人工作。
