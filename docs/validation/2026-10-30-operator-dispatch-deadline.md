# 管家同步派发期限修复

分支`fix/operator-dispatch-deadline`，基线`02d11c3f`。工具声明部署后，原H5本次请求约75秒被标失败，
当时双CPMS证据已齐、模型仍在继续；平台在约82秒报告Gateway未确认完成，run原有效期180秒。
源码SnailDispatcher固定平台60秒/传输75秒，与管家多轮作业期限矛盾。

让管家派发携带已签发run.expires_at；平台等待和传输超时都受剩余期限约束，已过期不出站。
未指定deadline的现有消费者保持60/75秒；不修改run授权期限、终态守卫或放宽完成校验。
新增82秒模拟回归与过期拒绝先2失败，修复后相关128 passed；镜像与临时Gateway通过。
全量/CI/CD与原H5回归待完成，未完成业务验收。错误正文和原订单只保存在私有验收证据。

交互契约核对：现役Snail 1.1.1的OpenApiChatRequest使用long timeout且无60秒上限校验；沿用秒单位和字段名，不改协议。

最终全量3572 passed / 9既有跳过，ruff/format、compileall/pip check、镜像锁和临时Gateway通过。
