# 客服发布失败：编辑往返与拒绝语义

日期2026-11-09；分支fix/consumer-publish-roundtrip，基线fba98028。现场Gateway为
1.2.7+fba98028b502；原受管发布重复500的直接异常是ConsumerFlowError。

## 已证实原因

只读对照同应用草稿/正式图：query仅type从mixed变constant；data_grant从start变量
引用变mixed空值。其余两个客户应用未见相同损坏。
逐项内存回放：现场图先拒query；仅恢复query后拒grant；两项恢复后通过。
GatewayServerSettings实际解析知识服务配置非空；不以进程env缺键误判配置缺失。

Dify固定源码实际运行时证明两种query表示输出相同。真实参数表单挂载反例产生
一次onChange，把data_grant写成mixed空值；修复后零改写、变量引用保留。
管理HTTP回归走真实SelfService发布，原缺handler回500；修复回409、
CONSUMER_FLOW_INVALID与本地原因，并记录refused审计，正式路由未变。

## 自动化与独立证据

Python相关107通过；前端原表单9项通过，其中新增浅层反例随后替换为实际组件
往返测试，后者旧红新绿；管理面15项浏览器用例通过。隔离Gateway8项通过。
最初HTTP夹具误连真实Console和测试入口拼写错误已修正，只有实际ConsumerFlowError
触发的红绿才作为回归证据。
新增长期检查针对已发生的编辑器清空变量/运行语义误拒/业务异常500，现有检查只
验证导入的正确图，未覆盖编辑后保存。拒绝授权auto/空值/越权仍按原安全合同验证。
固定Dify原生表单与PromptEditor往返反例已重放：旧版挂载即保存空grant，修复后零回调。
最终表单9项（含实际往返）通过，formatter/lint通过。38项文档/目录/版本检查及1.2.8 wheel/sdist通过。
DeepSeek指定模型CLI无回执，独立Codex会话已接续，不能将无输出视作通过。
独立Hack和最终检查待完成；生产草稿尚未修改，未完成业务验收。

## 交付范围

Gateway与Dify web同版交付；不换模型、不重配知识服务、不修改客户知识资料。
修复后须从原页面保存和发布核对，不把本地通过当生产成功。
