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
