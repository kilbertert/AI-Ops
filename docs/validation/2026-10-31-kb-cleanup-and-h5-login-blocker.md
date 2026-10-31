# 孤儿知识库清理与原 H5 登录根因（2026-10-31 深夜）

承接 [收尾记录](2026-10-31-dual-entry-wrap-up.md)。本文补两项：**已知孤儿库的清理结果**，
以及**原 H5 真实消费者登录失败的完整根因**（上一份只写到"SQL 错误"）。

## 1. 孤儿知识库：实际 4 个，已清理

上一份记录写"8 个孤儿"是错的。逐个核对引用后：

| 知识库 | 文档/分段 | 被谁引用 | 处置 |
| --- | --- | --- | --- |
| `497eb04cd51d…` 对客知识库-8f442002… | 0 / 0 | 无 | **删除** |
| `ed1c1412d51b…` 对客知识库-77d537c9… | 0 / 0 | 无 | **删除** |
| `c1edcc54d51b…` 对客知识库-86a94141… | 0 / 0 | 无 | **删除** |
| `4129e1b6d51b…` 对客知识库-60eea693… | 0 / 0 | 无 | **删除** |
| `188c54f4d521…` 对客知识库-a9a6bd4c… | 0 / 0 | **现役客户端路由**（`agt_22ce3c9a…` v1） | 保留 |
| `38676440d37c…` shiping1 | 1 / 17 | 现役 FAQ agent v9 | 保留 |
| `d340d150d2b3…` zhishiku | 1 / 28 | 现役 FAQ agent v9 | 保留 |
| `3064598cd01c…` WF01-C端只读指导-审核V1 | 61 / 61 | 现役 FAQ agent v2–v9 | 保留 |
| `5c9313bccdfc…` 41(1) | 9 / 60 | 营销 agent；`dify_knowledge_bindings` 两条 | 保留（`allowed=false`） |

判据（删除前逐个跑）：**0 文档 且 0 分段 且 无 agent 引用 且 无 agent 版本快照**。
四条创建时间正是本次四次失败开通的时刻（19:07 / 19:11 / 19:12 / 19:22），确认是本次产物。

删后同步把 4 条 `consumer_knowledge_grants` 置 `revoked`（保留历史行，不物理删除）。
页面复测：知识库使用范围由 9 个降为 **5 个**，其中 `allowed=true` 4 个。

> 成因待修：`_run_consumer` 在**每轮重试**里先建库再做事，失败后库留在空间里。
> 本轮只清理了既有产物，没有改代码 —— 下次失败还会再留一个。

## 2. 原 H5 登录失败：公司商城后端的注册缺陷

上一份只写到 `SQL错误！`。补全三个对照实验与真实根因。

**对照实验**（同一域名、同一 `tenant-id` 头，Redis 里先写入验证码）：

```
{phone}                          → 70009 验证码错误        （参数不全，属正常拒绝）
{phone, code}                    → 1 SQL错误！
{区号+真码}                       → 1 SQL错误！
{不存在的手机号, code}             → 1 SQL错误！             ← 与账号无关
```

**`cloud-mall-api` 日志的真实异常**：

```
### Cause: java.sql.SQLException: Field 'sysem_id' doesn't have a default value
java.lang.NullPointerException: Cannot invoke
  "com.qushiyun.cloud.weixin.common.entity.ThirdSession.setUserId(String)"
  because "thirdSession" is null

UserInfo(id=2116525247696560130, sysemId=null, tenantId=null, userCode=21830026,
         phone=13800138000, nickName=tourist138000, justNowRegister=1, ...)
```

链路：验证码校验**通过**之后，后端为这个手机号走"用户不存在则注册"，
插入 `UserInfo` 时 `tenant_id` 与 `sysem_id` 都是 null，`sysem_id` 无默认值 ⇒ 插入失败，
返回的 `UserInfo` 是个 `tenantId=null` 的半成品 ⇒ 写 `ThirdSession` 时 NPE。

**结论**：

- 不是我们的资产问题。用**不存在的手机号**得到同一个错误，说明任何手机号都过不去。
- 不是 `8888` 的问题。资产里写的"固定验证码 8888"与线上不符 —— Redis 里的键
  `ver_code_default::<phone>` 存的是随机 4 位码；但即使拿到真码，也撞在同一个 SQL 缺陷上。
- **资产的 `verification` 字段是陈旧描述**，应在修好商城后端后一并订正。
- 这是**公司商城后端**（`cloud-mall-api`）的缺陷，不在本仓范围。要么由商城侧修
  `sysem_id`/`tenant_id` 的落库，要么换一个已存在的用户凭据（本仓没有，也不应冒用在线客户会话）。

**因此 A–L 的 H / I / J1（真实消费者与原 H5、真实员工与原管家 H5）仍未执行。**

## 3. 当前生产状态（只读回读）

| 位置 | 状态 |
| --- | --- |
| 41 Gateway | `1.0.0+4ec55c7a1c0c`，`business_mutations=disabled`，health ok |
| 41 三个 unit | `aiops-gateway-41` / `aiops-node-runtime` / `aiops-node-callback` 均 active |
| Ulink 双端 | operator `published=true v1`、consumer `published=true v1`，两端 `channel=pending` |
| 36 控制台 | web = `aiops/dify-web-patched:dual-entry-merged-86d63d4a`；桥 = `e0864bdf` |
| 知识库 | 该租户 5 个，其中 4 个允许对客 |
| `origin/main` | 本次收尾文档合并后的提交 |

## 仍未做（按优先级）

1. **修 `_run_consumer` 的建库时机**：失败重试不再留下空库（本轮只清了产物）。
2. **原 H5 真实会话**：阻塞于公司商城后端注册缺陷。
3. **渠道声明**：需要真实渠道证据才能写 `[[channels]]`。
4. **订正资产描述**：`aiops-ulink-phone-20261030` 的 `verification` 仍写"固定验证码 8888"。
