# Dify 控制台连接预检与恢复

适用于原生预览在创建问答前返回 `OPERATOR_PREVIEW_INVALID`，且底层
`DifyDslClient` 拒绝控制台地址的情况。规则沿用[ADR-0213](../../adr/0213-outbound-endpoint-policy-has-one-rule.md)：
携带控制台密钥的出站连接须用 HTTPS；HTTP 只允许本机回环。不得取消此检查来恢复预览。

## 先定位，再改配置

1. 通过[环境手册](../env-41-runbook.md)的 `dev-host` 访问声明主机，读 health、
   同时段预览响应和是否已生成问答运行。浏览器错误文案不能替代 HTTP 错误码。
2. 用服务自己的配置读取器 `GatewayServerSettings.from_env()`，在服务身份下核对
   `AIOPS_GATEWAY_DIFY_CONSOLE_BASE_URL`；同时比对进程环境与配置文件。
   `gateway.env` 优先于 `production.env`，只改后者可能仍被前者覆盖。
   不打印配置对象、密钥、Cookie 或原始 HTTP 请求头。
3. `candidate()` 先构造 `DifyDslClient`。在未发请求前就拒绝远端HTTP时，
   这是部署连接配置错误，不是模型额度、场站数据或用户提示词错误。
4. 先在内存中使用候选安全地址，以同一应用、工作区和受控密钥只读调用
   `export_dsl()`、`tool_provider_ids()` 和原草稿 `candidate()`；只输出通过/失败及工具数量。
   可以复用已经登记并在运行的加密隧道，方法见[既有隧道](../../../ops/dify-inner-api/README.md)。
   回环本身不是身份授权，仍要保留原控制台密钥及工作区校验。

## 安装与验收

- 在已批准的配置窗口保存两份env的原始字节、摘要、权限和属主；候选只替换控制台URL键，
  通过 `dev-host write --if-sha256 --artifact-sha256` 交付已核对的配置产物。
  遇到并行部署导致摘要变化应停止并重新核对，不能覆盖另一会话的修改。
- 从合并修订运行 `deploy/deploy-41.sh --commit <merge>`。脚本先解包候选源码，
  用候选 `DifyDslClient` 及真实服务配置做无网络构造检查；拒绝时明确回报
  `console-preflight=refused`，不替换生产源码、不重启服务。成功后继续原标准部署与回滚。
- 预检只证明配置满足客户端构造约束，不能证明网络可达、身份有效或业务正确。
  部署后核进程实际URL、health与源码摘要，再从原Dify页面提交原问题；必须有正文、
  终态 `message_end` 和对应成功QA，不能只看202或health。
- 失败恢复本轮精确配置与相容的合并版本，保留问答与审计记录。
  不改草稿工具、不重发正式版本、不写订单或站点数据来修复连接问题。

## QA计划

对应[验收契约](dify-console-preflight.feature)。所有本地用例使用一次性数据根。

| 编号 | 环境、数据与步骤 | 期望结果、清理 |
|---|---|---|
| DC-01 | 本地构造Gateway，控制台为远端HTTP且密钥齐全 | 启动拒绝并点名配置键；未创建数据库 |
| DC-02 | 执行部署脚本原预检块，分别给远端HTTP、回环HTTP、HTTPS | 仅远端HTTP拒绝，未到生产变更标记；临时目录自动清理 |
| DC-03 | 一次性Gateway，经回环地址启动；运行verify-aiops-gateway | health、鉴权和退役路由通过；临时服务及数据根删除 |
| DC-04 | 合并部署后原Dify新会话，输入“天河有站点吗？”及原“广州天河”问法 | 页面显示本轮站点、成功QA与部署版本相符；证据留私有目录 |

DC-01/02：`uv run pytest tests/dify/test_dify_publish_api.py tests/test_cd_deploy_scripts.py`。
DC-03：`uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py --out dist/verification/gateway-console`。
DC-04沿用[站点原入口步骤](site-query-real-path-qa.md)，同时记录原H5是否受影响。
