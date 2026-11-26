# 41 ↔ 95 跨环境隔离只读实网验证与同环境多租户独立停用恢复（ADR-0347）交付回执（2026-11-26）

- **适用版本与运行面**：
  - `41` 控制面 Gateway：**`1.40.4+9c1b35a57d84`**（`release-9c1b35a57d84`，标准 CD `45759491745` 成功；累计合入 PR #1737 `c6ef0153` 与 PR #1740 `9c1b35a5` / `ADR-0347`；`/health` 返回 `business_mutations: disabled`，`execution_environment=41`、`registry.json revision: 13`，`41` 两现役租户 `Ulink`/`优链充` 启用、`95` 双租户 `false`）。
  - `36` 共享控制台：`deploy-web-1` 运行 **`aiops/dify-web-patched:onboarding-6dac04a1`**（含 PR #1738 `4f7acbe2` 视觉层级重构与 PR #1739 `6dac04a1` 首屏 3 主 Tab 极简化）；`kb-service --scoped-account`、`aiops-dify-login-proxy`（多环境 `ADR-0276`）与 `aiops-dify-console-bridge` 保持运行。
  - `95` 目标面 Gateway：**`1.40.4+a98898014689`**（`gateway.db user_version=4`，`environment.json revision: 14`，`enabled=false`，双租户 `false`，`active_routes=0`；本次验证全程保持 95 停用安全态，未触碰或重启 95）。
- **适用范围**：
  1. 补齐 **41 ↔ 95 跨环境知识检索正文隔离**（双向 `16/16`，含上游授权拒绝响应核验）与 **41 → 95 单向正式 HTTPS 问答/会话正文跨环境隔离**（`11/11`，95 保持停用安全态；反向 `95 有效会话 → 41` 待获批 95 激活窗口补测）；
  2. 记录 PR #1737（发布恢复 `reconcile` 终态判定优先与 `.bak` 冲突修复）、PR #1738 / #1739（`/aiops/environments` 体验重构与极简化部署）及 PR #1740（**ADR-0347 / D52 同环境多租户任意顺序独立停用恢复**）的合入、契约验证与控制面 CD；
  3. 核查旧路线与 `#1546` 收尾边界。
- **未触碰边界**：零公司仓库改动、零公司服务/业务库写操作、`95` 保持双租户停用安全态、`124` 保持 `draft_only` 未动。

---

## 一、41 ↔ 95 跨环境知识检索正文隔离（双向只读实网 `16/16` 全过）

在 `36` 现役 `kb-service --scoped-account` 上覆盖全部 4 个作用域主体（`41/1942105476598861824` Ulink [5 库]、`41/1941847786056323072` 优链充 [1 库]、`95/2019588094906601472` 平高 [2 库]、`95/1783022023241633792` 小趋充电 [1 库]），经正式 `KbServiceClient` 与直连 `kb-service` 上游响应核验完成 `4 × 4 = 16` 格清单可见性与正文切片检索矩阵：

| 类别 | 格子数 | `KbServiceClient` 与上游 `kb-service` 实测结果 |
|---|---|---|
| 对角线（本主体访问自有知识库） | `4 / 4` | 全部 `in_caller_list=true`，上游 `POST /kb/knowledge-bases/{id}/search` 返回 `HTTP 200`，检索命中真实正文切片（`2 / 2 / 1 / 1` chunks） |
| 跨环境非对角线（`41 → 95` 与 `95 → 41`） | `8 / 8` | 全部 `in_caller_list=false`；直连 `kb-service` 全部返回 `HTTP 502`（`error="upstream_error"`），消息明确携带上游 RAGFlow 归属权拒绝 `code=102 msg=Only owner of dataset <id> authorized for this operation.`；`KbServiceClient` 映射为 `KnowledgeSearchUnavailable`，0 切片泄露 |
| 同环境跨租户非对角线（`41` 内与 `95` 内互访） | `4 / 4` | 全部 `in_caller_list=false`；直连 `kb-service` 全部返回同款上游归属权拒绝（`code=102 / Only owner of dataset <id> authorized for this operation.`），0 切片泄露 |
| **合计** | **`16 / 16`** | **双向零跨环境/跨租户知识正文泄露（含上游权限拒绝实证）** |

---

## 二、41 → 95 正式 HTTPS 问答与会话正文跨环境隔离（单向只读实网 `11/11` 全过）

在 `95` 全程保持双租户停用安全态（`enabled=false`）的前提下，使用 `41` Ulink 真实有效消费端会话在正式公网入口 `https://api.mall.qushiyun.com`（及对照 `https://api.qumall.qushiyun.com`）执行跨环境问答/会话只读与追问探针（完整生产 QA/会话标识仅留存于 `0600` 私有证据文件，不在公开回执披露）：

| 核验项 | 次数 | 实测响应 | 结论 |
|---|---|---|---|
| 本人正向对照（`41` Ulink 正式问答回读） | `1 / 1` | `HTTP 200`（`completed`，6 blocks，标识留私有证据） | 通过 |
| `41` 会话跨环境读取 `95` 平高/小趋 4 条真实已完成 QA（消费端 + 管家端） | `4 / 4` | 全部 `HTTP 404`（`QA_NOT_FOUND`） | 通过 |
| `41` 会话同环境跨租户读取 `41` 优链充真实已完成 QA | `1 / 1` | `HTTP 404`（`QA_NOT_FOUND`） | 通过 |
| `41` 会话跨环境读取 `95` 平高/小趋 2 条真实会话详情（`GET`） | `2 / 2` | 全部 `HTTP 404` | 通过 |
| `41` 会话跨环境向 `95` 平高/小趋 2 条真实会话发起追问（`POST`） | `2 / 2` | 全部 `HTTP 404`（`CONVERSATION_NOT_FOUND`） | 通过 |
| `41` 有效会话直接提交至 `95` 正式 HTTPS 入口 | `1 / 1` | `HTTP 401`（`INVALID_ACCESS_TOKEN`） | 通过 |
| **合计** | **`11 / 11`** | **`41 → 95` 单向及 `41` 内跨租户零正文泄露** | **全过** |

