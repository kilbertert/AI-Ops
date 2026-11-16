# 控制台活动会话验证

候选分支fix/console-active-session，基线734f0201；**未完成业务验收**。
判据见[活动会话合同](../acceptance/console-active-session.feature)和[QA](../acceptance/console-active-session-qa-plan.md)。

- 两个旧版HTTP反例真实失败：退出后发布仍返回身份；原生退出路径未进入代理（404）。
- 定向170项通过：双代理同号主体及同公司token隔离、发布与交换同时退出拒绝、未配置交换密钥正常登录、
  到期与租户映射变化拒绝、原生退出正确透传/失败不泄露及CSRF缺失不撤销。
- 保留原平台发布码测试；它先改变type进行判权，不再顺带改变会话租户。租户变更作为单独拒绝反例。
- 旧版无key不保留绑定，候选无key只记会话哈希/身份且无公司token；没有扩大机器令牌入口。
- 关联服务覆盖：login service 93%，login server 80%，不是全业务覆盖；移除租户绑定检查与退出撤销各触发指定反例失败。
- ruff/format及wheel/sdist通过；全量5145通过/10跳过，文档结构30项通过，独立执行待补。

已有检查没有覆盖发布检查的活动绑定和原Dify退出入口，因此新增最小HTTP反例；复用既有双代理/映射夹具。
生产登录代理仍为旧包。只读确认服务身份/权限；未对生产注入会话、修改公司权限或业务数据。
124只读采样旧Gateway0.1.0 active，MemAvailable约6GB、swap约12GB；一次采样不能作为业务容量通过证据。

独立CLI会话8e49c2e0执行33个HTTP观察均符合原判据，实际模型gpt-6-astra；包括共享Dify账号和签名key的双环境，未读取生产。
Devin另找到无关畸形Cookie使SimpleCookie跳过撤销，作者实测两个反例失败后改为直接撤销已校验的会话；修复后关联118项通过，独立复验待补。
