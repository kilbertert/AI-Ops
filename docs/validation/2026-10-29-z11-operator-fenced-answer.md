# 管家诊断间歇失败的确定性根因与修复

2026-10-29，域目录迁移（#1159，`dc9e3eac`）部署后真实验收中复现。
基线 `dc9e3eacc4c5f213a3d9fbb4618164c38f150687`。

## 症状

CD 成功后管家原 H5 提交订单诊断，QA 终态 `OPERATOR_DIAGNOSIS_FAILED`。
网关固定原因日志（#1142 上线）首次给出确定性分类：

```
operator_answer_rejected reason=json_parse
operator_completion_refused code=OPERATOR_INVALID_ANSWER
```

`agent_runs` 中该 run `state=failed`、`observed_agent_version` 为空，双 CPMS 证据均已落库。

## 根因

对 41 上真实供应商（`deepseek-v4-flash`，经 `model-proxy`）用同一 SOP 与工具历史复现，
**同一请求三次**（流式 + 两条工具结果，`response_format=json_object`）：

| 次序 | finish | 是否合法 JSON | 首尾 |
| --- | --- | --- | --- |
| 1 | stop | **否** | `` ```json `` … `` ``` `` |
| 2 | stop | 是 | `{"summary":"订单 21157` … |
| 3 | stop | 是 | `{"summary":"订单211575` … |

供应商**间歇**返回 Markdown 代码围栏包裹的 JSON，围栏内仍是唯一、完整的报告对象。
网关此前只做 `json.loads(raw)`，于是把一次合规回答判成 `json_parse` 失败。
这解释了此前的形态：间歇、只在带工具的订单诊断上出现（会话类回答不走该解析），
重试有时成功、且成功时不留任何拒绝日志。

## 修复

`operator_cpms.parse_answer`：裸 JSON 直接解析；否则只接受**恰好一层**围栏
（```` ``` ```` 或 ```` ```json ````）且围栏内是单个 JSON 对象。散文、多份 JSON、围栏外有其他
文字、残缺对象仍按原样拒绝。放宽的是供应商的装饰，不是输出契约。

## 证据

- 先红后绿：9 项参数化用例（4 接受 / 5 拒绝）修复后通过，未修改任何拒绝分支。
- 管家专项 `tests/test_operator_cpms.py` + `tests/test_operator_conversation.py`：**73 passed**。
- ruff check / format 通过。

## 未完成

尚未合并、部署，也未在 41 上重跑真实订单验收。根因证据来自真实供应商的受控复现，
但线上修复效果需部署后由原 H5 入口确认。
