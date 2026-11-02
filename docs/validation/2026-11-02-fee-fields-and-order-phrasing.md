# 费用字段与订单门槛修复的验证记录（2026-11-02）

分支 `fix/fee-fields-and-order-phrasing`，基线 `8bffe408`（`origin/main`）。
决策与取舍见 [ADR-0193](../adr/0193-fee-fields-and-order-reference.md)。

> **本记录只覆盖合成 CPMS 响应与本地全链，未完成业务验收。**
> 真实订单上这些字段是否真有值、单位与币种是否与后台一致，仍未验证。

## 缺陷一：后台费用字段不进答复

合成一行含截图那一栏全部金额的 `chOrderInfoModel`，走
`project_evidence → render_report`（**不是手写证据**）：

```
启动费用：1.00   优惠金额：0       增值税比例：2     增值税费用：0.16
预付费：10.00    电损电费：0.20    电损服务费：0.10  占位费：0.50
是否预付费：是   系统记录的付款时间：2026-10-30 14:12:13
```

- 补字段前：`launchFee` 进不了证据（`project_evidence` 只挑声明字段），
  `启动费用/优惠金额` 在正文里查无此词 —— 与截图一致。
- 补字段后：上面八项都在。

**变异验证**：从 `operator_tools.json` 的 `evidence_fields` 删掉
`launchFee/marketAmount/vat/vatFee`，`test_every_backend_fee_field_reaches_the_answer`
变红。第一次写的版本手写证据，删字段时**仍然全绿** —— 夹具绕过了被修的那一层，
改成走 `project_evidence` 之后才有意义。这一点记在用例注释里。

## 缺陷二：「这笔单」落进无订单回合

```
帮我分析这笔单的电费2116050516255272961      →  ''
帮我分析这笔订单的电费2116050516255272961    →  '2116050516255272961'
```

（`_native_order`，修复前实测）

修复后在**真实入口** `POST /v1/assistant/questions` 上跑原话：
签发的是订单诊断（`/chOrderInfo/getOrderInfoDetail/ORDER-1001` 被读），
不是无订单回合。预览桥的参数化用例补了四条原话变体。

**变异验证**：把 `names_order` 的纯数字分支改成 `False`，网关用例与预览桥四条
参数化用例同时变红。

### 一个写测试才发现的坑

合成单号 `ORDER-1001` 紧贴中文时，`\border\b` **匹配不到** —— Python 的 `\w` 包含中文，
「费ORDER-1001」里 `r` 与「费」之间没有词边界。判据因此改写成
`(?<![A-Za-z])order(?![A-Za-z])`。这条没有被真实反馈覆盖到（真实单号是纯数字，
19 位数字段本身就能成立），但它是同一句话在英文单号下的形态。

## 守卫仍未删：站点名与时刻不算订单

```
帮我查询充电站 TEST2026-10-29 03:00:09   →  names_order=False
帮我找一下广州天河附近的充电站            →  names_order=False
我的手机号是13800001111                   →  names_order=False
```

`test_site_name_with_date_is_not_an_order` 与预览桥的 `TEST2026-10-29` 一例都仍在跑。

## 命令与结果

```
uv run pytest tests/operations tests/common tests/gateway tests/dify \
  tests/execution tests/test_console_bridge_deployment.py \
  tests/test_repository_layout.py tests/test_readme_architecture.py
# → 全绿

uv run pytest                       # → 3890 passed, 9 skipped
uv run ruff format --check …        # → 11 files already formatted
uv run ruff check …                 # → All checks passed!
node .sandcastle/adr.check.mjs      # → adr check ok
```

### 镜像上下文摘要：我先判错了一次，这里订正

首次全量跑时 `test_gateway_context_digest_is_reproducible_and_matches_the_lock` 失败
（摘要 `9c3419f2…` ≠ 锁值 `1f910bbb…`）。我当时用 `git stash` 在干净 `origin/main` 上
复现了同样的红，判定「既有状态，与本次无关」。

**那个判断是错的。** `git stash` 不回收**未跟踪**文件，而本次新增的
`src/aiops_diagnostics/common/order_reference.py` 正是未跟踪文件 —— 所谓「干净基线」
里仍然带着它，摘要当然一样。正确的核对方式有两个，都指向同一结论：

- `deploy/m2-live-03/context_digest.py --check` 在 `origin/main` 上 **match: true**；
  在本分支上 `files` 从 198 变 199、摘要 `1f910bbb…` → `a6c2d22e…`。
