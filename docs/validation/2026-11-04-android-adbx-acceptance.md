# adbx安卓真实验收：部分通过

基线9ca54968，分支test/android-adbx-acceptance。新增运行器集成在现有
`.claude/skills/verify-aiops-client-e2e/`，使用外部adbx，不修改该工具或生产服务。

## 真实环境与操作

设备NAM-AL00，Android 12，1080×2340；现装消费者APK `com.trend.ulink`，1.2.15/versionCode24。
通过正常手机号登录使用受控验收账号，路径“我的→下滚→AI客服”。没有注入会话、装新APK、
修改订单或执行充电操作。管家APK为1.0.4，本轮仅观察首页/个人页，不宣称管家问答通过。

| 用例 | 结果 | 证据 |
|---|---|---|
| 推荐功率问题 | PASS | 当前chat页面点原推荐项，新增功率解释，截图与UI树一致 |
| 新对话 | PASS | 点击后欢迎页出现，旧答案片段消失；验证当前页面，不匹配后台WebView |
| 普通知识提问 | FAIL | 设备输入并发送`How to charge safely`，出现“暂时无法理解这次请求…尚未查询您的订单” |

最终运行器自动执行推荐问答+新对话窗口为2026-11-04T09:10:50Z–2026-11-04T09:11:46Z，两步骤通过；
运行器另有初次UI导出失败（UNVERIFIED）和修复后单步骤通过记录，不删改失败证据。
普通提问由截图确认坐标后视觉操作，因为该构建的输入框没有暴露在当前WebView的UI树中。
其失败不能算成运行器通过；最终业务结论为**部分通过**。

## 普通提问失败的独立对照

2026-11-04T08:53:03Z，生产访问日志有Android 12/NAM-AL00/Html5Plus请求
`POST /v1/assistant/questions`，状态503；同秒Gateway记录
`CONSUMER_INTENT_UNAVAILABLE (RuntimeError)`。健康版本为`1.0.18+b3dfd4dc81a5`。
常规assistant_questions表未创建该问题的QA，不代表客户端没调用；失败发生在意图路由阶段。
具体RuntimeError成因未在本片判定，不用猜测归为APK或模型。已登记[缺陷#1345](https://github.com/kilbertert/AI-Ops/issues/1345)，独立复现和修复。

## 工具与脚本验证

- adbx隧道初次up但ADB握手失败：Windows短SSH会话退出后服务不再监听。临时保活会话后
  devices、shot、ui、frames可用；没有修改主机服务或防火墙。
- exec-out将UIAutomator状态输出混入XML，首跑UNVERIFIED；重定向状态输出并删除旧XML后通过。
- uni-app同时保留旧页面树：只匹配第一个当前WebView。空UI、后台页面、不可见节点、旧回答、
  用户回显、多设备/离线、畸形计划均有反向用例，失败/未取证不返回0。
- 代码/计划/设备/截图摘要留在私有report.json，账号、UI和截图不提交公开仓库。
  自动PASS仅证明同一当前节点包含预期片段，仍需截图和业务语义审阅。
- 20项运行器反向检查与25项目录/文档检查全部通过（合计45项）；Ruff/格式检查通过。全量4123通过/9跳过；CI待验证。

未完成业务验收：本轮普通问答失败；未覆盖语音、相机、跨账号、多语言、最新管家APK或真实故障准确率。
