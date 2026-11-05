# Runner队列治理实施验证

任务分支docs/runner-queue-design；用户已确认设计并授权实施。
判据Q1–Q8沿用[已确认方案](../architecture/2026-11-05-runner-queue-design.md#5-防看起来完成的验收)。
新增检查拦截误取消开放/外部PR、过期结果推送、分页截断与标签回退；原有Actions检查不覆盖这些行为。
使用现有pytest与assert自检，不引入新测试框架。

已执行：真实API确认专用runner仅aiops-deploy标签、online；原agent实例保留。
生命周期拒绝、实际push入口拒绝、取消API调用与终态、runner状态判定24项相关检查通过。
真实SDK容器运行合成sleep agent后SIGINT，子进程退出且对应容器消失；未调用模型或读取真实业务数据。
工作流actionlint、AFK边界检查通过。最终CI、占槽probe和生产CD待验证。

操作已写入[队列手册](../agents/runner-queue.md)。宿主具体注册记录保留私有，不公开凭据。
20次合并的长期观察未完成；未完成业务验收。

本地完整pytest：4699通过、9跳过、246.72秒；AFK检查、wheel/sdist通过。
该轮基线41d96649；随后合入b89c428e，最终候选以远端CI验证为准。

独立原生Codex会话实测关闭/重开、fork、CD排除、head竞争、API/取消失败与CD归因。
发现重复分页完整性误判与标签大小写误判；已修复唯一ID检查及大小写归一，新增两个最小回归断言。
最终版本1.2.4，远端CI和真实Actions调度结果待交付后记录。
