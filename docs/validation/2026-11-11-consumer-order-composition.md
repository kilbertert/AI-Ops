# 消费者订单构成：本地验证

日期：2026-11-11。候选1.7.1，实现与初验base `9339561e`；进入PR交付，尚未部署。未完成业务验收。
证据根`dist/verification/consumer-order-composition/`；判据与逐字段映射见
[QA](../agents/consumer-wf01/order-composition-qa.md)，决策见[ADR0246](../adr/0246-consumer-order-composition.md)。

## 已执行结果

| 检查/证据文件 | 结果与范围 |
|---|---|
| `original-red.log`→`original-green.log` | 父提供的真实临时HTTP复现，原七键投影丢电费/服务费，候选保留 |
| `test-red.log` | 实现前15失败，主要为新增构成不存在及扩展开关尚无；独立合成源判据 |
| `focused.log` | 消费者/HTTP/端点/目录/版本762通过 |
| `migration-green.log` | 19通过，唯一块替换、保留业务前后文、幂等、未知拒绝 |
| `full.log`、`full.xml` | 全量一次4903通过、9跳过、1失败；**不能称全量全绿** |
| `cancel-recheck.log` | 修正取消测试观察时序后9通过，无生产逻辑变动 |
| `hack-red.log`→`hack-green.log` | 两项独立反馈均先失败后通过：短恶意时间文本、Decimal超大指数不再裸异常 |
| `post-hack-focused.log` | 修复后187通过，含权限/Agent/flow/HTTP/来源端点/目录/文档检查 |
| `coverage.json` | billing行112/114，分支46/48；新增金额/时间解析及构成投影函数行/分支100% |
| `mutation.log`及两个变异日志 | 内存变异丢electricityFee、null→0均触发行为断言失败，未修改源文件 |
| `ruff-check-final.log`、`ruff-format-final.log` | 均通过；compileall及`uv pip check`通过 |
| `build-final.log`、`packages/` | wheel/sdist 1.7.1成功 |
| `image-build-final.log`、`gateway-image/identity.json`、`container-import-final.log` | Gateway镜像重建及无网络只读容器导入通过 |
| `gateway/aiops-gateway.json`、`gateway.log` | 临时根真实进程：健康200、鉴权401、规范200、退役404，清理完成 |
| `plugin-roundtrip.log` | 真实插件client HTTP与原`_invoke`透传相同JSON；未安装SDK，只验证透传逻辑 |
| `worktree-audit.log` | 34个worktree审计通过；未删除或改变其他任务 |

全量失败是新增取消用例在取消终态可见后立即断言回调已经返回；运行时正确地先取消、后结束HTTP。
改为等待测试回调完成信号再检查两次拒绝和唯一源GET；原证据保留。9个skip主要为未配公司GitLab
身份锚点的既有远端复核，以及一个空串调用方短路场景，不在本轮授权访问范围。

Gateway首次临时根检查早于Hack解析修复；修复不改启动/路由/鉴权，其后原HTTP回调和镜像导入
已定向通过。初验时尚无新commit，ADR提交覆盖门禁在提交后另行检查。

## 独立验证与真实来源离线核对

独立Hacker于2026-11-11 05:47–06:01 UTC运行临时HTTP源与真实监听Gateway，
未使用作者测试输出建立判据。H1短恶意时间文本、H2 JSON数字巨指数裸500均先复现，
修复后按原输入及新时间/指数输入独立复验闭合；H1不表示模型执行了恶意指令。

实际复验覆盖：完整构成及单次GET、数字精度/零/缺项、币种缺失/冲突、分时边界与隐私白名单，
v6取数前后撤权/取消/他人订单和第9次预算拒绝，以及callback1–5原七键投影。
临时探针结果为reader 37项通过、callback 12项、旧版callback 5项、迁移6项、
新增时间/指数15项通过；另有1项基线信封观察。独立运行既有相关套件178通过、无失败/跳过，
Gateway观察通过；3项内存变异（丢电费、null转零、绕过旧兼容开关）均被独立判据捕获。

**遗留E1未修复**：`code:false`在基线和候选均被通用信封当作成功；这是Python布尔值与0相等
导致的既有行为，非本次Decimal引入。本轮记录后另行处理，不扩张通用信封修复，
不声称非法信封类型全部拒绝，也不将独立验证描述为“范围内无发现”。

父会话对两笔已获本人授权的真实源做离线投影：费用逐项一致，分时分别2/1行，摘要/费用/分时
均无invalid，币种均missing。作者仅读取脱敏结果，未读取或提交原始订单数据。
父会话另对两份真实v6图离线验证：只替换唯一instruction块，前后业务文本及schema/model/tools
保持不变。这些证据证明投影和迁移，不证明真实模型最终表达或部署已生效。

## 制品与边界

镜像`aiops-gateway:consumer-order-composition`，最终身份以`gateway-image/identity.json`为准，
源码摘要`5a1fdfb5f4654f693f5548634a70bd40192c18ba3cb874c94f6b4ed799cbd13b`。
同版本wheel/sdist用于本地候选；PR确定性CI以提交后实际运行结果为准，尚无合并或正式部署身份。

源时间/金额非法值不能变成事实；feeRate只给来源数值，custom单位未核定。
分时电量可能费用反推；分项不闭合、总额与支付不同均不猜原因，不判断计费正确性。
真实模型完整表达、原Dify/H5及其他语言行为未验；父会话负责Devin闭环、标准CD与双v6原入口验收。
运维方法已落入QA，作者只运行临时loopback与本地构建/容器，未读取真实DB或运行远端操作。

PR交付前提交候选后接入最新origin/main，核对llmdoc属主文档及提交覆盖门禁。
本轮没有远端/本地分支删除或worktree清理；合并与部署由父会话执行。

## PR交付前复核

候选4个提交已无冲突rebase至`5d8704b1`，含llmdoc初始化及客服发布回执合并。
源码提交携带ADR0246或No-ADR说明，逐提交ADR门禁通过；目录/文档/版本/迁移57项通过，
ruff、wheel/sdist、Gateway镜像重建与容器导入通过。未因文档/版本变更重复本地全量；PR CI执行完整检查。
仅更新consumer/common/gateway三篇llmdoc，以CLI刷新对应validatedRevision，未手改meta台账。
原记录的`@tokenroll/llmdoc@3.6.1`在当前npm源不可用，本轮明确使用可用3.6.0；validate通过，
四个源码/工具路径均有owner，概念“订单构成”首项命中consumer文档。
