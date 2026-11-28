# ADR-0364: 自动化未合入关闭 PR 分支清理与 GitLab 镜像周期兜底

Status: Accepted
Date: 2026-11-27

## Context

在对本仓 worktree、本地分支与远端分支做全量清点时，暴露出远端分支及历史分支持续堆积的 5 个根因：

1. GitHub 仓库设置 `delete_branch_on_merge` 为 `false`，PR 合入后不会自动清理源分支；
2. 已被替代而未合入关闭（`CLOSED`）的 PR 分支长期无人回收（上次清理中 36 条滞留 `origin` 分支有近半属此类）；
3. 宿主工具 `dev-worktree` 仅拉取/剪枝 `origin/<default>` 单一引用且默认不提供可选远端回收入口；
4. [gitlab-mirror.yml](../../.github/workflows/gitlab-mirror.yml) 仅靠实时事件推进，且本仓 `.git/config` 未显式固定 `remote.*.prune=true`；
5. 本机 `.git/gc.log` 静默阻止自动对象垃圾回收。

其中第 1、4（本地）及第 5 项通过仓库设置、本地配置及宿主共识工具仓独立修复；第 2 与第 4（GitHub → 公司 GitLab 镜像周期自愈）须在本仓工作流中建立确定性防护。

## Decision

1. **新增 [.github/workflows/cleanup-closed-pr-branch.yml](../../.github/workflows/cleanup-closed-pr-branch.yml)**：
   - 监听 `pull_request_target: types: [closed]`，以环境变量传参且**不检出**不可信 PR 代码；
   - 仅当 `merged == false` 且 `head.repo.full_name == github.repository`（同仓分支）时触发，并依次执行四重 Fail-Closed 保护：
     1. 拒绝空白、默认分支及 `main`、`master`、`release/*`、`standby/*` 保护分支；
     2. 查询 `pulls?state=open&head=...`，若仍有同名分支关联的在途 PR 则跳过；
     3. 若分支已被手动删除（HTTP 404）直接幂等成功退出；若非 404 错误则失败退出；
     4. 校验远端分支当前尖端 SHA 与关闭时刻 `head.sha` 完全一致，防止并发复推误删新提交后才通过 GitHub `git/refs/heads/...` DELETE API 删除分支。
   - `GITHUB_TOKEN` 发起的分支删除不会用 `delete` 事件启动另一个工作流。仅在 DELETE 成功后，以 `actions: write` 权限向仓库默认分支上的 `gitlab-mirror.yml` 发出 `workflow_dispatch` 请求。保护条件跳过、已不存在或删除失败均不发请求。请求失败则明确标记清理任务失败；GitHub 分支已删除，可手动触发镜像或由后续镜像运行对账。
2. **为 [.github/workflows/gitlab-mirror.yml](../../.github/workflows/gitlab-mirror.yml) 增加每周兜底调度**：
   - 保留 `push`、`delete` 与 `workflow_dispatch`，新增 `schedule: - cron: "15 3 * * 0"`，在偶发 webhook 丢失后定期全量 `--mirror` 对账自愈。
3. **新增行为回归测试 [tests/tooling/test_pr_branch_cleanup_workflow.py](../../tests/tooling/test_pr_branch_cleanup_workflow.py)**：
   - 校验两份 workflow 声明形状、最小权限与禁止检出约束，并抽出真实 shell 脚本针对伪造 `gh` CLI 覆盖保护分支、同名在途 PR、已删分支、尖端前移、正常删除后按顺序触发镜像、删除失败、触发失败及特殊字符安全转义。

## Alternatives considered

### 1. 靠定期人工或 Agent 跑全量清理脚本替代事件驱动删除
- **最强论点**：不需要维护额外 GitHub Actions 工作流文件。
- **否决原因**：人工清理永远滞后且容易与在途分支/镜像产生竞态；结合 `delete_branch_on_merge=true` 与 `pull_request_target` 四重守卫，关闭 PR 时即开始自动清理，不依赖人工全量扫描。

### 2. 使用 `actions/github-script` 内联编写清理逻辑
- **最强论点**：不必通过命令行调用 `gh api`。
- **否决原因**：`bash + gh api` 形态与仓库现有 `.github/workflows/ci.yml` 测试模式一致，测试脚本可直接用假 `gh` 二进制逐字驱动线上 `run:` 正文，避免脚本漂移。

## Consequences

- PR 合入由 GitHub `delete_branch_on_merge=true` 立即删除源分支；未合入关闭的 PR 分支由 `cleanup-closed-pr-branch.yml` 安全删除；合入后的原生分支删除继续由 `delete` 触发镜像；自动清理未合入分支则显式请求同一镜像工作流。请求成功只表示镜像运行已触发，不表示 GitLab 已同步；需要时查看镜像运行结果。独立事件、手动触发和每周定时仍可兜底。
