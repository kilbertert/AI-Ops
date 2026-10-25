## 2026-10-25 · 平台账号发布：哨兵 `-1` 被当成租户 id

**目标/分支**：`fix/platform-account-publish-workspace`，基线 `beaeb88`。

### 一、查的是什么

用户报告：`system` 在 Dify 控制台点「发布到 AI-Ops」被拒，提示「你所在的租户还没有
可用的 Dify 工作区」。而 36 上 workspace 早就建好了。

顺带核实了 ADR-0056「多 workspace」清单的完成度：

| 清单项 | 状态 |
|---|---|
| 1. Dify 侧按租户建 workspace | **已完成**：36 上三个（41-Ulink / platform / admin-legacy） |
| 4. 发布桥按发起人租户解析 | **已完成**（`pinned_workspace` 已改成解析值） |
| 5. 试运行的草稿来源按租户解析 | **已完成**：`_publish_bound_app` 用 `_workspace_for_tenant(context, binding.tenant_id, …)`，注释明写「不能再用常量」 |
| **平台账号落哪个 workspace** | **缺失** ← 本次修的 |

### 二、根因

平台账号的 `tenant_id = -1` 是哨兵值。桥把它原样发给
`/v1/dify/workspace?tenant_id=-1`，网关 `_SAFE_ID` 校验首字符必须是字母或数字 →
400 → 桥把所有非 2xx 译成「没有工作区」。

登录侧 #882 早就把平台账号映射到 `platform` workspace，发布桥漏了同一层。

### 三、第一版是错的

从 `dify_login_service` import 常量 → 本地全绿、桥在服务宿主上 `ModuleNotFoundError`
（部署制品只含五个模块）。改为桥内重写 + 一条用例钉回出处。

**这是本轮唯一一个「本地测不出来、部署才暴露」的缺陷**，值得记。

### 四、判据

`3957 passed, 9 skipped`；三条新用例，平台映射那条做过变异。

### 五、没做的

- 桥把所有非 2xx 译成「没有工作区」，400/404 不可区分 —— 另记。
- ADR-0056 清单第 2、3、6 项（映射表加 `dify_tenant_id`、开户接 `tenant_id`、
  发布权的 workspace 维度）未逐条核实 —— 本次只确认了第 1、4、5 项与平台账号缺口。
