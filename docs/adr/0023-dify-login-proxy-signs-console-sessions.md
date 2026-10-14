# Dify 登录代理：公司账号换会话，凭 `SECRET_KEY` 自签

日期：2026-10-14
状态：已采纳
影响版本：PRD #772 B（子票 #778）；取代 PRD #772 中「**不使用 `SECRET_KEY` 自签会话**」一句，
以及「凭据代管（为映射账号生成并保管随机密码）」整节；取代 ADR-0021 中「公司账号登 Dify
走企业版 SSO（挂起）」一句。

## 背景

运营今天要拿一个**与公司身份无关**的 Dify 邮箱密码登控制台。PRD #772 要把它换成公司账号
登录，并据此决定谁能编辑、谁能在生产上发布（发布权见 #780）。原方案打算"代理保管一个
为映射账号生成的随机密码、走官方 `/console/api/login`"。

**实现前核实推翻了那个方案**：Dify 1.17.1 **没有任何接口能给一个已有账号设密码** ——
邀请创建时 `password=None`、激活只设资料、重置要邮件令牌，而 PRD 又要求邮箱不可投递。
详见 #778 的阻塞评论。

## 决定

**代理持有 Dify 的 `SECRET_KEY`，为映射账号自签控制台会话。**

核实依据（Dify 1.17.1 `8387590a`，部署实例 `deploy-api-1`）：

* 控制台会话是 `SECRET_KEY` 签的 HS256 JWT；签发见 `services/account_service.py`
  `get_account_jwt_token_for_account_id`：`{user_id, exp, iss, sub}`，`iss` = 部署版本。
* 逐请求校验只有 `extensions/ext_login.py` 的 `PassportService().verify()` → 取
  `user_id` → 查库（`load_user` 拒绝 `banned`）。**没有任何一步校验密码**，也不查会话存储。
* csrf 令牌同源同形：`libs/token.py` `generate_csrf_token` = `{exp, sub=user_id}`，
  同 `SECRET_KEY`；`check_csrf_token` 要求头与 cookie 逐字相同且 `sub == user_id`。

**已在 36 上只读验证**（未改 Dify 任何数据）：按上面形状自签一枚会话打
`/console/api/workspaces/current/summary` → `HTTP=200 ROLE=owner`；本仓的签名算法与 Dify 的
`PassportService.verify` 交叉核对通过（`VERIFY=PASS`）。

## 为什么它同时消掉了原阻塞

自签模型下**根本不存在"要保管的密码"**：代理不去调 `/console/api/login`，也就没有"先给
账号准备好一个密码"这一步。开户仍走官方「邀请 → 激活」，而**激活 token 由邀请接口在响应里
直接返回**（`members.py`：`url=f"{console_web_url}/activate?email=…&token={token}"`），
代理从响应里取它调 `/console/api/activate` —— **全程不经过邮件**，于是"不可投递邮箱"这条
要求自然满足，不再与"重置密码要邮件"矛盾。

## 被接受的代价（本决定的核心）

**代理持有 `SECRET_KEY` = 能签任何 Dify 账号（含 owner）的会话**，且那把 key 同时保护
Dify 库里存着的工具/模型凭据（`libs/encryption.py` 的字段加密也用它）。因此：

* 代理被攻破 ⇒ 全部映射账号失守 **且** Dify 侧存储凭据可被解密。这与"自签任意会话"同量级，
  不是可以靠"少存点东西"缓解的。
* 缓解（部署侧，非本仓可自证）：代理只监听**回环**，经控制台入口**同源反代**，不对外另开
  端口；代理进程不落任何公司口令或令牌；日志只记公司用户 id 与结果码。
* **轮换步骤**（写在这里，因为轮换是这条代价的对价）：
  1. 生成新的 `SECRET_KEY`，同步更新 Dify 的 `.env` 与代理的配置；
  2. 同时重启 Dify（api/worker）与代理 —— **必须一起**，否则一边签一边验会全部 401；
  3. 重启后所有既有会话失效（用户重新登录），这是期望行为，不是故障；
  4. 注意 Dify 用它加密的**存量**字段（工具凭据）需要按 Dify 自身的方式迁移 —— 若 Dify
     1.17.1 无该迁移路径，则本步骤是一次**破坏性**操作，需先在快照上演练。
