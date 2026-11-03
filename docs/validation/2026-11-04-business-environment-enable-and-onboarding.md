# 业务环境清单在 41 上的启用与优链充开通复测（真实）

执行时间：2026-11-04 01:50–02:05（北京时间）。环境：41 生产网关。
制品：`1.0.17+010705a88d05`（PR #1329 合并后由 CD run 41687518920 部署）。
身份：平台账号真实 Dify 会话（`system` / `41-1454348737375174658@aiops-login.invalid`，
`platform=true`，328 个客户），经同源桥 `/__aiops/manage` 驱动。

## 一、启用（三处写入，均按产物门）

| 步骤 | 内容 | 结果 |
| --- | --- | --- |
| 清单 | `/etc/aiops-41/business-environments.json`，`aiops-business-environments/1`，一项 `cpms-41` | 0600，owner `aiops41`，sha256 `5dd96f2c…` |
| 配置 | `gateway.env` 追加 `AIOPS_GATEWAY_BUSINESS_ENVIRONMENTS_FILE=…` | `--if-sha256 3ad7d2ea…` → `8d00ff4a…`；备份 `gateway.env.bak-business-env-20261104`（`3ad7d2ea…`） |
| 重启 | `systemctl restart aiops-gateway-41` | active，`/health` 200 |

进程环境回读（`/proc/<MainPID>/environ`）确认三项同时在位：

```
AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL=1942105476598861824:https://api.mall.qushiyun.com/charging-pile
AIOPS_GATEWAY_BUSINESS_ENVIRONMENTS_FILE=/etc/aiops-41/business-environments.json
```

以服务身份（aiops41）用**运行中进程的环境**解析：`approved_business_endpoint(优链充)`
= `https://api.mall.qushiyun.com/charging-pile`；已钉死的 Ulink 仍解析到同一条（环境变量优先）。

## 二、开通复测（页面动作，同一作业续跑）

沿用页面 2026-11-03 那次留下的作业 `3d3eb447-56f8-4126-96dc-3d3d31a4abe6`
（`scope=both`，目标优链充 `1941847786056323072`，名称「优链充－客服与管家」）。

| 轮次 | 客户端这一端 | 说明 |
| --- | --- | --- |
| 启用前 | `failed / SETUP_BUSINESS_CONNECTION_REQUIRED` | 修复前的形状 |
| 第 1 次 retry | `failed / SETUP_STEP_FAILED`，但已写入 `business_endpoint`、`plugin`、`models`、`knowledge_base_ids`、`app_id`、`key_ref` | 业务连接这一步已通过；失败在其后的某一步 |
| 第 2 次 retry | **`ready`，`published=true`，`version_no=1`** | 作业整体 `completed` |

**第 1 次那个 `SETUP_STEP_FAILED`：归因未定，本记录不宣称根因。**

已确证的只有三条：

1. 该端资源的持久字段在**第 1 次 retry 内**就写齐了（`business_endpoint`、`plugin`、
   `models`、`knowledge_base_ids`、`app_id`、`key_ref`），所以失败发生在 `consumer:key`
   之后的 `consumer:register` / `consumer:publish` 之间。
2. Dify 上该应用**只有一枚** service key，创建时间 `2026-11-03T17:54:29Z`；key 文件
   `548b8483….key` 的 mtime 是 `2026-11-04T02:00:55+08:00`（同一时刻）。两枚时代不同
   意味着文件不是由"第一次 retry 自己建出的那枚"写入的 —— 它由第一次 retry 之后、
   第二次 retry 之前的一次独立操作写入。
3. 那次独立操作是**我做的**：为定位失败点，我在 41 上用服务身份单独调过一次
   `service_keys.ensure`（返回同一路径）。它自己就完成了 `_adopt`。

所以「第一次 retry 因为 `service_key_ops` 快照要等下一轮才对账」这个说法**没有证据**，
而且与 `service_keys.ensure` 的代码不符：`before=[]` 且列表恰好多出 1 枚时，同一轮就
`_adopt`。**第一次 retry 的真实失败点仍未知**，`SETUP_STEP_FAILED` 也没留下栈。

这也说明本片要修的那个可见性缺口是真的：一次失败在生产上**无法在事后**定位，
只能靠人手工复现，而复现动作本身会改变现场。

## 三、结果回读（库 + 接口）

| 项 | 值 |
| --- | --- |
| `consumer_endpoints` 优链充行 | `(1941847786056323072, production-41, https://api.mall.qushiyun.com/charging-pile, registered_by=setup)` ← **修复前不存在这一行** |
| `chat_route_bindings` | `consumer/dify/548b8483… active rev 1 v1`；`operator/node/a57602c1… active rev 2 v1` |
| `consumer_knowledge_grants` | 新建对客库 `9d856a66…` 已授权（`revoked_at` 为空） |
| 页面作业终态 | `completed`；客户端 `channel=pending`（"该端尚未接入使用渠道"） |
| 现役入口边界 | `/health` 200、`/v1/openapi.json` 200、`/v1/runs` 404、匿名问答 401 |

管家端保持 `draft_only`：优链充已有一条 active 的 operator 路由（`a57602c1`），
开通按 ADR-0189 只建草稿、不自动顶替正在服务的那一版。

## 四、仍未验

- **客户端真实问答**：新客户端应用 `548b8483…` 尚未有真实用户会话跑过一轮。
  渠道声明仍为空，页面按 `pending` 显示，这是有意的（没有渠道就没有"已接入"）。
- **管家草稿切正式**：需要在页面上显式"发布到管家"，是一次切流量动作，本轮未做。
- **失败原因不可见**：`SETUP_STEP_FAILED` 是 `_error_code()` 对非受控异常的兜底，
  页面只显示通用码，journal 里也没有栈（`self_service.py` 捕获后不记栈）。
  第二轮那次的真实失败点因此**至今未知**（见第二节第 1 次 retry 一节）。
  可见性修复见 PR #1331。
