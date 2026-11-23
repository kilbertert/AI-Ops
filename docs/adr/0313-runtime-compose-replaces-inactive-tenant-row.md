# 313. 未开通租户的运行行允许被重铸候选原位替换

状态：已采纳；env95 第二租户验收实测暴露的无出口缺陷，与 ADR-0312 同族
（发布取消后的恢复面缺失），失败面在安装侧运行组合。

ADR-0312 让被发布消费过的候选在准备对账时重铸为新应用，但重铸只是恢复
链的第一段：新候选仍要经 `candidate-install` 写入远端运行制品。实测
（env95 install task）显示运行组合把「被消费的候选行」当成「现役租户
运行」一并封死：

- `compose_runtime`（environment_artifacts.py）对同租户既有行一律
  `existing tenant runtime cannot be replaced by candidate` 拒绝。守卫
  的本意是「共享执行资源不得顺带变化」——已开通租户的行不得被候选
  顶替。但发布在中央步之后被取消时，该租户的行是**从未承接过流量的
  已消费候选身份**：目录里 `enabled` 恒为 False，行内容与候选恒不相等
  （ADR-0312 保证重铸应用 id 不同），每次重计划都重铸、每次重铸都撞同
  一条拒绝——状态机无出口。
- 即使组合放行，`Installer` 的应用时校验
  （install.py `active tenant runtime removed or replaced`）在任一租户
  已开通时冻结**全部**既有行逐字相等——环境级粒度的行冻结，把未开通
  租户的候选行也焊死。两处判据同源、同缺陷：把「有现役租户」误读成
  「所有行都现役」。

「未开通」的远端可核验证据取自安装目录
`registry/environment.json`：安装写入行时 `enabled` 为 False，
`publication.activate` 开通时置 True，restore 回切 False——与
`candidate_directory` 判「现役环境」用的正是同一面旗
（`any(t["enabled"])`），判据与既有语义同口径而非新造。enabled 为
False 的行不承接任何路由流量：候选合同下开通前无正式入口，恢复后入口
已回切——顶替它对现役租户与候选租户都无影响；开通/曾开通行缺失
`enabled` 键的畸形形状仍按保护处理（fail-closed）。

## 决定

**组合放行（compose_runtime）**：新增 `replaceable_tenants` 参数（调用方
传入「允许被替换的租户」集合，默认空集保持原拒绝）。同租户既有行与候选
不等时：租户在集合内则摘除旧行、走既有追加路径原位替换；不在则维持
`cannot be replaced` 拒绝。跨租户 app_id 占用检查与幂等短路
（`same == [candidate]`、`source_runtime_sha256` 血统比对）不变——替换
后的快照重放同输入仍收敛为无操作。

**安装侧判据（Installer.candidate_runtime）**：组合前读安装目录，把
`enabled` 恰为 False 的租户 id 集合作 `replaceable_tenants` 传入；先跑
`check_registry` 保证目录即登记在案的版本。目录缺 `registry_sha256` 记
录（旧安装形状）时集合为空，退回全量拒绝——判据读不到就拒绝，不猜。

**应用时行闸（Installer 部署校验）**：`removed or replaced` 行检查从
「任一租户开通则冻结全部行」收窄为「只冻结 `enabled` 非 False 的行」；
缺 `tenant_id` 的畸形行不在可替换集内，仍须逐字保留。与组合层形成
「同一判据、两道门」：组合产出即受约，应用再独立复核一遍。

## 替代方案

由计划/规格显式携带「替换声明」字段：安装侧能拿目录 `enabled` 直接核验
现役性，声明确认不了远端真相，反而给错判一个伪造入口；用可核验状态
而非声明。

对同租户重铸一律放行（不判 `enabled`）：开通中的租户行若被候选顶替，
路由面按行找应用会指向未开通应用——守卫的存在理由正是这个场景，
必须保留。

人工在目标机摘除已消费行：运行制品的写面归安装器所有，绕过受管面改
`consumer-targets.json` 既破坏 digest 血统（`runtime_sha256` 对不上
`installed.json`），也让下一次组合失去可核验基线。

## 后果

env95 形状（两租户均未开通、一租户行是被消费候选）下重铸候选可完成
安装组合；任一租户开通后，同租户替换仍在组合层与应用层双重拒绝。

`registrations.json` 为单租户登记清单，重放时由
`source_runtime_sha256` + 内容比对判幂等，替换语义不改变该协议。
schema/1 单租户原位更新旁路（同租户原安装可更新自身配置）不变；本判据
只管 schema/2 组合面。

替换只改应用身份（app_id 等行内容）；租户 id 恒等，行数守恒——不存在
「借替换摘除他人行」的路径。登记侧的 lineage 原位 UPDATE
（ADR-0312 `register_runtime`）是这条链的下一环：新候选 app_id 落到
目标库时由它接管。