* 这条代价是**用户明确选择的**（2026-10-14）。

## 被否掉的替代形态（否定理由保持有效）

* **代理持随机密码走官方 `/console/api/login`（PRD 原方案）**：Dify 无接口设密码，方案不可实现。
* **让合成邮箱可投递、走官方重置流程**：不引入特权 key，但要新增一块邮件基础设施、
  每账号一个真实地址；在自签可行且用户已选的前提下不再是首选。
* **回环开放的 `POST /console/api/register`**：不碰 `SECRET_KEY`，但要打开 `ALLOW_REGISTER`
  （PRD 明确保持 false），且注册路径分支多于邀请。
* **fork Dify / 加 OIDC**：PRD Out of Scope。

## 后果

* **映射表里没有密码**（`dify_login_mappings`）：键是公司用户 id，附 Dify 账号 id、
  邮箱、权限码快照、状态、时间。这是原方案最大的一处简化。
* **失权封禁改变形态**（#779）：自签模型下没有"Dify 会话"可吊销，封禁是**我们自己的映射**
  状态（`status=disabled`），每次登录重读权限码。原 PRD 里"把 Dify 账号置为 banned"的
  做法不再需要 —— 没人拿密码进 Dify 第二次。
* **会话寿命**由代理签发时决定（`AIOPS_LOGIN_PROXY_ACCESS_MINUTES`，默认 60）：
  撤权后最晚一个访问令牌周期内失效，比原方案（靠 Dify 的 refresh 寿命）更短、更可控。
## 追加（#779）：失权封禁**不**去 Dify 做，落在映射状态上

票面原本写「把对应 Dify 账号置为 `banned`（Dify 在下一次请求即拒，已核实）」。实现时核实
这句话**不成立**，两处：

1. **没有写入口。** `AccountStatus.BANNED` 只在**读取**侧被检查（`load_user` 抛
   `Unauthorized`）；Dify 1.17.1 的控制台 API 里**没有任何 moderation / ban 接口**
   （grep `banned` 只命中 `error.py` 的错误定义与 oauth 的提示文案）。
2. **退路不可逆。** 「把成员从工作区移除」（`remove_member_from_tenant`）会把该成员的
   app / dataset **转给 owner**，而重新加入的条件是 `status == PENDING or RBAC_ENABLED`；
   本实例 **`RBAC_ENABLED=False`**，于是一个已激活账号被移除后，重新邀请**静默不生效**
   ——不建 join、也抛不出 `AccountAlreadyInTenantError`。把它当封禁手段等于：永久失去该成员，
   并且悄悄改掉他的 app 归属。

**所以封禁落在 `dify_login_mappings.status`**（我们自己的一张表），语义与票面一致：

* 每次登录重读权限码；**失去**编辑权限码 ⇒ 拒绝，并把此前 active 的映射置为 `disabled`，
  审计记 `revoked`。
* **重新获得**权限码后再登录 ⇒ 放行，状态改回 `active`，审计记 `reactivated`（可逆）。
* 公司登录**失败**（密码错等）**绝不**改变状态 —— 一次手误不该变成一次锁定。
* **活着的会话随之失效的边界是令牌寿命**：会话无状态，代理不在请求路径上。撤权窗口 =
  `AIOPS_LOGIN_PROXY_ACCESS_MINUTES`（默认 60 分钟，比公司令牌 960 分钟短一个量级）。
  `access` 与 `refresh` 签成同一寿命，所以这个窗口是**硬**的，不是"两个中较短的那个"。

* **账号仍必须是 `active` 且是该工作区成员**：`load_user` 对 `banned` 直接 401，
  `account_initialization_required` 对 `uninitialized` 拒绝。开户仍要真的做。
* 公司用户仍**只给 editor**；`SECRET_KEY` 只在代理内，不进浏览器。