> **边界说明**：本节 11 项均以 `41` 消费端有效会话发起且 `95` 维持停用态，因此证明的是 `41 → 95` 方向及 `41` 内跨租户隔离；反向「`95` 有效激活会话访问 `41` 问答/会话正文」需在后续获批 `95` 受控激活窗口内补测。

---

## 三、工程缺口收敛与 ADR-0347（D52）合入交付

| PR / 修订 | 解决的问题 | 验证与部署状态 |
|---|---|---|
| **PR #1737**（`c6ef0153`） | 修复发布恢复时旧代码先判 `phase=="restored"` 而跳过 `state=="reconcile"` 的终态顺序缺陷（改为 `reconcile` 优先，确保中央同步失败时不提前标 `cancelled`），并修复同摘要私有 `.bak` 备份 `O_EXCL` 冲突 | CI 全绿；已随标准 CD 部署至 41 |
| **PR #1738**（`4f7acbe2`）& **PR #1739**（`6dac04a1`） | 重构 `/aiops/environments` 视觉层级并极简化首屏为 3 主 Tab 与单一行动引导 | CI 全绿；已构建并部署至 36 `deploy-web-1`（`aiops/dify-web-patched:onboarding-6dac04a1`） |
| **PR #1740**（`9c1b35a5`，[ADR-0347](../adr/0347-multi-tenant-independent-publication-restore.md) / D52） | 补齐同环境多租户同时激活后按任意顺序（含非 LIFO）独立停用其一而不中断另一租户的完整协议：目标端新增环境级 `publication-delegation-baseline.json`、`publication-tasks/` fail-closed 信任扫描、稳态 `check_files` 摘要绑定与零重启精细停用；控制面新增 `restore_task()` 单环境并发互斥（`ENVIRONMENT_TASK_BUSY`）、跨任务（含过期 `verify` 遗留 staged）peer 委派检测与最后租户双 CAS 回滚 `references.json` 及中央目录 `enabled=false` 收敛 | 经 2 轮独立对抗审查（14 项缺陷全修复）、Devin Review 全部 5 项反馈闭环、本地 114 项定向测试与全量 CI（4/4）通过；已合并并随 CD `45759491745` 部署至 41（`1.40.4+9c1b35a57d84`） |

---

## 四、旧路线与 `#1546` 收尾核查

- **Issue `#1546`**（原针对备用受限入口的 48 小时临时周期）：已由 [ADR-0306](../adr/0306-formal-scoped-dify-service-ingress.md)（PR #1654）将该备用受限入口转为正式服务 API 入口，同时保留原 `443` 入口继续排障观察，不作误关。
- **可选运行安全合同**：`tools/content/render_business_ingress.py` 与 `ops/nginx/business-environment-ingress.*` 按既有交付状态声明作为可选运行安全合同保留，不作破坏性删除。

---

## 五、明确未宣称范围与仍挂起阻塞

1. **ADR-0347 95 实机双租户同时激活窗口未执行**：PR #1740 已完成自动化契约与恢复矩阵验证（114 项）并部署 41 控制面（`1.40.4+9c1b35a57d84`）；95 目标面当前仍运行 `1.40.4+a98898014689` 且保持 0 活跃租户安全态。实机验证「95 平高与小趋同时激活 → 仅停用其一保持另一租户可用 → 再停用最后租户回归 `enabled=false`」需在获批受控窗口内先将 95 受管升级至 `9c1b35a5` 或更新修订后执行。
2. **跨环境正式 HTTPS 反向（`95 有效会话 → 41`）问答/会话正文隔离未实测**：因本轮保持 `95` 全程停用安全态，反向 `95 → 41` 正式 HTTPS 正文隔离留待下一次获批 `95` 激活窗口一并补验（双向 `41 ↔ 95` 知识库检索正文隔离 `16/16` 与消费者登录跨环境解析拒绝均已完成）。
3. **业务语义准确率未人工验收**：本回执与前序双环境 P5 回执共同证明技术链路与隔离闭环，不宣称工程师或业务方已完成语义准确率签收。
4. **原生 Android APK 外部阻塞**：`#1353`（连续会话）与 `#1422`（Markdown 呈现）仍待公司发包，正式 HTTPS 与 H5 证据不替代原生客户端。
5. **同租户第二真实用户隔离**：尚缺同租户第二真实用户授权材料，不创建或冒充公司用户。
6. **95 历史误回显测试 operator 令牌后端精确撤销回执**：正式长期开通 95 前提条件不变，95 继续保持 `enabled=false` / `active_routes=0` 安全态。

> 原始脱敏外私有核验摘要（`0600` 权限，`kb-cross-env-retrieval.private.json`、`kb-cross-env-upstream-http.private.json` 与 `qa-conv-cross-env-isolation.private.json`）留存于受控私有运维缓存目录，不入公开仓库。
