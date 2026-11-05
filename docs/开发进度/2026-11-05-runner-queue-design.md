# 队列治理设计

目标：消除agent长任务阻塞部署，以及PR已结束任务仍排队/运行。
分支`docs/runner-queue-design`，基线`6e8a05fe`。

已完成：读取runner/运行记录和工作流，核验标签匹配，整理官方平台约束；
提出专用部署槽、轻任务分流、PR生命周期回收与可观测性方案。
详见[设计](../architecture/2026-11-05-runner-queue-design.md)和[验证](../validation/2026-11-05-runner-queue-design.md)。

未实施：在线runner注册、工作流修改、自动取消与并发验收。下一步按设计Q1–Q8执行。
本轮是方案设计，不宣称容量提升或业务验收。
