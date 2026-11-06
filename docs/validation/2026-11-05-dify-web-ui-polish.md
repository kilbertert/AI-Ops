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

---

## 七、追加回执：客户/已开通客户选择器加关键字搜索（`tenant-search`，PR #1428）

**背景**：`/apps` TenantSetup 面板的「客户」下拉有 300+ 条客户行，纯滚动逐个
找不可用。同一面板里「已开通客户」也是同一形态。

**修法**：新增共享 `SearchSelect`（dify-ui Combobox `TriggerWithPopupInput` 形态：
trigger + 弹层内搜索输入 + 选项列表 + 空态），两个选择器一起换掉；
弹层默认按输入过滤，传 `itemToStringValue` 保证 options 重建时选中态稳定。
入口来源两项是小列表，保持 Select。

| 项 | 值 |
|---|---|
| 镜像 | `aiops/dify-web-patched:tenant-search`（36 组装 `998559b10c57`） |
| 产物 | 288M→gzip 85M，sha256 `c4e62d70b967077e9e8840f695804dc6aba8316856a753d95df324b51f33ff7a` |
| 部署 | `/opt/dify/deploy/.env` DIFY_WEB_IMAGE + force-recreate，容器 Ready 无报错 |

**36 实测**（同取证通道，真实会话 + 交互探针，截图 `tenant-search-open.png` /
`tenant-search-filtered.png` / `tenant-search-workspace.png`）：

| 断言 | 结果 |
|---|---|
| 「客户」下拉弹层含搜索输入（`搜索客户`） | PASS |
| 关键字过滤真在缩列表 | PASS：328 条 → 填「威昌」→ 1 条（永威昌盛） |
| 「已开通客户」下拉同样含搜索输入（`搜索已开通客户`） | PASS |
| 容器 | `Next.js 16.3.4 ✓ Ready`，无报错 |

**结论**：PASS。客户选择从滚动 300+ 行变为关键字即筛。

---

## 八、追加回执：主页只列租户自己的应用（`home-only-apps`，PR #1429）

**背景**：Dify 主页（`/`）下半屏是 explore 推荐的「模板」画廊——分类 tab、
搜索框、整墙模板卡片，与本部署只服务租户自有应用的定位不符。

**修法**：新增 `11-home-only-tenant-apps.patch`（两文件）：
- `features/home/home-content/home-content.tsx`：删 `HomeTemplatesHeader` +
  `TemplateCard` 网格 JSX，连带删掉专用状态（categories/keywords/
  filteredList/useQueryState/useLocale）与 `explore.apps` 查询
- `features/home/page.tsx`：删 `explore.apps` 服务端预取

保留：`ContinueWork`（最近）、LearnDify 与 HomeBanner（均 systemFeatures
门控）、TenantSetup 面板。

**验证分工**（本轮起按服务共识改为「先合并后部署」；协调方独占终态构建）：

| 层 | 检查 | 结果 |
|---|---|---|
| 补丁面 | `--dry-run` 打补丁通过（9 hunks） | PASS |
| 编译面 | esbuild 解析两文件通过；无悬挂引用 | PASS |
| 制品面 | 候选镜像产物 grep：`home-templates-title` 标记不存在、`复制并开通客户` 存在 | PASS |
| 终态 | main `b4afb94a`（含 #1429/#1430/#1432/#1433）统一构建 `release-b4afb94a`，36 容器 `ce992b141186` | — |

**36 实测**（终态镜像，同取证通道 + 交互探针，截图 `home-only-apps.png`）：

| 断言 | 结果 |
|---|---|
| Templates / 模板 区块（含分类 tab、搜索、卡片墙） | PASS：正文与 DOM 均不存在 |
| Workflow Planning Assistant（代表性模板卡） | PASS：不存在 |
| Welcome back + Recents（租户自有 app ×2） | PASS |
| 复制并开通客户 AI 服务（受管面板） | PASS |

**结论**：PASS。主页信息面收敛为 欢迎回来 + 最近（租户自有应用）+ 受管面板；
explore 模板数据不再请求也不再渲染。

**过程备注**：首轮构建误在主检出执行——`build-web-image.sh` 的
`VERSION_DIR` 是 `$PWD` 相对路径，产出的镜像不含分支补丁，36 实测仍渲染
画廊而"部署成功"。教训：构建必须在持有补丁的 checkout（worktree/合后 main）
里跑；本机 15G 内存被 webpack+多会话叠加压爆重启过一次，全机约定同一时刻
只跑一个 web 构建。
