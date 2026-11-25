# 327. 登记路由冲突只挡外血统占用，不挡本血统发布留行

状态：已采纳；只收窄 `SelfServiceStore.register_runtime` 的 `SETUP_ROUTE_CONFLICT`
谓词口径，不改冲突检查的位置、时机与其余任何判据。

> **后续更正（ADR-0331）**：下文「同租户异应用=占用」子句的前提——operator app_id
> 是确定性派生、再登记必然重提同一值——已被 env95 生产证伪（重准备必然重铸
> app_id），该子句由 **ADR-0331** 移除。本 ADR 的其余收窄（retracted 墓碑不计、
> 异租户占用仍冲突）继续现役。

`register_runtime` 的登记路由冲突检查对 `chat_route_bindings` 按
`environment+tenant` 计**任意状态**行、对 `dify_app_registry` 按
`tenant_id=? OR app_id IN (候选)` 计行。而这两张表**只由发布路径写**：
`chat_route_bindings` 撤回后按设计留墓碑行（模块文档明示「撤回后这一行仍在」，
状态域只有 `active`/`retracted`），`dify_app_registry` 由发布激活写入
（`agent_manifest`/`chat_route_bindings` upsert），登记路径从不写它们。

于是「租户一旦发布过」与「再登记/再观察」互相排斥：已发布+已恢复的租户提交
`candidate-register` 或远端 `observe-registration` 时，必然撞上自己发布血统
留下的墓碑与注册表行，抛 `SETUP_ROUTE_CONFLICT`——登记观察拿不到任何
verdict（`unconfirmed`→`reconcile`→`checking` 永驻），新登记也在同一谓词上
再中毒。该死锁在 env 95 生产实证：平高/小趋两租户各 2 条 retracted 绑定 +
1 条 operator 注册表行，候选登记每次远端观察都失败。

冲突谓词的本意是挡**占用**：活跃路由绑定仍在服务、或本候选的确定性 app_id
被别的租户正式绑定、或本租户正式绑定了别的应用（`test_registration_
rejects_formal_binding` 钉住的后一种语义）。本血统发布留行三者都不是。

## 决定

- `chat_route_bindings` 只计 `state != 'retracted'` 的行：撤回墓碑是设计内
  持久态而非占用，活跃绑定（含未来新增的非撤回态）仍一律拒绝——失败方向
  保持封闭。
- `dify_app_registry` 收窄为两种真实占用：本租户绑定了**其它** app_id
  （正式绑定与候选不一致）或本候选 app_id 被**其它**租户绑定。同租户同
  app_id 是本血统自留行，放行——operator app_id 是确定性派生，再登记必然
  重提同一值。

不放宽的边界：活跃绑定、异租户占用、同租户异应用绑定依旧冲突；谓词仍在
所有写路径之前，冲突即中止且本次调用零写。

被否方案：登记后由 restore 清理墓碑/注册表行——撤回墓碑是发布 CAS 的设计
基座（advance 按 expected 修订推进），删除会破坏再发布的对账基线；worker
侧绕行 register_runtime 直读库表——丢掉 `receipt-fd` 完整性核验与
`verify()` 双验，等于另造一条无防护登记面。

实测：env 95 部署 9d8e64fd 上逐跳回放 `observe-registration`，内层
`environment-runtime-register --observe-only` 退出 1；谓词对活库逐项求值
确认命中上述两类本血统行。修复后已发布租户可再登记/再观察，登记任务拿到
`registered`/`unchanged` verdict 正常终态化。
