# 失败行记录具体判据，便于事后分辨成因

**验证时的生产版本**：Gateway `1.0.0+9e456e918181`（本改动尚未部署）。

## 目标
2026-11-01 排查订单链路时，`chat_dify_runs` 只有 `reason='CHATFLOW_ANSWER_INVALID'`。
该码同时覆盖"片段外复述状态""片段缺失""字段不符"等多种情形，
**无法判断是提示词不明确还是守卫过严**——这正是本轮卡住的那一步。

## 交付
- 新增 `chat_dify_runs.reject_detail` 列（迁移沿用既有 `ALTER TABLE` 模式，默认 NULL）；
- `fail(run_id, reason, detail=None)` 写入；
- 两条 Dify 失败路径都传 detail：`consumer_chat_service`（受管 consumer 路径）
  与 `gateway_runtime._settle_dify_failed`（订单链路实际走的路径）；
- `reason` **语义不变**，仍为稳定错误码，调用方分支逻辑与三处精确断言均未受影响。

## 验证
全量 **3830 passed / 9 skipped / 0 failed**；`ruff` 与内容摘要通过；
新增回归 `test_failure_reason_records_the_specific_judgement_not_only_the_code`
（变异验证：撤掉改动即红）。

## 未完成
- **尚未部署**，因此还没有一条带 `reject_detail` 的真实订单失败记录；
- 部署后需重跑一次订单查询，才能读出是哪条判据触发；
- 固定 32 次样本批次仍未执行。

**业务验收未完成。** 详见 [validation](../validation/2026-11-01-reject-detail.md)。
