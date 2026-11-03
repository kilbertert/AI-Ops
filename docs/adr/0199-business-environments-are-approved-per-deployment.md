# 199. 业务环境的批准从「按租户」上移到「按部署」

日期：2026-11-03
状态：已采纳

## 问题

「复制并开通」一个新客户，客户端客服这一端在第一步就失败：

```
客户端客服：该客户的业务环境连接尚未配置，请联系平台管理员
```

`SETUP_BUSINESS_CONNECTION_REQUIRED`。判据是 `approved_endpoint()` 在
`AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL` 里找这个租户的条目 —— 那是一个**按租户映射**，
没有条目就拒绝。

于是「一键开通」的真实步骤是：改 `/etc/aiops-41/gateway.env` → 重启网关 → 回页面点重试。
页面能建工作区、装插件、配模型、复制应用、申请凭据、发布路由，却卡在一条只有运维能改的
字符串上。这不是"设计如此"，这是一步没有被搬到产品里的部署操作。

## 取证（2026-11-03，41 上只读）

- `cloud_charging_pile.ch_order_info` 有 **67 个租户**，优链充 695 单、Ulink 252 单，
  同一个库；调用是同一个 host + `tenant-id` 请求头（`data/charging_status.py:34`）。
  **不存在"每个租户一个业务主机"这回事。**
- 公司租户目录里没有业务地址：`qumall_upms.sys_organ` 359 行 60 列逐列核过，
  `biz_data`/`biz_name` 全 NULL、`second_domains` 全 NULL、`home_url` 是网站域名；
  `sys_datasource`（唯一同时有 `url` 与 `tenant_id` 的表）**0 行**。
  ⇒ 地址不能从公司目录推导，只能由平台部署侧声明。

## 决策

**新增 `AIOPS_GATEWAY_BUSINESS_ENVIRONMENTS_FILE`：平台已批准的业务环境清单（0600 JSON）。**

`approved_business_endpoint()` 的顺序是：

1. `AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL` 里**按租户登记的条目**（权威覆盖，运维钉死的优先）；
2. 否则取清单里已批准的地址。

清单只有一项 ⇒ 自动用，零点击；多项且无 `default: true` ⇒
`SETUP_BUSINESS_ENVIRONMENT_AMBIGUOUS`，**不替客户挑**。

失败形状：

| 情形 | 码 |
|---|---|
| 环境变量没登记、清单为空 | `SETUP_BUSINESS_CONNECTION_REQUIRED`（与今天逐字相同） |
| 清单多项且未声明默认 | `SETUP_BUSINESS_ENVIRONMENT_AMBIGUOUS` |
| 清单文件坏 / 重复 id / 两个默认 / 非法 URL | **启动失败**，不留到第一次开通 |

## 为什么不直接把映射表删掉

现役 Ulink 是运维钉死的一行。删表等于让"谁负责这个租户的地址"变成"平台配置文件的当前
内容"，而将来真出现第二套部署时，召回每一个现役租户的反向依据就没了。保留覆盖的代价是
一次 `getattr` 查表，收益是运维可以永远不改清单就把某个租户钉在别的地址上。

## 为什么不能从开通请求里传地址

那等于按客户给的任意 URL 去取业务数据，而客户是**被开通方**。批准必须留在部署侧，客户
能选的只有"开哪一端、从哪个已发布来源复制"。

## 被否掉的替代形态

- **开工单让运维加一行环境变量**：今天的现状，正是它让"一键"名不副实。
- **从 UPMS 推导**：取证结果是那里没有地址；写一个"取不到就回退"的推导等于把空值当批准。
- **清单多项时取第一个**：把文件顺序变成语义，缺陷表现是"给 A 客户接了 B 客户的业务系统"，
  而页面显示成功。
- **全平台默认地址（清单都不要）**：只有一个环境时行为与清单完全一样，但第二个环境出现时
  "谁属于哪个"会变成猜。

## 验收

`tests/dify/test_consumer_dependencies.py`：未钉死的租户走清单并登记；已钉死的租户仍用自己的
地址；多项无默认拒绝；有默认则解析；坏清单在 `validate()`（启动）就失败。
本机全量 `pytest`、`ruff check`/`format --check` 通过。真实开通验收另记在
`docs/validation/`。
