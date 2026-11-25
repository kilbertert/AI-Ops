# 320. 登记表部署上按申报入口分派本环境凭据链，operator 个人令牌走本环境 UPMS

状态：已采纳；`identity/caller_auth.EntryCallerResolver` +
`gateway_api._fallback_caller_resolver` 装配点（1.40.2）。

ADR-0319 给环境 HTTPS 域名补开了用户助手精确入口，但把「环境未配公司来源密钥时
operator 被既有链拒绝」写成设计内的 fail closed——它没有回答「operator 在该环境
用什么凭据被验证」。登记表（`environment_registry_file`）部署的本环境 Gateway 上
并存两条凭据链：会话链（`third_session_service_token` + thirdSession → Redis 解析）
与 UPMS 链（个人 Bearer → `UpmsCallerResolver` → `GET /user/info`）。问题出在装配：
`_fallback_caller_resolver` 三选一互斥，`third_session_service_token` 一配就短路
return，UPMS 兜底行永远不可达——operator 的个人 Bearer 连服务凭据比对都过不了，
两条链实际只剩一条活。已否决的配置出路：远端 check_token 契约在公司侧未成立；
本地 `company_jwt_key` 是被否的旧 payload-trust；摘掉会话令牌等于杀掉 consumer 链。

## 决定

- 装配只在**三个条件同时成立**时加一层 `EntryCallerResolver`：配置了
  `environment_registry_file`、会话链已选中、`runtime.upms.base_url` 可用。
  分派判据复用 `is_operator_entry`（strip+lower，与用户入口 nginx 的规范化同口径）：
  `operator` 入口的个人 Bearer 走本环境 UPMS；其余入口（`consumer`、缺失、非法值）
  逐字落到既有链——缺登记表时返回值与引入分派之前逐字一致，旧 41 合同不变。
- 入口只决定**谁来解析**，身份仍由所选链全量验证。UPMS 的拒绝不落回会话链：
  入口声明 operator 就是把凭据交给 UPMS，被拒后改试另一条链等于「同一份凭据
  换链再猜一遍」，与密钥门 docstring 里写明的错投是同一种错。
- `third_session` 不越过入口：UPMS 链只收到令牌（它本来就不读会话/入口参数），
  会话值只在落到会话链时透传——一个请求不能同时是两种凭据。
- 租户准入不变：`RegisteredCallerResolver` 包在最外层，UPMS 解析出的
  `effective_tenant_id` 同样过 `require()`——operator 只能进其租户已登记启用
  的环境，跨环境/停用一律拒绝且无默认回落。
- 零新配置：复用 `AIOPS_UPMS_BASE_URL` + 登记表 + 既有会话凭据，不引入第二把
  共享秘密——个人令牌由 UPMS 上游验证，不是「试出来哪个解析器不报错」。
- 来源密钥门原样在前：`company_source_key` 已配置时 `SourceKeyCallerResolver`
  仍是第一级，密钥验过走公司令牌链——分派不替代那条不同安全模型的路径。

## 替代方案

按令牌形状分派（先试会话、被拒再试 UPMS）：顺序错了就是静默降级，两条链身份
来源不同，落点无法预期——ADR-0009 D8 已否过同类方案，排除。
给 operator 入口也注入来源密钥、复用公司令牌链：公司侧 check_token 契约未成立、
jwt_key 是被否的 payload-trust，且本环境 UPMS 已能验证同一批人，排除。
把分派写成第二个 `map`/无条件生效：非登记表部署（41）入口头可信度不同，无条件
生效会改变既有合同——登记表存在性就是这个能力的开关，排除。
UPMS 不可用时硬编码回退会话链：把「验证不了」伪装成「换一条试试」，排除。

## 后果

- 本决定**只替代** ADR-0319 后果条款中「operator 依赖 `company_source_key`」在
  登记表部署上的部分（该条款已标注例外并保留原文）；未启用分派的旧模式部署、
  来源密钥注入路径本身均不变。
- 首次鉴权、任务复核与结果回读共用同一种权威站点范围：UPMS 混合断言在鉴权
  出口规范化到实时站点集合（`refresh_scope` 同款目录），避免「同一组站点、
  两种指纹」的误拒；真实站点收缩仍按复核拒绝。
- 规范化是**替换不是并集**：`/user/ds` 断言的 `siteIds`（直授站点）不作为
  授权来源保留——本条链的站点权威只有实时店铺→站点映射一份，取并集等于拿
  登录快照给自己加授权。仅持有直授站点且店铺→站点映射为空的 operator 在
  首鉴即被收窄为空集合而拒：这是 `refresh_scope` 对同一账号本就会在复核/
  回读做的替换的**提前对齐**（既有行为的同一口径前移），不是新收窄；旧 41
  的全局权威与该部署上的集合边界均不变。
- 登记表部署上 operator 与 consumer 各有可验证的链；非登记表部署逐字不变。
- `X-Business-Entry` 自报面不扩大：自报 operator 只是把凭据交给 UPMS 验证
  （仍需真 UPMS 令牌），自报 consumer 仍需服务令牌+有效会话；环境公网唯一入口
  的用户入口片本就覆盖注入规范化值。
- UPMS 成为该部署上 operator 路径硬依赖：`/user/info` 故障按本链既有合同判
  invalid/unavailable（`UpmsCallerResolver` 的既有映射不改），与会话链各自的
  错误分类互相独立、互不抹平。
- 管理面（不传入口头的 `authenticated_agent_caller`/`shortcut_caller`）落到既有链，
  行为不变——用户入口白名单本来就不含这些路径。
- 租户顶层主账号（`is_tenant_level_account(user_type)` 即 `type ∈ {-1, 1}` 且非代查、
  持有真实 `b_user_id`）在 `EntryCallerResolver._normalize_operator_scope` 中按既有
  `data_scope_rule.py` / `build_trial_scope` 单一口径归一为 `SCOPE_TYPE_ALL`（2026-11-25
  95 正式 HTTPS 窗口实证：公司 UPMS `/user/ds` 返回 `code=1` 降级到角色 `dsType=2` 后，
  顶层账号因无门店绑定得到 `organ` + 空 `site_ids`，导致正式入口误报
  `OPERATOR_SITE_SCOPE_REQUIRED`）。普通/代理商账号（`type ∉ {-1, 1}`）与代查上下文
  不进入此分支，继续按实时店铺→站点映射 fail closed；跨租户隔离仍由 `effective_tenant_id`
  与 `RegisteredCallerResolver` 强制兜底。
