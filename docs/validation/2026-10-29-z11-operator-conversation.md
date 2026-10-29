# 管家无订单对话与真实站点查询：本地验收

分支`fix/operator-conversation`，基线`5bdebfdb0e754d592801fdb1f7d363d6e2df437a`。
产品反馈原句“帮我找一下广州天河附近的充电站”在模型前被原生桥拒绝400；正式入口的
“订单状态3是什么意思”也被要求选单。已先复现，再以回归测试修复。

业务理解：管家是问答入口，订单只是一种业务输入。用户确认本轮必须查询真实站点且限当前账号。
实现见[ADR0165](../adr/0165-order-is-an-input-not-a-conversation-gate.md)，
验收映射见[场景](../agents/operator-cpms/conversation.feature)和[QA](../agents/operator-cpms/conversation-qa.md)。

## 自动化证据

- 最终全量3372 passed / 9 skipped（139.93秒）；专项319 passed（17.05秒）。
- 真Java/Gateway整链14场景通过、37次合成模型请求：普通咨询无业务读取，站点搜索一次CPMS，
  原订单双工具、各拒绝场景、原生与普通试用保持原判据。
- 租户/站点越权、缺身份、超量、分页、空范围、取消中读取、条件注入等回归；三个定向变异
  （去租户校验、去站点校验、增大页预算）全部检出；原源码恢复。
- 新对话协议模块语句覆盖41/44，93%；此次窄套件未覆盖已有订单模块全部分支，不以总覆盖率
  宣称安全。未配置Java覆盖工具，Java以真实装配整链验证。
- Ruff、格式、包依赖无新环、README链接、Gateway临时数据根8项HTTP检查通过。
- JDK21锁定依赖Maven package成功，jar摘要
  `ca6672176c6e81d7378dd4e2b0f75f7016459e4a82803ec4b8642a49983e79eb`。
- 部署内容锁通过真实`build-image.sh --update-lock`更新。首次全量5项失败分别是内容锁、
  新模块环/导航、使用主checkout解释器的2条检查，均修复后全量通过。

本地证据位于任务checkout的`dist/verification/`，非公开CI制品；记录含源码摘要/时间。
尚无本次远端CI、合并或部署结果；后续真实模型/页面验收另记。

## 公司接口只读核对

按公司源码规程查`iot/cloud-charging-pile:release`的分页控制器、请求DTO、mapper；
`/chSite/page`使用name/address过滤、current/size分页，响应具id/tenantId。正常ulink公司身份
只读实测`code=0`，records/total/size/current/pages字段存在，首批10条。只写字段契约，不公开
真实站点/联系人或会话。实现仅保留id/name/address，不使用联系人、电话、价格或空闲字段。

严格校验响应当前租户与站点集合；最多4页×50条，每页3秒、262144字节，最多显示10条。
地点按用户原话提取，缺定位不声称距离排序；达到查询上限明确不完整。
正常对话/找站是新清单类型，节点先升级，再切Gateway和桥；不靠修改Dify提示词解锁。

## 边界

真实公司API探测不等于真实模型或页面验收；**未完成业务验收**。
无订单回合不继承前次订单，不提供任意工具或跨租户搜索；C端原路径保持不变。
本轮不接地图定位、实时空闲枪口、价格、知识库检索或执行业务写动作。
操作自检：公司源码读取沿用`company-code-recon-procedure.md`；正常登录和独立只读核对沿用
`runtime-retirement-operations.md`；部署顺序与恢复已补到节点手册。
