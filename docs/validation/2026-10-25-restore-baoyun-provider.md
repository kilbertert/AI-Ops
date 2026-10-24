# 模型出口恢复 baoyun，移除 Cline

基线90a5a280，分支docs/restore-baoyun-provider。用户明确要求使用原baoyun并删除Cline。

## 实际变更

- 五处AI-Ops运行配置使用baoyun，模型恢复为`deepseek-v4-flash`；出口凭据与执行清单同步。
- 六份已停平台状态逐一经平台管理API核验，智能体均绑定baoyun模型项。
  其中五份删除了Cline模型配置，另一份原本没有；改绑和删除后均读回核对。
- 删除Cline专用凭据文件，覆盖本项目运行配置中的同凭据副本。
- 开发机共享模型代理移除Cline一项及其凭据，其他三条兼容渠道保持原样。
  文件更新后运行时仍缓存旧模型别名，重启代理后`/v1/models`已无Cline别名，服务active。
- 历史失败日志与不可变运行证据保留，不将历史事实改写成baoyun执行。

## 验证

先对原baoyun凭据发一条不含业务数据的短请求，HTTP 200。随后通过实际受控模型出口
请求同一模型，HTTP 200、答案非空、`finish_reason=stop`；供应商返回模型版本
`deepseek-v4-flash-0731`。两条均为供应商连通性测试，不是订单诊断。

核验：运行配置无Cline端点或模型引用、无Cline命名凭据文件；六份平台API读回通过。
维护容器与网络已清理，生产Gateway active，gateway.env摘要不变，业务入口未启用。

**未完成业务验收**：本片没有新增业务POST、没有执行本人订单模型工具循环，也没有公司App
真机验收。baoyun当前可用不代表未来额度保证。下一步使用baoyun完成既定本人订单正例。