- main 上最近合并的 PR #1304 的 `verify` 是 **pass**。

所以这是**本次改动引入的预期变化**，不是既有失败。按
`docs/agents/local-gateway-build.md` 真构建镜像并 `--update-lock`：

```
docker pull python@sha256:70729b46…           # 锁里声明的 base
M2LIVE_GATEWAY_IMAGE=aiops-m2-live-03-gateway:feephasing \
M2LIVE_DIST=$PWD/dist/verification deploy/m2-live-03/build-image.sh --update-lock
# source_sha256 = a6c2d22e49c47ef86b53aeacb32ea0826934c2df6a1aecf3e0da8d790109431e

python deploy/m2-live-03/context_digest.py --check      # → match: true, files: 199
docker run --rm --network none --read-only --entrypoint python \
  aiops-m2-live-03-gateway:feephasing -c \
  'from aiops_diagnostics.gateway.gateway_api import create_gateway_app; \
   from aiops_diagnostics.common.order_reference import names_order; \
   print("Gateway import: passed", names_order("帮我分析这笔单的电费2116050516255272961"))'
# → Gateway import: passed True
```

镜像里跑出来的 `True` 是这次构建真的带上了新模块的证据，不只是摘要对上了。

## 评审抓到的两个真缺陷（已修，附复现）

首轮 CI 全绿、单测全绿、镜像可导入，但 Devin Review 提了三条，其中两条**实测成立**：

### 1. 日期排在单号前面时查错单

```
帮我分析站点 TEST2026-10-29 这笔单2116050516255272961
  门槛 names_order → True
  提取 _extract_order_no → 'TEST2026-10-29'      ← 查的是日期，不是订单
```

根因是**门槛与提取看的不是同一组候选**：门槛只问「句中有没有 15 位以上数字段」，
提取器却从左到右取第一个含数字的 token。修法是把候选集定成一个函数
`order_reference.digit_runs()`，两个入口都用它；纯数字段唯一时它就是这一轮的单号。

修后：网关取 `2116050516255272961`；`帮我查询充电站 TEST2026-10-29 03:00:09` 仍为空。

### 2. 英文单号紧贴中文时预览桥把一张单算成两张

```
帮我分析这笔单的电费ORDER-1001
  → REJECT「请在本条消息中填写一个明确订单号；暂不支持“上一个订单”」
```

显式提取的 `order` 没有词边界，从 `ORDER-1001` 的 `ORDER` 又截出一个 `-1001`；
候选扫描另得 `ORDER-1001`，并集长度 2 ⇒ 判定「说了两张单」。

修法不是加边界，而是**中英分开写**：中文允许紧贴（`订单2116…` 是原生写法），
英文必须有分隔符（`order 123` / `order no: 123`）。只加 `(?<![A-Za-z])` 会连
`订单号:…` 也匹配不到 —— 那条的中文前缀紧贴在 `order` 之前。

两个缺陷各补了用例：预览桥的参数化表加了这两条原话与一条 `order no:` 写法；
`tests/common/test_order_reference.py` 直接断言 `digit_runs` 是两处共用的候选集。

**为什么它们没被单测发现**：我的用例都拿「日期不在句子里」和「单号是纯数字」的输入，
评审的两条恰好是两者的交叉。这说明「补了字段/换了判据」不等于「所有既有输入都还成立」。

## Devin 的第三条：Gateway 本地服务验证（已补）

`names_order` 改变了 `/v1/assistant/questions` 的管家路由，按 AGENTS.md 只用 pytest
不够。按 `.claude/skills/verify-aiops-gateway/SKILL.md` 在一次性数据根真跑服务：

```
uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py --out dist/verification/gateway
  /health -> 200        /v1/assistant/questions -> 401
  /v1/agents -> 401     /v1/openapi.json -> 200
  /v1/runs,/v1/enroll,/v1/standard/diagnoses,/v1/dify/tools/order_snapshot -> 404
  all checks passed
```

**该 skill 的边界照实说**：它证明启动、鉴权面与退役路由，**不**证明管家订单诊断整链、
真实 CPMS 或模型行为 —— 那些仍依赖远端，本轮未覆盖。

## 未做

- 真实订单验收：这些字段是否真有值、币种与单位，需按
  `docs/agents/operator-cpms/README.md` 在真实账号与订单上复核。
- 部署：需要 Gateway、Java 节点 jar、控制台桥三者配套发版。**只更新其中一两个会让
  `OPERATOR_EVIDENCE_MISMATCH`**（证据字段集合两端精确相等比对）。
