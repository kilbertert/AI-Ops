# 252. 管家答复语义复核暂降为观察，不再拦截交付

Status: Accepted（2026-11-12用户明确要求先让诊断能够成功且顺利运行，本阶段不以复核结论拦截交付）。

管家诊断在「生成分析→复核→交付」一段仍按一处不合格整段拒收：实测拒因包括
unsupported_claim（事实一致性复核未通过）、unsupported_station（答复含来源未提供的
站名，且已知会把充电枪编号 qushi0902-1 误判为站名）、review_uncertain 与
review_unavailable/quota/auth（复核服务自身不可用同样阻断）。用户要求本阶段先放下
这些会让诊断报错的校验。

决定：operator_answer_review.review 的 verdict 降为观察信号，不再一票否决交付。
完成回调里 verdict 仍照常计算并写入快照 answer_review、轨迹明细与行级日志
review_reason，供后续评估与恢复拦截；但完成路径按「有没有答复」而不是「复核是否放行」
决定 delivery_mode——有答复即 diagnosis 交付，只有模型根本没交出答复（节点上报
model_failure）时才退回已授权证据（evidence_only）或在无证据时维持
OPERATOR_ANSWER_NOT_GROUNDED。复核 step 的轨迹 status 只在复核自身没跑出结论
（unavailable）时记 refused；allow/reject/uncertain 都算已评估。

不变边界：凭据/秘密字段拦截（contains_secret）、订单与站点授权复核、证据来源与时间
完整性（EVIDENCE_MISSING/EVIDENCE_MISMATCH）、工具参数与契约、取数前置、scope/grant
与历史快照授权全部照旧——这些都是权限与数据完整性闸，不是本阶段要放下的语义质量
校验。模型自身失败（quota/stream/failed）仍按既有 NODE_FAILURE_CODES 归因，不伪装成交付。

终局闸拒的归因不变形：复核后的授权/范围闸拒（订单站点变动、scope 变化、历史快照
失权）是终局拒绝，不是「没交出答复」——同一 run 重派必撞同一道闸。这批拒绝统一纳入
既有 stamp 约定（原码写 run.reason、答复吸收进终局快照不投递），并在任务上留
completion_refusal 兜底：闸拒意外未落码时 worker 直接归原码，不按
no_answer_accepted 重采样空耗一轮再误报 OPERATOR_MODEL_OUTPUT_UNSTABLE。复核自身
抛错（含 settings/provider 缺属性的 AttributeError）一律收敛为 unavailable 观察，
不得穿出拦截交付。

## 备选方案

- 直接删除复核调用：失去「这条答复按旧判据会被拦」的对照数据，恢复拦截时没有评估依据。
- 加配置开关双路径：本轮是明确的临时降档，不新增无人要求的长期模式与配置分叉；
  恢复拦截只需把交付闸改回 verdict 判据。
- 顺带修 qushi0902-1 站名误判：观察模式下误判只影响记录的原因码，修复留待恢复拦截时再评。

风险提示：复核不再拦截后，编造操作、无依据金额断言等答复会直接交付用户——这是用户
明确接受的本阶段取舍；复审记录保留原样，不代表答复被认可。
