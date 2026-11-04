# #1345自由提问故障与恢复证据

基线ac5e2fea，分支fix/consumer-free-question。模型依赖故障、故障表达和回答质量分别判定。

## 已知根因与证据边界

原16:53:03北京时间的Android请求503，只留下CONSUMER_INTENT_UNAVAILABLE(RuntimeError)，
未创建QA。代码把所有HTTP/网络/解码错误映射为同一个RuntimeError，又只记录异常类型；
因此原请求的精确HTTP错误已无法从该日志恢复。不能把另一管家请求的403冒充本请求证据。
用户确认失败后已充值或调整模型凭据；当前相同服务配置和身份、原句独立分类两次均为
knowledge（1.81秒、1.67秒）。配置文件时间早于故障，但这不证明外部余额/密钥未变化。

确定性回放命令：`uv run pytest tests/consumer/test_consumer_model_failure.py -o addopts='' -q -x`。
修复前真实HTTP返回明确额度码，Gateway却回CONSUMER_INTENT_UNAVAILABLE（1 failed）；
修复后保留额度码、retryable=false、受限日志，不再把服务失败说成无法理解用户。
补充普通403、429限流、500、故障后同句恢复及草稿结果回读；没有依赖生产欠费状态来做红测。

## 真实安卓恢复（修复部署前）

现装消费者APK1.2.15/code24，Android12；用户已有登录，直接在客服聊天页使用adbx视觉定位输入。
生产1.0.19+ac5e2fea715a，未安装APK、重登或修改配置。

| 输入 | UTC | 结果 |
|---|---|---|
| How to charge safely | 10:19:16–10:19:25 | QA completed、run succeeded；截图可见充电安全操作正文 |
| What about using the air conditioner | 10:22:25–10:22:31 | completed；正文明确当前资料无相关说明 |

两次服务端conversation_id不同：不能据同页两气泡宣称上下文关联通过。
后一句的知识缺失也不能记为知识质量通过。原始截图/XML/QA对照保留私有证据。

## 检查与剩余

最终定向165项通过。提交前全量4136通过/9跳过/1失败：部署dry-run从旧HEAD归档，
而VERSION工作区已升1.0.20，包内1.0.19触发版本一致性闸门；待成套提交后复验该项。
CI待验证，不能沿用其他分支数字。
Gateway一次性根8项通过；首次指定库外输出触发脚本打印相对路径异常，改按技能指定仓库内dist
输出后正常退出，临时根均清理。wheel/sdist和Gateway镜像构建通过；格式调整后已重建内容锁。
故障模拟仅覆盖程序行为，未完成业务验收：不宣称真实故障诊断准确率、多语言质量或模型稳定性。
覆盖率运行22项通过；model_request语句83/97、分支31/40，新增错误类与分类函数无遗漏语句。
进程内恢复旧错误吞并逻辑后同一回归重新失败（1 failed，1.23秒），源码未改。
PR合并、部署后自由问答复验待完成。
