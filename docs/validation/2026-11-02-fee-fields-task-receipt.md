# 任务回执：费用字段与订单门槛（2026-11-02）

范围：从 Codex 会话 `01a19b06-39db-7bc3-b875-441fae0caafd` 交接的两条用户反馈，
到现在为止的**开发、合并、发版与验收**。只写实际发生的事。

## 一、原始需求（Codex 会话交接）

用户提供两张截图：

1. 后台「费用信息」栏有启动费用、优惠金额、增值税比例、增值税费用等字段，AI 答复里没有。
2. 同一张单 `2116050516255272961`，说「这笔单的电费…」分析不出来；说「这笔订单」可以。

Codex 已完成复现与规划，被打断在执行验证补丁的阶段。

## 二、交付与合并

| PR | 标题 | 合并提交 |
|---|---|---|
| #1305 | 费用区字段进证据与答复，「订单」二字不再是订单门槛 | `15382378` |
| #1306 | 费用字段与订单门槛合并部署回执 | `099c1207` |
| #1307 | 输出契约收紧 reply 其它行的数字禁令 | `cf42ae46` |

`origin/main` 现为 `cf42ae46`，三个 PR 全部 MERGED，required checks 均 pass。

### #1305 做了什么

- `operator_tools.json` 订单详情证据字段 26 → 35：补 `isPrePay`、`prePayAmount`、
  `dsElectricFee`、`dsServiceFee`、`launchFee`、`vat`、`vatFee`、`payTime`、`occupyFee`。
- `operator_cpms._order_fact_text` 按后台那一栏顺序渲染，缺列不出行、值为 0 照写、
  布尔写「是/否」、付款时间可读化；标签沿用公司 `orderDetail.*` 译文。
- 新增 `common/order_reference.py`：`names_order()` 判「这条消息在指哪张订单」，
  正式入口与原生预览桥共用；判据是形状（15 位以上纯数字）而非词表。
- `README.md`、控制台桥打包清单、部署测试同步。

### #1305 评审中修掉的真实缺陷

Devin Review 提了三条，两条实测成立并在合并前修掉：

1. **日期排在单号前面时查错单** — `帮我分析站点 TEST2026-10-29 这笔单2116…`
   门槛放行后 `_extract_order_no` 从左到右先抓了日期。候选集抽成 `digit_runs()`，两处共用。
2. **英文单号紧贴中文被拆成两张单** — `电费ORDER-1001` 里 `ORDER` 无词边界又截出 `-1001`。
   中英正则分开写：中文允许紧贴，英文必须有分隔符。

第三条（Gateway 路由变更需真跑服务）按 AGENTS.md 补了 `verify-aiops-gateway`。

### #1307 做了什么

线上实测发现的**新问题**：同一句原话 6 次里 2 次整轮失败，`reason=unsupported_fact`。
根因是 #1305 把费用字段枚举写进了「系统会在这里显示…」，与紧随其后的「不要复述」打架，
模型在事实段外写数字被 `check_notice` 拒。改法是把禁令写死并提高优先级，**不放宽校验**。

## 三、发版（实际执行）

| 制品 | 身份 | 状态 |
|---|---|---|
| Gateway（41，CD `41422309905`） | `1.0.0+cf42ae46edd0` | 已部署 |
| 节点声明 | `ad96701057ea…` 仓库/宿主/容器三处一致 | 已生效，心跳 age≈11s |
| 控制台桥（36） | 修订 `15382378`，`order_reference.py` `baf83624…` | 已安装，active |

顺序是**节点声明先于网关**：证据字段两端精确比对，任何「新网关 + 旧声明」窗口都会让
订单诊断全部 `OPERATOR_EVIDENCE_MISMATCH`。声明是独立挂载，换它只需定点重建容器，不重建 jar。

## 四、真实账号端到端验收（已执行）

用库外加密清单的 `aiops-ulink-account-20261030` 登录公司 UPMS 拿**公司签发**令牌
（口令须 AES、令牌在响应顶层），直打 `172.18.0.1:8788` 带 `X-Business-Entry: operator`
与 `X-AIOps-Source-Key`。

**通过的部分**：

```
帮我分析这笔单的电费2116050516255272961 → 202 → completed
答复正文包含：是否预付费 是 / 预付费 10.0 / 电费 5.13 / 服务费 1.71
启动费用 1.0 / 优惠金额 0.0 / 增值税比例 2.0 / 增值税费用 0.16 / 合计 7.84 …
agent_runs: order_no=2116050516255272961, state=succeeded,
            chat_binding=operator_order_diagnosis:production-41:1942105476598861824,
            exec_config_state=matched
```

评审回归用例 `帮我分析站点 TEST2026-10-29 这笔单2116…` 的 `order_no` 也等于真实单号
（不是日期），确认缺陷 1 已修。

**未通过的部分**见下节。

## 五、必须如实说明的三件事

### 1. #1307 的修复**没有生效**

运行用的清单文本来自 `agent_versions` 快照里的 `node_manifest`（`chat_execution.py:435`），
是**发布时**冻结的，不是运行时按新代码重算。实测最新 run：

```
new_text: False | old_text: True
```

网关代码已是新版本，但线上跑的还是旧契约。

### 2. 因此稳定性**没有改善，仍然差**

部署后连跑 6 次：**2 completed / 4 failed**。基线（旧契约）是 4/6。
这个数字不能用来证明回归，因为两边跑的**都是同一份旧契约**——它只说明
「这句原话在当前生产上大约 1/3～2/3 成功率」这个区间本身很宽。

最近 3 小时的拒绝原因：`unsupported_fact` 8 次、`json_parse` 1 次。

### 3. 重发布路径受阻（未解决）

要让新契约生效必须重发布，两条路都走不通：

- `aiops-admin pull-dify` / `sync-from-dify` → 该租户在环境 `production-41` 里没有
  Dify workspace 登记；
- `POST /v1/operator/publish` → 只接受 `dify_app_registry` 里的绑定，节点 app 不在其中。

这属于**既有的部署缺口**，不是本次改动引入，也没有在本次解决。

## 六、验证与文档

- 全量 `uv run pytest`：3895 passed / 9 skipped（#1305 时）；#1307 为 625 passed。
- `ruff check` / `ruff format --check`、`node .sandcastle/adr.check.mjs`：通过。
- 镜像按 `local-gateway-build` 真构建并 relock（`7d141e5d…`）。
- 新增文档：`docs/adr/0193-fee-fields-and-order-reference.md`、
  `docs/agents/operator-e2e-real-account.md`（真实账号端到端方法与工具，本轮沉淀）、
  `tools/verification/operator_e2e.py`、
  两份 `docs/开发进度/` + 两份 `docs/validation/`。

## 七、结论

**能交付的**：费用字段进答复、「这笔单」不再落进无订单回合——两条用户反馈在真实账号上
验证跑通；三个制品已发版且身份一致。

**不能声称的**：稳定性已修好。#1307 的措辞改动已在 main 上，但线上未生效；
成功率仍是 43%～67% 量级，且失败的还是同一类原因。

**下一步需要决定**：如何让节点清单重新发布（补 workspace 登记 / 提供不依赖 Dify 的
重发布路径 / 其它），在决定之前不把 #1307 算作已交付效果。
