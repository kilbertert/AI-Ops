# 管家额度失败与二次异常回归

日期：2026-11-04。基线`9ca54968`；任务分支`fix/operator-diagnosis-failure`。

## 可复现成因

本次失败请求的节点记录明确为HTTP403、用户额度不足，发生在首轮模型调用。
Gateway收到model_failed后仍进入成功结果回读，订单快照不存在而触发TypeError；
无订单回合落到OPERATOR_RESULT_UNAVAILABLE。两者最后都被包装成OPERATOR_DIAGNOSIS_FAILED。
Dify管家试运行分支还漏了error_message，因此页面只能显示裸码。原始运行记录保持私有。

`uv run pytest tests/operations/test_operator_failure.py -n 0 -x`在修复前失败：
期望OPERATOR_MODEL_QUOTA_EXHAUSTED，实际OPERATOR_DIAGNOSIS_FAILED，日志TypeError。
修复后按run终态先收敛；失败码不会因平台返回/抛异常而丢失，不再请求成功回读所需的CPMS数据。

## 分类依据与边界

核对锁定版本[OpenAI Java SDK 4.39.1的OpenAIServiceException](https://github.com/openai/openai-java/blob/v4.39.1/openai-java-core/src/main/kotlin/com/openai/errors/OpenAIServiceException.kt)：
读取statusCode/code/type及嵌套cause。明确insufficient_quota/insufficient_user_quota才判额度不足；
兼容当前供应商403且包含“用户额度不足”的形状。HTTP403本身不证明欠费。

向Gateway只传model_quota_exhausted/model_failed/model_stream_incomplete/node_failed，
未知原因收为node_failed；不转发上游异常正文。消费者原完成路径不改；原有失败回调字段不变。
首次部署须Java节点与Gateway一起核对；旧节点只能报泛化依赖失败。

## 自动化证据

- 全量：4126 passed / 9 skipped。JUnit：`dist/verification/full-suite.xml`。
  跳过为既有公司GitLab身份锚点缺失及已无缺口的语言目录用例；没有新增跳过。
- 新增22项Gateway失败/试运行回归，及1项原生预览SSE中文错误回归；相关专项70项通过。
- 真实Java + Spring AI + Gateway，合成模型/CPMS/Dify/身份：19场景通过，含首轮额度码、
  中文额度消息、普通403、供应商错误、合法报告、非法证据和虚假写操作。
  `dist/verification/operator-output/report.json`记录源码摘要、依赖摘要、JDK及完成时刻。
- `verify-aiops-gateway`：一次性根8项通过，证据`dist/verification/gateway/aiops-gateway.json`。
  不证明远端模型、知识库活性或真实故障准确率。
- Maven离线package成功；该模块无JUnit，不把“No tests to run”计为测试。
  wheel/sdist、真实Gateway镜像构建及镜像内导入通过，节点/镜像内容锁检查一致。
- Ruff静态与格式、compileall、依赖一致性、workflow policy、git diff --check通过。
- 覆盖率：164项订单/会话/失败测试通过；新增失败收敛函数5/5语句、3/4分支已执行，
  未命中的是其他终态原因。worker为41/43语句、12/16分支。该函数2个条件，无新增循环；
  Java cause遍历硬上限16。JSON报告与coverage数据库同前缀曾触发合并警告，报告读取成功。
- 变异：测试进程内移除_raise_node_failure后，首个回归重新失败（1 failed，1.60秒）；
  工作区源码未更改。原红测和Java三类403对照共同防止只测日志而未测用户结果。

测试夹具第一次把无订单写成order_no空串，触发422；改为按真实客户端省略该字段后通过。
这次夹具失败不是新的生产根因。临时进程与数据根已清理，正常生产失败记录未覆盖。

## 待完成

未部署，远端CI待验证。操作者会充值，尚未确认完成；未做充值后的真实入口复测。
未完成业务验收：没有工程师确认的真实故障根因，不宣称准确率或长期模型稳定性。
