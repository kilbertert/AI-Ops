# 95 受管升级（`9c1b35a5`）与 ADR-0347 双租户同时激活 / 非 LIFO 独立停用 / 反向跨环境隔离现场验收回执（2026-11-26）

- **适用版本与运行面**：
  - `41` 控制面 Gateway：**`1.40.4+9c1b35a57d84`**（`release-9c1b35a57d84`，`/health` 返回 `business_mutations: disabled`，`execution_environment=41`；演练终态 `registry.json revision: 17`，`41` 两现役租户 `Ulink`/`优链充` 启用，`95` `enabled=false`、双租户 `false`）。
  - `36` 共享控制台：`deploy-web-1` 运行 **`aiops/dify-web-patched:onboarding-6dac04a1`**（3 主 Tab 极简管理页），`kb-service --scoped-account`、多环境登录代理（`ADR-0276`）与管理桥保持运行。
  - `95` 目标面 Gateway：本次获批受控维护窗口中，经控制面受管制品更新与 `/aiops/environments` 页面一键开通受管安装器，由 `1.40.4+a98898014689` 升级至 **`1.40.4+9c1b35a57d84`**（`gateway.db user_version=4`，`/health` 返回 `business_mutations: disabled`；演练终态 `environment.json revision: 18`，`enabled=false`，双租户 `false`，`active_routes=0`）。
- **适用范围**：
  1. `95` 经受管安装器原地升级至 `1.40.4+9c1b35a57d84`；
  2. 同环境双真实租户（平高 `2019588094906601472` 与小趋充电 `1783022023241633792`）**同时激活上线**与四路正式 HTTPS 并发核验；
  3. 补齐 **反向 `95 有效激活会话 → 41` 正式 HTTPS 问答/会话正文跨环境隔离（`22/22`）**（与前序 `41 → 95` `11/11` 及双向 `41 ↔ 95` 知识正文检索 `16/16` 共同构成双向实网证据）；
  4. 现场验证 [ADR-0347](../adr/0347-multi-tenant-independent-publication-restore.md)（D52）：**非 LIFO 先单独停用先激活的平高租户**（95 零重启、小趋持续正常响应），随后停用最后租户小趋并完整回滚环境委派基线与中央/目标双目录 `enabled=false`。
- **未触碰边界**：零公司仓库改动、零公司服务/业务库写操作、`41` 两现役租户全程无中断、`124` 保持 `draft_only` 未动；完整生产 QA/会话标识及脱敏外证据仅留存于 `0600` 私有运维缓存目录，不在公开回执披露。

---

## 一、95 受管升级与双租户同时激活（Phase A）

1. **受控制品注册与 95 受管升级**：
   - 在 `41` 通过 `aiops-admin environment-artifacts` 原子更新受控引用至合并修订 `9c1b35a57d84441dbbb52c12cbf121785c2ee657`（预委派控制面引用基线 `T0` 摘要：`779253630ce8823797ed488d47b83d3a25537f65672a0a09043b587065632dff`）。
   - 在真实浏览器 `/aiops/environments` 保存 95 草稿并生成冻结预检计划（9 项本地制品/清单/配置校验全过）→ 触发平高四阶段一键开通 `task_3c48dd4334c33aa872fc2ba8fde643d0`（`candidate-prepare` → `candidate-install` `task_e4d096f0e1c38d23600aa3674f89d7cb` 将 `95` 升级至 **`1.40.4+9c1b35a57d84`** → `candidate-register` → `candidate-validate` 三能力全 `passed`，`ready_to_publish=true`）。
2. **双租户依次激活实现同环境同时在线**：
   - 激活平高（`task_8310c23449fbf904d6077efede4965d0` → `observed / phase=activated / directory=synchronized`）：目标端自动生成 `/opt/aiops-access-95/publication-delegation-baseline.json`（`0600`），控制端生成 `delegation-baseline-95.json`（`0600`），`41 reg rev 14`。
   - **保持平高活跃在线**，在 `/aiops/environments` 触发小趋充电四阶段一键开通 `task_08b1eefa0c1e12ffa708e98ba35190ac`（全绿 `ready_to_publish=true`）→ 确认发布激活 `task_f7f515cd682f2f270146012b9ee82c34`（`steps.source.state="unchanged"`，自动复用已激活环境委派基线，最终 `observed / phase=activated / directory=synchronized`）。
   - **双租户同时在线基线**：`95 reg rev 16`（`enabled=true`，平高 `true`、小趋 `true`，`knowledge-delegation.json` 含 `2` 项能力，`aiops-access-95.service MainPID=109387`）；`41 reg rev 15`（`95 enabled=true`，双租户 `true`；管理页总览呈现 `3 个环境 · 4/6 租户已启用 · 目录 R15`）。
3. **四路正式 HTTPS 并发实测（全过）**：
   - 平高消费端、小趋消费端、平高管家端、小趋管家端 4 路对 `https://api.qumall.qushiyun.com/v1/assistant/questions` 发起正式请求，全部返回 `HTTP 202 → 200 completed`（`4/4` 通过）。

---

## 二、反向 `95 有效会话 → 41` 正式 HTTPS 跨环境隔离（`22 / 22` 全过）

在 `95` 双租户同时激活期间，持平高与小趋真实有效消费端会话及管家端令牌执行反向跨环境隔离核验：

