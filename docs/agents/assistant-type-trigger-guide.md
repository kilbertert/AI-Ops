# 智能问答三类 type 触发与联调指南（前端专用）

> 日期：2026-09-08。历史 95 环境入口为 `https://api.qumall.qushiyun.com`；当前 41 环境
> 请使用 `https://api.mall.qushiyun.com`，两者不得混用会话或数据源。
> 本文档所有示例问题均已在生产环境实测，触发结果与标注一致。
> 背景 issue：#150（智能问答统一入口）。

## 0. 一页速览

```text
POST /v1/assistant/questions          ← 创建唯一入口（三类全走这里）
body: {"question": "...", "order_no": "可选"}

响应自带 type 字段,前端只需一次 switch:
  type=faq       → 200,answer 直接渲染,流程结束
  type=qa        → 202,拿 qa_id,       轮询 GET /v1/assistant/questions/{qa_id}
  type=diagnosis → 202,拿 diagnosis_id,轮询 GET /v1/standard/diagnoses/{diagnosis_id}
```

## 1. 三类 type 怎么触发（实测过的示例问题）

### type=faq —— 提问命中固定问答目录（同步 200）

触发特征：问的是常见业务问题，与 FAQ 目录 28 条中的某条高度相关。

| 示例问题（可直接复制测试） | 实测结果 |
|---|---|
| `充电结束后拔不出充电枪怎么办` | 200 type=faq，question_id=`consumer.faq.q010` |
| `充电费用是怎么计算的？什么是尖峰平谷分时电价？` | 200 type=faq，question_id=`consumer.faq.q015` |
| `什么是"即插即充"？如何开通与使用？` | 200 type=faq，question_id=`consumer.faq.q008` |

**不要用这些测 faq**：带订单号的问题会优先走 diagnosis（即使文字也像 faq）。

### type=qa —— 自由提问，不在 FAQ 目录（异步 202）

触发特征：一般性新能源/用车问题，目录里没有对应条目，也没带订单号。

| 示例问题（可直接复制测试） | 实测结果 |
|---|---|
| `磷酸铁锂电池平时怎么保养充电才比较延长寿命` | 202 type=qa |
| `新能源汽车开久了动力电池衰减到什么程度需要更换` | 202 type=qa |
| `家用充电桩安装需要向物业申请什么手续` | 202 type=qa |
| `电动车长期不开的话电池应该保持多少电量存放` | 202 type=qa |

轮询：`GET /v1/assistant/questions/{qa_id}`，按 `retry_after_ms`（1s）节流，终态 `completed` 读 `result.text`（真实模型回答，通常 30–90 秒）。

### type=diagnosis —— 带订单号的提问（异步 202）

触发方式有 **两种**，都要求订单属于**当前登录账号**：

1. **显式传参**：body 带 `order_no`
   ```json
   {"question": "这个订单为什么充电提前结束了？", "order_no": "<本账号自己的订单号>"}
   ```
2. **文本内嵌**：订单号写在问题文字里，后端自动提取并校验归属
   ```json
   {"question": "订单 <订单号> 为什么充电突然停了"}
   ```

实测（显式传参，本账号订单）：202 type=diagnosis。

轮询：`GET /v1/standard/diagnoses/{diagnosis_id}`，终态 `completed`/`inconclusive` 读 `result`（summary/root_cause/…）。

> ⚠️ 两个注意：
> - 订单号必须是**当前 thirdSession 账号自己的**，否则显式传参返回 404 `ORDER_NOT_FOUND`（不泄露），文本内嵌则回落 qa。
> - **2026-09-21 前**模型配额受限（glm-ark 月配额 9-21 重置），带订单诊断可能终态 `failed` + `error.code=DIAGNOSIS_BLOCKED`——这是模型限制不是链路故障，前端提示"诊断暂不可用，稍后重试"即可。

## 2. 完整联调 curl（可直接跑）

```bash
# 公共头（三个场景通用）
H1='Content-Type: application/json'
H2='tenant-id: 2019588094906601472'
H3='third-session: <当前登录的thirdSession>'
H4='X-Business-Entry: consumer'

# 场景1 faq（同步）
curl -i "https://api.qumall.qushiyun.com/v1/assistant/questions" \
  -H "$H1" -H "$H2" -H "$H3" -H "$H4" \
  -d '{"question":"充电结束后拔不出充电枪怎么办"}'
# 期望: 200 {"type":"faq","answer":"..."}

# 场景2 qa（创建+轮询）
curl -i "https://api.qumall.qushiyun.com/v1/assistant/questions" \
  -H "$H1" -H "$H2" -H "$H3" -H "$H4" \
  -d '{"question":"磷酸铁锂电池平时怎么保养充电才比较延长寿命"}'
# 期望: 202 {"type":"qa","qa_id":"qa_..."}
curl -i "https://api.qumall.qushiyun.com/v1/assistant/questions/<qa_id>" \
  -H "$H2" -H "$H3" -H "$H4"
# 期望: {"status":"completed","result":{"text":"...","reminder":true}}

# 场景3 diagnosis（创建+轮询）
curl -i "https://api.qumall.qushiyun.com/v1/assistant/questions" \
  -H "$H1" -H "$H2" -H "$H3" -H "$H4" \
  -d '{"question":"这个订单为什么充电提前结束了","order_no":"<本账号订单号>"}'
# 期望: 202 {"type":"diagnosis","diagnosis_id":"dx_..."}
curl -i "https://api.qumall.qushiyun.com/v1/standard/diagnoses/<diagnosis_id>" \
  -H "$H2" -H "$H3" -H "$H4"
# 期望: {"status":"completed|inconclusive","result":{...}}
```

## 3. 前端参考实现（伪代码）

```javascript
const res = await post('/v1/assistant/questions',
    { question, ...(orderNo ? { order_no: orderNo } : {}) });

switch (res.type) {
  case 'faq':
    renderAnswer(res.answer);            // 同步,流程结束
    break;
  case 'qa':
    result = await poll(`/v1/assistant/questions/${res.qa_id}`,
                        () => 1000 /* retry_after_ms */);
    renderAnswer(result.text);           // completed 时
    break;
  case 'diagnosis':
    result = await poll(`/v1/standard/diagnoses/${res.diagnosis_id}`,
                        () => 1000);
    renderDiagnosis(result);             // completed/inconclusive 时
    break;
}
```

## 4. 常见坑（实测踩过）

| 现象 | 原因 | 处理 |
|---|---|---|
| 轮询报 `diagnosis not found` | 用 qa_id 去轮询了 diagnoses 端点 | 按 type 分派轮询端点；现在错轮询的报错 message 会直接指路正确路径 |
| 401 `INVALID_ACCESS_TOKEN` | thirdSession 过期 | 换当前登录的新会话 |
| 404 `ORDER_NOT_FOUND`（显式带单） | 订单不属于当前账号 | 换本账号自己的订单 |
| 422 `INVALID_REQUEST` | body 多了未知字段（如 platform）或缺 question | 严格按本文档字段发 |
| diagnosis 终态 `failed DIAGNOSIS_BLOCKED` | 模型配额/能力受限（9-21 前） | 前端提示"稍后重试"，勿当业务失败 |
| qa 终态 `failed QA_FAILED` | 模型侧错误 | 稍后重试 |

## 5. 历史接口（分开展示用）

- 通用问答历史：`GET /v1/assistant/questions?limit=50` → `{type:"qa_list",questions:[...]}`
- 订单诊断历史：`GET /v1/standard/diagnoses?limit=50`

两个历史按调用者隔离，"我的问答"与"我的诊断"分开展示。
