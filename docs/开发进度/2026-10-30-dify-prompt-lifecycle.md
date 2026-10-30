# 两端提示词的表达由页面控制，事实仍由系统给

日期：2026-10-30
分支：`feat/dify-prompt-lifecycle`（未合并）
契约：[docs/acceptance/dify-prompt-lifecycle](../../acceptance/dify-prompt-lifecycle/acceptance.feature)

## 里程碑目标

产品要的杠杆只有一句话：**运营在两个指定提示词框里写自然语言，就能改两端的表达**。
2026-10-30 实测主线 `4af09893` 的状态是：客户端 realtime 的 `reply` 已经能写正文
（#1216/#1218），管家 `pre_prompt` 已经进执行清单，但**事实判据太弱**、**澄清与无结果的正文
仍是固定文案**、**找站的正文被模板覆盖**、**迁移会吞掉运营的编辑**。这一片补这四件事。

## 已完成

### 1. 客户端事实判据从"数字集合"换成"数字挂在哪个标签上"

新模块 `consumer/consumer_facts.py`，四条判据：

| 判据 | 拦的是什么 |
|---|---|
| 金额标签绑定 | `合计 20，付款 27.36` —— 两个值都真，标签互换 |
| 货币断言必须来自快照 | `每小时 3 元` —— 曾因"0–10 小计数豁免"通过 |
| 不承诺到账/退款/扣款 | `已到账`、`已退款成功`；`没有退款记录` 仍允许 |
| 状态方向 | 快照说结束不能写"未结束"；说充电中不能写"已结束" |

另：金额为 0 时，"没有退款记录"与"已退 0"等价（`covers_zero`），不逼模型说一句不自然的话。
订单号先从正文里等长掩码再找金额标签的近邻数字 —— 否则 `订单号 ORDER-A-1，账上记的支付`
里的 `1` 会被绑到"支付"上。

### 2. 澄清／无结果／转人工的正文交回提示词

`clarify`／`not_found`／`human` 可以带 `reply`，判据比 realtime 更硬：**一个数字都不许出现**，
不许写链接，不许出现"已提交/已退款"类执行声明；客服入口行仍由内容包给、系统追加。
`unavailable`（服务故障）与 safety／photo_unreadable／followup 保持系统输出：模型没有故障
信息，让它描述故障只会多一个错处。

### 3. 管家找站：先取数，再写说明，事实由系统列

节点调用 `cpms_site_search` 取得当前账号可见的真实结果后可以写一两句说明；**说明里不许有
数字、价格、距离、空闲状态，也不许重抄结果里的站名地址**。名单、数量、截断范围、无定位限制
与来源全部由 `render()` 按证据写出。没有调用工具就写了说明 → 整轮失败
（`OPERATOR_SITE_NOTE_WITHOUT_EVIDENCE`），不再静默丢弃。

### 4. 迁移不再吞掉运营的提示词

`tools/content/upgrade_consumer_flow.py --live-documents` 三种分支：SYSTEM 为空 → 初始化；
与模板逐字相同 → 幂等；不同 → **拒绝**并说明 `--replace-system`。拒绝发生在任何其他改写之前，
不会出现"环境变量改了、提示词没改"的半迁移。

### 5. 验收与证据

- `tools/verification/verify_prompt_lifecycle.py`：23 项（16 格 + 4 条负例 + 3 条版本判据），
  无人值守可复跑，默认写临时目录。
- 新增测试：`tests/consumer/test_consumer_prompt_facts.py`（23）、
  `tests/consumer/test_consumer_prompt_migration.py`（5）、
  `tests/consumer/test_prompt_lifecycle_grid.py`（20）、
  `tests/execution/test_prompt_version_lifecycle.py`（3）。
- 变异验证：删掉标签绑定／货币守卫／承诺守卫／状态守卫／无证据拒绝／迁移保护，对应用例逐条转红，
  恢复后转绿（见 [validation](../../validation/2026-10-30-dify-prompt-lifecycle.md)）。

## 改动文件

| 文件 | 作用 |
|---|---|
| `src/aiops_diagnostics/consumer/consumer_facts.py` | 新增：四条事实判据 |
| `src/aiops_diagnostics/consumer/consumer_reply.py` | 接入判据、货币豁免、`validate_notice` |
| `src/aiops_diagnostics/consumer/consumer_knowledge.py` | 澄清/无结果/转人工的正文由提示词写 |
| `src/aiops_diagnostics/consumer/consumer_documents.py` | 提示词模板补 `reply` 与事实规则 |
| `src/aiops_diagnostics/operator/operator_conversation.py` | 找站说明的判据与渲染、提示词 |
| `src/aiops_diagnostics/operator/operator_cpms.py` | 无证据的找站说明拒绝 |
| `tools/content/upgrade_consumer_flow.py` | 迁移保留运营编辑 |
| `deploy/dify-chat/consumer-service.dsl.yml` | 仓库副本提示词同步 |
| `deploy/m2-live-03/images.lock.json` | 源码变了，上下文摘要跟着变（否则复核对不上） |
| `README.md` | 新模块进分层表 |

## 已知缺口

- **未在真实环境验收**：41 上的 Dify 应用、Java 节点、真实模型、原 H5 都没有跑。候选 PR
  不改正式环境，真实验收由 Codex 在合并制品后接续，步骤见 qa-plan §五。
- **找站说明依赖模型先调工具**：真实模型是否稳定"先取数再写说明"只能在 41 上验。没调工具
  且没写说明时仍走系统模板（可用），调了工具才由提示词控风格。
- **不证明业务准确率**：四条判据拦的是可机械判定的错话；一句每个值都挂在正确标签上、
  但因果关系说反的话仍会通过。
- 客户端 `unavailable` 时提示词写的正文会被忽略（有意），因此"服务故障时改提示词"没有着力点。

## 下一步

1. Codex 复核本 PR；合并后按 `ops/dify-console-bridge/README.md` 同版安装桥（本片未改桥）。
2. 在 41 上跑 qa-plan §五的页面级 16 格与两端版本闭环，证据进私有目录。
3. 页面验收通过后，才可以把"业务验收通过"写进交付状态。
