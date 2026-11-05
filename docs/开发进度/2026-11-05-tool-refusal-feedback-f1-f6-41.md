# 工具拒绝回喂 F1–F6 部署验收（41）

目标：新网关 `1.1.1+097c8ae9037a` 与新节点 `097c8ae9037a-2811531eac88` 上 41 后，
执行 `docs/acceptance/tool-refusal-feedback-qa-plan.md` 的 F1–F6 部署验收矩阵。
本次只跑验收、不改代码。

结果：F1/F2/F3/F5/F6 通过；F4 部分达成——运行失败与不静默成功在
restore/retract 两种中途触发下都成立，retract 路径 `run.reason` 确定性落
`retracted_by_operator`；换版路径下 `tool_refused:` 因失败上报与拒绝判定共用
`_current` 闸而落不了 `run.reason`，记为机制性限制并提出改进建议（详见同日
验收记录）。

已知缺口：闸上授权拒绝的原始码归因只能翻节点容器日志；显式建会话再发问会
404 `CONVERSATION_NOT_FOUND`（未深挖）；轻量问题不取数直接作答属既有噪声地板。