| 核验类别 | 次数 | 实测响应 | 结论 |
|---|---|---|---|
| `95` 平高/小趋有效消费端会话在 `95` 入口跨环境读取 `41` 真实 QA（`GET`） | `2 / 2` | 全部 `HTTP 404`（`QA_NOT_FOUND`） | 通过 |
| `95` 平高/小趋有效消费端会话在 `95` 入口跨环境读取/追问 `41` 真实会话（`GET` + `POST`） | `4 / 4` | 全部 `HTTP 404`（`CONVERSATION_NOT_FOUND`） | 通过 |
| `95` 平高/小趋有效消费端会话直接提交至 `41` 正式 HTTPS 入口（`GET QA` / `GET 会话` / `POST 提问`） | `6 / 6` | 全部 `HTTP 401`（`INVALID_ACCESS_TOKEN`） | 通过 |
| `95` 平高/小趋有效管家令牌直接提交至 `41` 正式 HTTPS 入口（不带 `X-Business-Entry` 负向 + 带 `X-Business-Entry: operator` 正式入口） | `6 / 6` | 4 项 `HTTP 401`（`INVALID_ACCESS_TOKEN`）+ 2 项 `HTTP 403`（`INSUFFICIENT_SCOPE`） | 通过 |
| `95` 平高/小趋有效管家令牌在 `95` 入口跨环境读取 `41` 真实 QA 与会话（`GET`） | `4 / 4` | 全部 `HTTP 404`（`QA_NOT_FOUND` / `CONVERSATION_NOT_FOUND`） | 通过 |
| **合计** | **`22 / 22`** | **反向 `95 → 41` 零跨环境问答与会话正文泄露** | **全过** |

> 结合 [2026-11-26 跨环境隔离回执](2026-11-26-cross-env-isolation-and-multitenant-restore.md) 中的 `41 → 95`（`11/11`）及 `36 kb-service` 双向 `41 ↔ 95` 知识正文检索（`16/16`，含上游 RAGFlow `code=102` 归属权拒绝实证），`41 ↔ 95` 跨环境知识与正式 HTTPS 问答/会话正文双向隔离已完整闭环。

---

## 三、ADR-0347 非 LIFO 独立停用与最终租户基线回滚（Phase B & Phase C）

### 1. Phase B：非 LIFO 先停用平高（`task_8310c23449fbf904d6077efede4965d0`），小趋持续零中断

- **页面操作**：在 `/aiops/environments` 「运行与排查」标签中，跳过后激活的小趋卡片，直接点击先激活的平高卡片「恢复本次开通」。
- **任务收敛**：`state="cancelled"`、`phase="restored"`、`activated=false`、`error=""`。
- **零重启与精细裁剪核验**：
  - `95` `aiops-access-95.service` **`MainPID` 保持 `109387` 不变**（零重启、未中断同环境对端租户）；
  - `95` `environment.json` 推进至 `rev 17`（`enabled=true`，平高 `false`，小趋 `true`）；
  - `41` `registry.json` 推进至 `rev 16`（`95 enabled=true`，平高 `false`，小趋 `true`）；
  - `95` `/etc/aiops-access-95/knowledge/knowledge-delegation.json` 精准裁剪为 `1` 项能力（仅保留小趋 `1783022023241633792`）。
- **正式 HTTPS 实测（`4 / 4` 通过）**：
  - 平高消费端与管家端：均精确拒收 **`HTTP 403`（`INSUFFICIENT_SCOPE`）**；
  - **小趋消费端与管家端：均持续返回 `HTTP 202 → 200 completed`**。

### 2. Phase C：停用最后活跃租户小趋（`task_f7f515cd682f2f270146012b9ee82c34`），双端基线完整回滚

- **页面操作**：点击最后活跃的小趋卡片「恢复本次开通」。
- **任务收敛**：`state="cancelled"`、`phase="restored"`、`activated=false`、`error=""`。
- **目标端与控制端基线回滚核验**：
  - `95` `environment.json` 推进至 `rev 18`（**`enabled=false`**，双租户 `false`，**`active_routes=0`**）；
  - `95` `/etc/aiops-access-95/knowledge/knowledge-delegation.json` 已移除，`production.env` 中 `AIOPS_GATEWAY_KNOWLEDGE_DELEGATION_FILE` 已清除（回到预委派 `P0`），`aiops-access-95.service` 健康运行于 `1.40.4+9c1b35a57d84`（`business_mutations: disabled`）；
  - `41` `registry.json` 推进至 `rev 17`（**`95 enabled=false` 自动收敛**，双租户 `false`）；
  - `41` `/var/lib/aiops-41/onboarding-assets/27e78825/references.json` **精确回滚至预委派 `T0` 摘要 `779253630ce8823797ed488d47b83d3a25537f65672a0a09043b587065632dff`**。
- **正式 HTTPS 停用拒收（`4 / 4` 通过）**：平高与小趋消费端、管家端四路请求全部返回 **`HTTP 403`（`INSUFFICIENT_SCOPE`）**。

---

## 四、运营确认与仍暂时挂起项（诚实边界）

1. **95 历史误回显测试 operator 令牌**：2026-11-26 业务/运维负责人已明确确认该历史测试令牌问题已处理解决，不再作为 95 长期正式开通的前置阻塞项（本轮演练完成后 95 按默认安全态停用，待需要时可随时通过 `/aiops/environments` 正式启用）。
2. **仍暂时挂起的外部与人工项（不越界宣称）**：
   - **业务语义准确率人工签收**：`business_accuracy="unconfirmed"`，待工程师/业务方人工确认；
   - **原生 Android APK 发包**：`#1353`（连续会话）与 `#1422`（Markdown 呈现）仍待公司发包；
   - **同租户第二真实用户跨用户隔离**：缺第二真实用户授权材料，暂挂起；
   - **`124` 环境**：保持 `draft_only` 未动。

> 原始脱敏外私有核验数据（`0600` 权限，`adr0347-live-20261126/{phase-a-dual-active,phase-a-operators,phase-b-non-lifo-pg-disabled,phase-c-final-restored}.private.json`）留存于受控私有运维缓存目录，不入公开仓库。
