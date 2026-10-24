# M3 baoyun 本人订单真实 API 链验收

基线1247dc65，分支docs/m3-baoyun-real-acceptance。时间：2026-10-25 05:33–05:34 CST。
范围：既定两主体、一个本人订单、最多两条业务POST；隔离配置/数据库、临时编辑应用，结束即停。

## 实际通过

| 判据 | 结果 |
|---|---|
| 实际Gateway容器内完整身份链 | 两次权限解析一致；真实目录判consumer；A可见、B不可见，同租户不同主体 |
| B查询A的订单 | 404 `ORDER_NOT_FOUND`，零run |
| A查询本人订单 | 原POST返回202 queued，232.7 ms |
| 真实调度 | Snail平台经gRPC派发同一run到指定Java节点 |
| baoyun模型执行与回调 | 节点两轮模型响应后完成；run=succeeded，expected=observed，exec_config_state=matched |
| 原GET交付 | QA=completed，blocks包含同订单、充电结束、来源和观察时间 |
| 独立对照 | 同订单SQL、CPMS、run快照三者订单号和状态码一致；status_code=1，source=cpms.chargingInfo |
| B回读A的QA | 404 `QA_NOT_FOUND`，无正文 |
| 撤回 | 管理路由retracted、行保留；A历史回读410 `CHAT_ROUTE_RETRACTED` |
| 清理 | 临时应用删除，原客服导出摘要不变；输入删除、容器/网络为空、timer inactive |
| 生产状态 | Gateway active，gateway.env摘要与窗口前一致，生产业务入口未启用 |

总计两条业务POST、一个run，无业务请求重发。run建立到完成31.473秒；这是后端记录时间差，
不是公司App页面端到端时延。节点日志观察到两轮token usage响应，不据此声称独立供应商账单
总次数。原始会话、订单、日志、SQL/CPMS对照与文件摘要均在私有回执中。

## 真与未验证

真实：Redis会话、公司身份接口/目录、订单授权与业务库、Dify临时应用导出及发布接线、Snail
平台、Java节点、baoyun供应商、CPMS、原App API的POST/GET与回读守卫。没有替换这些组件。
本轮执行引擎为node，Dify只提供编辑来源；不把它写成Dify Workflow真实模型执行验收。

本轮没有重新跑草稿试用、恢复历史版本或双租户并发，这些保留此前各自的验收范围。
请求由验收脚本驱动原API，**不是公司App真机点击**；临时SOP不是现役客服SOP，未常驻开放。

**未完成业务验收**：尚无工程师确认的真实故障诊断结论，不声明业务准确率；公司App页面和
正式启用验收仍待完成。本轮完成的是一个真实本人订单状态的API成功链及对应权限/撤回检查。
下一步对接登录App的页面验收和正式发布配置，继续使用baoyun，不重做已通过的底层接线。
