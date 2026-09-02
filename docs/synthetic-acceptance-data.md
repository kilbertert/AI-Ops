# 标准 API 生产结构仿真验收数据

本目录的数据严格按当前 AI-Ops `FixtureSources` 和健康报告输入字段组织，但全部使用 `SYNTH-*` 标识生成，不包含真实订单、真实租户、真实设备、生产凭据或个人信息。

生成并执行：

```bash
uv run python tools/generate_synthetic_acceptance_data.py
uv run python tools/run_synthetic_acceptance.py
```

覆盖三种场景：

- `SYN-COMPLETE`：400 个时序点，验证完整订单、曲线降采样、极值保留和五维计算输入。
- `SYN-PARTIAL`：订单可访问但无遥测，验证报告部分完成、曲线为空和来源不可用。
- `SYN-FORBIDDEN`：不同租户订单，验证授权查询统一返回 `ORDER_NOT_FOUND`。

该数据集可以作为 #92 的离线系统验收输入，不能替代真实环境验收，也不能用于宣称健康评估或故障诊断准确率。
