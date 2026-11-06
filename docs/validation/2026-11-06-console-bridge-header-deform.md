# 回执：编排页头部遗产按钮变形修复（PR #1436）

日期：2026-11-06 · 制品修订：`9b6a6b73`（合并后 origin/main）· 部署：桥 bundle → 36

## 问题

工作流编排页头部出现 5 颗 AI-Ops 钮：React 受管条「受管发布」+ 桥脚本
`dify-publish-button.js` DOM 注入的 4 颗遗产钮（在 AI-Ops 试运行/撤回/恢复/发布到
AI-Ops）。点击节点、右侧配置面板展开挤压头部 flex 行后，遗产钮文字折行变形。

## 根因（两层）

1. **守卫标记失配**：脚本判 `[data-aiops-managed="true"]` 退场——那是受管面板本体
   （`aiops-management.tsx` ManagedPublish，抽屉内容）的标记，抽屉未开时 DOM 缺席；
   头部受管条的常驻标记是 `data-aiops-workflow-managed`。工作流页上守卫形同虚设。
2. **对账循环会停表**：挂载循环「挂满 4 颗或试 40 次」即 `clearInterval`。受管标记
   由 React 异步挂上（状态未回遗产钮已注入），停表后再无人摘除——按钮以非 React
   节点永久赖在头部。另 `aiopsMakeButton` 无 `white-space:nowrap`，挤压即折行。

## 修复

- 守卫改并集 `[data-aiops-managed="true"], [data-aiops-workflow-managed]`（两脚本同改）
- 挂载循环改无上限对账（每 tick：有标记→摘净，无→补缺），对齐 `dify-knowledge-open.js`
  已验证范式
- 注入按钮补 `white-space:nowrap;flex-shrink:0`
- 测试补钉：并集标记/`clearInterval` 禁现/不折行/预览守卫同源

## 部署与验收证据

- `build-bundle.sh origin/main` → `/tmp/bridge-9b6a6b73.tar.gz`
  sha256 `698d6ca8…a099`，`dev-host cp 36` 上传
- 36 上验外层摘要 → 解压新暂存 → `deploy.sh`：`installed revision=9b6a6b73`
  （六条路由无会话 403 探活全过，nginx -t/reload 正常，备份 `/root/bridge-backup-20261106-9b6a6b73`）
- 服务端投放校验：`/__aiops/publish-button.js` 含并集标记与 nowrap
- 浏览器实测（WF01 受管应用 `/app/394b5399…/workflow`，点击节点后）：
  `aiops-*-btn` 四钮 DOM 中**零存在**、`data-aiops-workflow-managed` 在、
  头部区（视口顶 60px、高 >44px）零折行按钮；截图确认头部仅 `Features | 受管发布`
- 判据说明：avatar/画布节点徽标为结构性多行，不属于变形
