# 管家原生预览与持续开放

原生调试框选择 `cpms-node` 后，发送消息经现有同源桥进入 Gateway 单订单试用，再由 Java
节点执行。公司身份来自正常 Dify 登录会话对应的公司令牌；不把长期 token 写入工具或浏览器。
消息没有订单时按管家对话处理：咨询、补充订单的澄清、搜索当前账号可见站点。
查询具体订单仍须唯一明确单号，不支持“我的上一个订单”或沿用前次订单。
纯订单号或“订单号: ... / order ID: ...”接受短数字编号；混合问题中未标注的纯数字仍要求
至少15位，避免把手机号当订单。字符集按 Java 任务入口为字母/数字/下划线/连字符，
带句点或冒号的编号不截断后误查，明确拒绝。

## 生效范围

预览使用当前页面尚未保存的提示词与工具勾选，按真实 provider UUID、两项工具及无参数 schema
校验。节点模型、预算和端点仍由平台模板管理；结果标明 Java 节点试用。Dify 顶部的模型选择
不修改平台配置。正式发布继续读取保存版，不把预览自动发布。

原生请求被同源注入脚本接到 `/__aiops/operator-preview`，浏览器保留自己的 Cookie/CSRF。
桥先验证来源与 Dify 应用访问权，Gateway 再交换公司身份、核订单与站点、签发短期任务权限。
普通AI-Ops试运行可输入问题或明确订单号，使用保存后的草稿；无订单时不发放订单工具。
站点搜索只显示CPMS当前返回的名称地址，无定位不承诺距离排序、价格或空闲枪口。
普通应用原生预览不变。停止按钮走 `/__aiops/operator-preview/cancel`；超时/断连同样请求
取消对应试用，不能用此入口取消正式任务。原生预览不生成假的 Dify 对话历史，正式证据留在
Gateway QA/run；界面 SSE 的空 thought 只是兼容旧 Agent 面板的显示结构。

## 部署顺序

1. 正常 PR/CI 合并后，Gateway 由既有 CD 部署；核 health 与源码 SHA。
2. 从同一合并修订运行 `ops/dify-console-bridge/build-bundle.sh REV OUT`。打包脚本将原生
   预览 JS 拼入已有 publish-button.js，携带更新后的 Python 桥和 nginx snippet；不重建 Dify。
3. 按 [桥部署手册](../../../ops/dify-console-bridge/README.md) 用 dev-host 传摘要制品，运行
   包内 deploy.sh，备份并安装原桥，核 nginx 和桥服务。公司登录代理不重启，避免清除有效会话。
4. 正常公司登录进入 Dify 后，在该管家应用输入明确订单。页面长期打开时先刷新以加载新脚本；
   登录代理公司令牌窗口目前为15分钟，过期时重登。不得自签会话或注入令牌缓存凑验收。

## 持续开放正式 Java 路线

用户 2026-10-28 已授权这条路线持续作为正式管家入口，不再沿用 10 月 26 日临时开窗后撤回的策略。
实施前读取目标应用、当前版本和 route state/revision，备份 DSL 与路由快照；确认 Java 节点在线。
按既有 `/v1/dify/publish` 管理 API 发布该应用，使用服务配置读取器取得发布 key，不从模型或
浏览器取。回读必须是 `entry=operator / engine=node / state=active`，版本与应用匹配。
正常公司身份从原 `/v1/assistant/questions` 提交明确订单，核同 QA 唯一节点 run、matched、
两项 CPMS 证据和结果可读。不存在/不可见订单、无身份和缺订单分别拒绝/澄清，无数据泄露。

发布失败保持旧路由；开放后若健康/真实取数失败，通过原 retract/restore 恢复，保留审计。
成功则保持 active，不能运行旧验收脚本 finally 里的自动撤回，也不能遗留撤回定时器。
本次开放不等于公司前端 MR 的部署，页面入口的发布状态仍须独立核实。

## 验证

- [场景](native-preview.feature)、[步骤](native-preview-qa.md)。
- `uv run pytest tests/operations/test_operator_native_preview.py tests/operations/test_operator_cpms.py tests/execution/test_node_publication.py`。
- 原始前端请求/停止形状：`node tests/operator_native_preview.mjs ops/dify-console-bridge/operator-native-preview.js`。
- 真 Java 隔离验证沿用 `tools/m2_close/m3node/verify_operator_cpms.py` 的锁定 JDK/依赖；
  模型、CPMS 和身份是合成时必须注明；不能冒充真人授权整链。

标准映射：同源/CSRF 沿用 RFC 6454 的既有边界；SSE 采用 HTML Living Standard 的
text/event-stream 行和空行分帧。当前 Dify 1.17.1 的 base.ts/hooks.ts 决定消费事件字段，升级必复验。
