# Dify 一键开通研究验证

日期：2026-10-29（北京时间）。本轮为研究文档，不是新业务功能交付。

## 来源与交叉核对

- Snail AI平台锁为Gitee `10c1d3e50b701be62da23a3403d79a41187ab8cd`，SDK1.1.1。
  官方GitHub固定修订`31c52e8277f012cfa5cfbafc53c2baaf654afa3d`读取的122个文件blob与Gitee锁一致；
  README和AgentService存在差异，未混称相同版本。AgentService用摘要匹配的本地JAR静态反汇编补证。
- Dify1.17.1固定修订`8387590ace4a094de812b7847fc6a4c3a27cd52b`的复制、导入、保存和workspace实现；
  最新官方文档的1.18应用包行为只作版本差异提示，不当成现役功能。
- AI-Ops对应方法、已有单测及优链充部署/真实403回执交叉检查；研究后同步`22bed508`，
  不将日志改进当成一键开通实现。
- 拟议异步开通的HTTP202按RFC9110区分接收和完成；字段与路径尚未定稿。

## 可重跑验证

```bash
uv run pytest -n 0 tests/test_dify_workspace_ready.py tests/test_dify_ensure_workspace.py \
  tests/test_dify_publish_api.py tests/execution/test_node_publication.py \
  tests/test_dify_login_workspace.py -rs
```

结果：**94 passed**（15.68秒，5条已有弃用警告）。测试基线`9cc3fd58`。
早期命令包含不存在的login测试路径，收集0条退出5；修正文件名并重跑后才计通过。
这些测试证明已有机制/限制，不表示自助开通的新UI/API已实现。

临时SQLite数据根用真实AgentManager创建draft，再调用真实node_publication.publish：
agent不存在及draft两种情况都拒绝“requires an existing published agent”。
同时读取ReadySettings默认插件常量：未包含当前管家openai_api_compatible插件。
不更改断言去掩盖限制，不对真实数据根执行这些探测。

## 边界与交付

本轮仅新增研究/里程碑文档。静态检查、formatter与文档引用检查用于这次文档本身，
最终PR CI结果另查，不能把94项专项写成全量CI。
没有启动Snail平台服务、没有调用生产管理API或修改账号/租户/模型/路由。
未新增公共API、持久化schema或安全规则，故本轮不新增生产代码测试、突变测试和接口契约文件。
研究中的页面、状态和验收表属于实施建议，后续实现需独立Gherkin/QA计划。
**未完成业务验收**：新自助开通流程尚未实施；优链充正例样本仍等待用户提供有站点权限的账号。

运维知识自检：本轮只有本地测试、官方源码获取及断网制品静态检查，没有新的生产运维动作；
方法和来源均记录在[Snail研究](../agents/snail-ai-publication-research.md)及
[业务方案](../agents/dify-self-service-onboarding-research.md)。原始源码与制品探测留在gitignored研究目录，不入公开PR。
