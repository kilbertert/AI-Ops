# 管家输出契约部署与ulink真实验收

PR [#1149](https://github.com/kilbertert/AI-Ops/pull/1149)已合并
`acc3d94851144f33e0c4d9d698e6f4f2e8069237`。最终必需CI全部通过：3349 passed / 10 skipped；
Workflow policy和spec-tree lint通过。Devin试用到期、Codex审查额度用尽，无待处理线程，
不将服务占位pass记作完成代码审查。本轮按用户授权合并、部署节点并验收。

## 制品与切换

从合并修订归档出的干净Java目录离线Maven package成功，jar摘要为
`e38da6f0556a83796a81e2c9810eac0444aed22bd74fa2dcad59b5484e95e260`，
与本地验收候选完全相同；构建不含JUnit用例，行为证明来自先前Java/Gateway验收。
仅切换Java节点，构建标识`acc3d948-e38da6f0556a`，服务身份沿用aiops41。
容器内jar摘要再次一致，发布目录143个文件中只有节点jar变化；平台、模型代理、数据库容器ID未变。
Gateway仍为`1.0.0+f58e86d62729`，只读健康通过，4条正式路由前后逐字段相等。

北京时间16:25切换成功，旧发布目录和原env备份保留。第一次40秒窗口未等到就绪，脚本
自动恢复旧jar/env/current；旧节点同样约58秒启动，证明该窗口短于现役0.22 CPU配额启动耗时。
第二次等待上限240秒，新节点约62秒启动，HTTP/gRPC与容器检查通过。无在飞任务时执行切换。
未改CPU配额、权限、模型、提示词、正式路由或业务数据。方法见[节点运维](../../deploy/node-runtime/README.md)。

## 真实入口与证据

ulink通过正常公司账号、密码及图形验证码登录Dify；原H5通过正常密码登录。
凭据已按用户要求保存在库外加密资产清单，未写入Git或公开证据，不自签用户会话。
验收窗口为北京时间16:26—16:34，真实公司身份、CPMS、模型供应商、Snail平台、Java节点、Gateway。

| 场景 | 可观察结果 | 服务端核对 |
| --- | --- | --- |
| 原管家H5：首页智能检测→订单检测→选单→发送 | 202后completed，完整报告可见，页面错误0 | 同一QA的唯一run succeeded/matched，双工具 |
| Dify原生预览 | HTTP200，页面显示Java报告及cpms.orderDetail/chargingMonitor证据 | 试用run succeeded/matched，五字段JSON |
| 普通在AI-Ops试运行 | 提交202，原同源结果接口200/completed，包含报告证据 | 试用run succeeded/matched，未发布草稿 |
| 同输入回放与取证重试 | 部署后本次5个run全部成功（2正式＋3试用） | 每个run严格五字段、双工具、预期节点等于实际节点 |
| 独立CPMS回读 | 每个run两项业务data/source逐项一致（10项） | 只剔除本次读取必然变化的observed_at，不忽略业务字段 |
| 匿名请求 | 401 ACCESS_TOKEN_REQUIRED，无结果 | 未扩大入口权限 |
| 不存在订单 | 403 CPMS_INVALID_RESPONSE，无结果 | 上游拒绝未变成诊断成功；不是跨用户授权反例 |

原始QA/run编号、真实订单、会话与截图保存在私有证据目录，未上传公开PR。
正文/节点证据来自同次请求与窗口关联，未拿历史“最新成功”冒充本次结果。
检查结束在飞run为0、四条正式路由未变，新节点持续运行，未残留本次浏览器或隧道。

## 验收中遇到的驱动问题

- 固定35秒回读时仍为running；随后同QA completed。驱动改为等待终态，重新原页验收通过。
- Playwright对SSE的Response.text取不到正文；用原页面可见证据和对应run复核，首个run实际已成功。
- 普通试用合法返回202，旧驱动误要求200；按接口契约回读已提交QA，结果completed。
- 第一次离线Maven clean缺插件；从合并修订解出无target的新目录package，未下载浮动构建依赖。

本次输出格式故障恢复验收通过。5次成功不代表长期稳定性；没有工程师确认真实历史故障根因，
诊断准确率**未完成业务验收**。JSON对象模式不保证业务语义，Gateway继续严格校验。
