# 管家诊断失败时的证据交付

用户确认保留正常模型诊断；取到证据后，模型回答未通过复核或模型生成失败时，
交付本轮授权原记录并明确“诊断结论未完成”，不再次盲派同一轮。该路径不是业务准确性保证。

## 行为与边界

正常答复继续走事实复核；失败答复不拼入兜底正文或会话历史。证据交付复用最终授权、
取消、发布版本、来源及回读校验。无证据时仍失败；权限拒绝、工具失败和未知节点失败不转换为成功。
模型额度不足/断流发生在已取证之后时，只保留已收到的证据；此前失败则保留原错误码。

证据正文使用来源原数值，null与零分开，不增加币种、不相加、不反推。
订单详情以中文/英文标签列示，其他工具为有界来源字段摘录；数组至多展示6条，长内容明确省略，
源自身的total/truncated与显示截断分别保留。日志/遥测候选不能解释成交易已关联或完整过程。
渲染只读取执行层data，不输出原HTTP响应、身份、授权参数及离线权限引用。

QA的completed表示已有可交付结果。运行快照delivery_mode=evidence_only表示诊断未完成；
原日志页显示“仅交付证据（诊断未完成）”。复核原因限定为固定枚举，无原回答、金额或密钥。
旧运行不改写：有review/refused事件的失败只说明历史未通过，未留存的具体原因不猜测。

## 验收合同

```gherkin
Feature: 管家取证与诊断完成分别交付
  Scenario: 取证后模型回答未通过
    Given 当前用户已授权且本轮已取到订单记录
    When 最终回答被事实复核拒绝、不确定或复核服务不可用
    Then 原问答入口交付来源原值和诊断未完成说明
    And 不交付被拒正文且不重新派发整轮
    And 原运行日志显示仅交付证据及受限原因
  Scenario: 取证后生成失败
    Given 本轮已取得证据
    When 节点回报模型断流、生成失败或额度不足
    Then 保留本轮可交付记录且不声称诊断完成
  Scenario: 无证据或授权已失效
    Given 本轮无证据或用户已经取消、撤权、发布被撤回
    When 模型回报结果
    Then 不借证据兜底交付未授权信息或恢复运行
```

## 可执行验证

|编号|环境/数据|步骤与判据|清理|
|---|---|---|---|
|ED1|临时Gateway、合成CPMS/节点|test_operator_answer_review：拒绝/不确定/不可用；原POST/GET只一轮，原值可见，错误正文消失；取消与撤权仍拒绝|fixture清理|
|ED2|同上|test_operator_failure：取证前/后3种模型错误与派发异常，区分有证据与无证据|fixture清理|
|ED3|临时记录|test_operator_evidence：0/null、恶意字符串、数组及长度截断、受限凭据|无外部状态|
|ED4|真实Java/Gateway、合成模型/CPMS|verify_operator_cpms.py：正常诊断、追问、换站及证据兜底，断言无resample|关闭进程/容器|
|ED5|固定Dify源码及实际日志组件|浏览器用例验证证据交付标签、受限原因、历史、撤权；完整Next构建|关闭浏览器|
|ED6|合并部署后原Dify/H5、获准账号|原问题新会话，按QA核对相同run的证据/版本/状态，再开原日志；成功与诊断未完成分开记录|关隧道，不删历史QA|

命令：`uv run pytest tests/operations/ tests/dify/test_operator_run_logs.py tests/dify/test_operator_run_logs_bridge.py`。
Gateway实跑沿用verify-aiops-gateway技能，临时数据根；Java/前端构建配方分别见
[输出合同](output-contract-qa.md)与[日志手册](run-logs.md)。ED6未经生产授权及执行前标待验证。

## 排查与发布

按[41手册](../env-41-runbook.md)用dev-host在服务身份下以SQLite mode=ro读取精确QA。
先比agent_runs.state/reason和assistant_questions.error_code，再看trace_json的tool/review/resample。
成功取证后review拒绝与接口失败不同；禁止用最晚一条记录代替用户指定QA。
读取模型配置用服务配置读取器及实际进程环境，只有配置文件的shell不等于服务环境，
缺进程key会产生探针自身的unavailable；不输出环境或密钥。

本变更需Gateway与完整Dify web制品配套；同源发布桥与Java接口不变，不需修改公司H5；Java错误分类修复需配套更新节点JAR。
按既有CD及固定上游补丁构建发布，保留旧制品。页面构建scratch必须有独立Git根，
否则Tailwind会向上扫描整个AI-Ops工作区导致无关样式候选异常；不得删CSS绕过检查。
原值问题的业务准确性、任意模型断言正确率与全语言质量仍需独立业务验收。
