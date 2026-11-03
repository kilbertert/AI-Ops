# 平台已批准的业务环境清单（gateway.env 增补片段）
#
# 目的：让"复制并开通"一个新客户不再需要改服务器环境文件并重启网关。
#
# 现状（2026-11-03 取证）：业务地址只有一份部署。
#   41 的 cloud_charging_pile 里有 67 个租户共用一个库，
#   租户由请求头 `tenant-id` 区分，不是每租户一个主机。
#   UPMS sys_organ 359 行没有任何业务地址列（biz_data/biz_name 全 NULL、
#   sys_datasource 0 行、home_url 只有网站域名）⇒ 地址不能从公司目录推导。
#
# 语义：
#   AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL 仍是权威覆盖 —— 登记了某租户就用它的地址。
#   本清单只接住环境变量里**没有登记**的租户（新客户）。
#   清单为空 = 保持今天的行为（未登记租户 SETUP_BUSINESS_CONNECTION_REQUIRED）。
#   清单多项且无 default:true = SETUP_BUSINESS_ENVIRONMENT_AMBIGUOUS，不替客户挑。

# 清单文件路径（0600 JSON，经 dev-host write 产物门写入）
AIOPS_GATEWAY_BUSINESS_ENVIRONMENTS_FILE=/etc/aiops-41/business-environments.json

# 清单文件内容（参考样例；实际值以受控位置为准，文档不写明文）
# {
#   "schema": "aiops-business-environments/1",
#   "environments": [
#     {"id": "cpms-41", "base_url": "https://<当前 AIOPS_GATEWAY_OPS_CHARGING_STATUS_BASE_URL 的地址>"}
#   ]
# }

## 启用步骤（41）

1. 写清单（产物门，内容取自已批准端点，不在文档里写明文值）：

```bash
printf '%s' '{"schema":"aiops-business-environments/1","environments":[
  {"id":"cpms-41","base_url":"<与 AIOPS_GATEWAY_OPS_CHARGING_STATUS_BASE_URL 同一地址>"}]}' \
  | dev-host write 41 /etc/aiops-41/business-environments.json \
      --artifact-sha256 <新文件 sha256>
```

2. 在 `/etc/aiops-41/gateway.env` 追加一行（同样按产物门写，带 `--if-sha256`）：

```dotenv
AIOPS_GATEWAY_BUSINESS_ENVIRONMENTS_FILE=/etc/aiops-41/business-environments.json
```

3. 只 `systemctl restart aiops-gateway-41`；`/health` 回读版本与启动成功即生效。
   启动失败说明清单不可用（权限不是 0600、schema 不对、URL 非法），按错误信息修清单，
   不要先用空键启动再补。

4. 开通验证：对目标租户重新点「复制并开通」，客户端这一端应不再出现
   `SETUP_BUSINESS_CONNECTION_REQUIRED`；回读 `consumer_endpoints` 应有
   `(目标租户, production-41, 该地址)` 一行。

## 回滚

清空 `AIOPS_GATEWAY_BUSINESS_ENVIRONMENTS_FILE` 只**停止新的自动登记**：已经写进
`consumer_endpoints` 的 `(租户, 环境) → 地址` 行仍然生效，运行期照旧读它。
要让某个租户失去业务连接，必须同时删掉那一行 —— 这是运维动作，不是改配置的副作用。
