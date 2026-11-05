# 36 实测回执：受管界面 dify-ui 化（`ui-polish`）

> 对应 PR #1414（`feat/dify-web-ui-polish`，构建自 `4a0b17ca`）。
> 范围：三个定制面（`/apps` 的 TenantSetup 面板、configuration 的 ManagedPublish 条、
> workflow header 的 WorkflowManagedStrip 条）换成 dify-ui 原语；上游 Dify 界面未动。

## 一、制品身份

| 项 | 值 |
|---|---|
| 开发机镜像 | `aiops/dify-web-patched:ui-polish`（image id `ec95a1a295d2`） |
| 产物 tar | 288MB；gzip 后 85MB，sha256 `98c6e81c1338b504b1dcf402c4c14e53642da51fac2c0881a6296e7e177ff197` |
| 36 侧镜像 | `aiops/dify-web-patched:ui-polish`（`1e8c937bb1af`，36 上 3×COPY 组装） |
| 切流方式 | `/opt/dify/deploy/.env` `DIFY_WEB_IMAGE=`（备份 `.env.bak-web-ui-polish-*`）+ `--force-recreate web` |
| 回滚路径 | 改回 `.env` 指 `operator-followup` + force-recreate；上游镜像 tag 未动 |

传输备注：单流 scp 在跨国链路上从 ~500KB/s 衰减到 ~15–33KB/s；
改 8×11MB 并行分块后传完，拼块 sha256 与本机一致。

## 二、验证方法

- 通道：`tools/verification/dify-console-tunnel`（ssh `-L 18443` + CONNECT 代理 18080），
  浏览器 URL 保持 `https://dify.ranlei.work`，同源桥校验不变；真实登录会话（0600 会话文件）。
- 判据分两层：DOM 断言（新结构真的在渲染）+ 截图（观感）。
- 受管条的可见文案与旧版一致，所以「新 UI 生效」的判据是**结构性**的：
  `[data-aiops-*]` 区域存在 且其下 `<details>` 计数为 0（旧版用 `<details>` 实现折叠，
  新版用 dify-ui Popover）。

## 三、实测结果（2026-11-05，36）

| 面 | 断言 | 结果 |
|---|---|---|
| `/apps` TenantSetup | 面板展开渲染 | PASS（截图 `ui-polish-apps-expand.png`）：主按钮、开通范围 Checkbox、来源 Select、卡片式操作列表 + StatusDot 均在 |
| configuration ManagedPublish | `[data-aiops-managed]`=1；`details`=0；`版本操作` 可见 | PASS（截图 `ui-polish-config.png`）：紧凑工具条 `管家 · deepseek-v4-flash · 正式 V29` + `发布到管家` + `版本操作` |
| workflow WorkflowManagedStrip | `[data-aiops-workflow-managed]`=1；`details`=0；两个 Popover 可开 | PASS（`ui-polish-wfstrip/preview-pop/kb-pop.png`）：预览 popover 含问题/订单号字段；知识库 popover load-on-open 出 5 个 checkbox |
| 行为保持 | `开始预览` 空问题时置灰 | PASS：新 `loading={busy}` + `!question.trim()` 与旧逻辑等价 |
| 容器 | `docker logs deploy-web-1` | `Next.js 16.3.4 ✓ Ready`，无报错 |

## 四、结论

PASS。三个定制面已切换为 dify-ui 原语并在生产生效，交互与无障碍属性
（`data-aiops-*`、aria-label、role=status）保持，验证通道已关闭。
