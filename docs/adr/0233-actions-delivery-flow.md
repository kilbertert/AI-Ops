# 交付检查、自动同步和发布阶段分离

Status: Accepted

## 背景与决定

密集并行开发时，每次 main 更新都 rebase 开放 PR，会产生额外 synchronize、重复 CI，
并改变作者本地分支之外的提交身份。现有工作流还把部署与发布说明写在同一作业，
发布元数据失败会被显示成部署失败。此次保留 `verify`、`Workflow policy` 的门禁身份，
把优化放在执行与证据层；候选合并前仍须包含最新 base、通过确定性 CI 并经人工 review。

自动事件只报告；手动明确指定一个同仓库所有者、非草稿、无人占用的 PR，
通过带 `expected_head_sha` 的更新 API 合入 main，不重写历史，不自动启动冲突修复。
CI 增加 XML 与构建证据；CD 分成部署和发布说明两个作业，后者失败可单独重跑。
守望者按 deploy-41 的结果判断部署，未结束的发布作业不会冒充生产部署排队。

## 依据

以下是平台规范的具体映射，而非泛化的“最佳实践”：

- [必需检查的 skipped / Pending 语义](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)：
  不给必需 workflow 加顶层路径过滤，保留 draft→ready 事件与固定检查名。
- [PR 事件与测试合并分支](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request)：
  CI 验证合并结果，ADR 检查读取 head；不再以一次 squash 样例推导所有合并树相等。
- [并发控制](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)：
  过期 PR 检查可取消，正在部署的作业不取消；一个 pending 不是 FIFO 队列。
- [更新分支 API](https://docs.github.com/en/rest/pulls/pulls#update-a-pull-request-branch)：
  expected_head_sha 在作者新推送后拒绝旧请求；占用字段在更新前读取。
- [pytest 退出码](https://docs.pytest.org/en/stable/reference/exit-codes.html)与
  [JUnit 输出](https://docs.pytest.org/en/stable/how-to/output.html#creating-junitxml-format-files)：
  退出码保持失败，XML 提供结构化结果，不依赖终端摘要排版。
- [最小权限与 runner 边界](https://docs.github.com/en/actions/reference/security/secure-use#hardening-for-self-hosted-runners)：
  PR 检查仍在 hosted runner；生产访问仍限现有 CD，contents:write 只给发布作业。

## 替代方案

- **继续全部自动同步**：每个 PR 都较快接近 main，但高频合并放大重跑并改写远端历史；
  改成手动指定同步，代价是作者在交付前主动同步；本仓 auto-merge 未开启，不依赖它做筛选。
- **GitHub 原生 merge queue**：适合在高并发合并时验证组合；
  [当前可用条件](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue)
  要求组织拥有仓库，本仓当前属个人账户，故不添加无消费者的 merge_group 或自造队列服务。
- **拆成多个必需 CI 名称**：并行显示更细，但会扩大 Ruleset 迁移面；当前主要证据是无关更新导致的重跑，
  先保留两个必需入口、把步骤及产物说清楚。
- **workflow_run 串联 CI/CD**：方便跨 workflow 取制品，但带来上游来源、权限和 SHA 校验；
  现有 protected-main push 已能限定交付来源，本轮不引入这条额外链路。

## 后果与边界

增加镜像构建会增加单次全量 CI 时间，不能据此承诺每次更快；收益首先来自减少无关分支更新，
并把构建失败提前到合并前。文档档仍是源码引用启发式，不宣称完整依赖分析。
保留现有数量与 skip 门槛；后续改门槛应有独立依据。

release-notes 仍使用装有 canonical dev-changelog 的既有 runner，不复制治理工具到 hosted 环境。
部署锁和发布锁分别串行；没有增加部署权限或更改访问链路。SSH 故障、其他组件部署和真实业务验收
仍须各自处理，不能把工作流整理当作已恢复生产。实施状态以本轮验收记录为准。
