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

---

## 五、追加回执：受管发布条收进抽屉（`strip-drawer`，PR #1423）

**背景**：`ui-polish` 上线后实测发现 workflow header 右侧行拥挤——受管三组控件
（发布组/知识库/预览）平铺后与上游按钮叠成两行互相重叠。

**修法**：header 只留单个「受管发布」触发钮，三组内容收进 dify-ui Drawer（右侧抽屉，
`swipeDirection="right"`），预览/知识库改内联块；knowledge 在抽屉挂载时加载。

| 项 | 值 |
|---|---|
| 镜像 | `aiops/dify-web-patched:strip-drawer`（`89e8ac4d571f`，36 组装 `97077d328093`） |
| 产物 | 288M→gzip 85M，sha256 `89eaa8733c93b68638d7ed2a5c0bd06b451c06fb6d197cb6595a719f3186d99c` |
| 部署 | `/opt/dify/deploy/.env` DIFY_WEB_IMAGE + force-recreate，容器 Ready 无报错 |

**36 实测**（同取证通道，真实会话）：

| 断言 | 结果 |
|---|---|
| 头行整洁：仅一个 `受管发布` 钮，无重叠 | PASS（截图 `drawer-header.png`） |
| 抽屉打开三段齐整，含渠道警示/发布钮/版本操作/预览字段/知识库列表 | PASS（`drawer-open.png`） |
| `details`=0；checkbox=5；空问题置灰 | PASS |
| 浏览器规格 6/6（含抽屉内嵌套版本操作 Popover 不互关） | PASS |

**结论**：PASS。header 拥挤/重叠消除，交互与无障碍标记保持。

---

## 六、追加回执：开通操作行去重降噪（`ops-declutter`，PR #1426）

**背景**：`/apps` TenantSetup 面板的开通操作行既在行级重复错误文案，又在每个
入口 chip 里重复同一来源的 `messages[error_code]+failureDetail`，再加逐行脚注，
同一条信息出现两到三遍，行长且乱。

**修法**：行级只留状态徽标（已完成/部分完成/未完成）+ 各入口 chip（明细只此一份）
+ 失败行的重试钮；行级错误文案仅当操作失败且没有任何入口级明细时兜底；脚注
「开通完成的应用…」改为列表底部单例。

| 项 | 值 |
|---|---|
| 镜像 | `aiops/dify-web-patched:ops-declutter`（36 组装 `774d5d33d370`） |
| 产物 | 288M→gzip 85M，sha256 `41fd8541c83756c71aa51e800262c96e1dfa4c40607d1580eaf48684f53f39a8` |
| 部署 | `/opt/dify/deploy/.env` DIFY_WEB_IMAGE + force-recreate，容器 Ready 无报错 |

**36 实测**（同取证通道，真实会话，截图 `ops-declutter-panel.png`）：

| 断言 | 结果 |
|---|---|
| 状态徽标：已完成(绿)/部分完成(橙)/未完成(红) | PASS |
| 入口 chip 承载明细（`data-aiops-progress` 保持） | PASS：如「客户端客服：该端尚未接入使用渠道」「管家：草稿（原正式版继续服务）」 |
| 行级不再重复入口 chip 已含的错误文案 | PASS：未完成行只见徽标+chip+重试 |
| 脚注单例 | PASS：仅列表底部一处 |
| 容器 | `Next.js 16.3.4 ✓ Ready`，无报错 |

**结论**：PASS。操作行信息去重后整洁可读，`data-aiops-*` 断言面与重试路径保持。
