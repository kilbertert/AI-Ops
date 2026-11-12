# 回复策略恢复与验收

用户确认：客服恢复11月11日最后自然语言版本及提示词；管家币种标注不一票否决。
仅AI-Ops，保留商城证据读取与Java错误传播修复，不操作业务订单、不修改公司App/H5。

## 验收合同

```gherkin
Feature: 自然答复与业务相关的事实复核
  Scenario: 管家仅多了币种标注
    Given 金额数值及其他事实均有本次证据支持
    When 回答使用来源未提供的币种名称或符号
    Then 不因币种单独拒绝或改为证据兜底
  Scenario: 带币种的回答还有金额或站点错误
    Given 回答改写金额或使用无来源站名
    When 执行事实复核
    Then 不因币种例外覆盖其他拒绝判据
  Scenario: 客服自然回答
    Given 客服使用已确认历史提示词与受控工具
    When 原H5或Dify试用完成
    Then 显示Agent最后自然正文并附真实来源
    And 即时危险提示和已授权官方客服入口仍可用
    And 无效链接、无权限数据、撤权及未完成工具仍执行既有校验
```

## 发布方法

1. 先读current-delivery-state及最新验收记录。通过41手册的dev-host、服务身份、SQLite mode=ro
   读取当前consumer路由及agent_versions。主租户V27、第二租户V5于11月11日发布；
   与当前V28/V6逐字段对比，已确认workflow仅instruction不同。原始提示词/环境/账号只保留私有证据。
2. 从当前Dify工作流draft读取hash并备份，构造候选时只替换answer Agent的instruction，
   内容取自上述历史snapshot；graph其他部分、features、conversation_variables及环境不变。
   写前再核hash，冲突则重新读取，不覆盖并行编辑。用原管理预览验证后正常发布新版本，
   新版本号不必等于历史编号，但指令需逐字等于历史目标。
3. 管家业务SOP只去除旧币种禁止句，保留步骤和模型/工具选择；系统后缀在新版发布时重新冻结。
   旧完整后缀仍可识别并剥离，不能把它当业务提示词重复拼入。先预览后按原发布按钮发布。
4. 源码通过正常PR/CI/合并/CD；只恢复上述应用，不更换模型。发布前后逐项核路由变化，
   其余应用保持原值。回退用旧完整制品和已备份发布版，不恢复业务数据库。

## QA

|编号|环境和操作|判据与清理|
|---|---|---|
|RP1|临时Gateway及合成复核；仅币种allow、其他reject、未知站名|原回读能交付，其他拒绝保持；fixture清理|
|RP2|临时客服原POST/GET及试用，模型自然回答并调用受控来源|正文与模型末轮一致，来源/隐私/回读权限有效；fixture清理|
|RP3|历史完整指令块迁移，包含v6和旧管家后缀|幂等、保留业务文案、拒绝重复/未知块；不修改生产|
|RP4|当前真实复核模型+合成证据|币种单独allow，金额/状态/虚构退款reject；结果只证明该组探测|
|RP5|合并部署后原Dify/H5与两个客服历史提示词|正常语言答复，原金额/状态核对；管家原问题不因币种变证据兜底；保留QA关闭隧道|

命令：pytest对应test_operator_answer_review、test_consumer_agent、test_consumer_agent_flow、
test_consumer_natural_delivery；Gateway实跑用verify-aiops-gateway且临时数据根。
RP5使用既有env-41-runbook和consumer-wf01/live-release的正常登录、草稿预览和发布方法。
未完成RP5与业务验收前不得称线上已恢复或模型准确率达标。
