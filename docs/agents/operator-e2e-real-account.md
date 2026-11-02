# 以真实运营账号跑 41 的端到端验收（2026-11-02 实测定型）

> **读者**：要证明「管家端某个改动在真实用户路径上生效」的人。
> **本文替代**此前「operator 入口拿不到会话，只能标待验证」的结论 —— 那条是**错的**：
> 账号凭据一直在库外加密清单里，只是没被找到。
> 配套：ADR-0193（费用字段）、`docs/validation/2026-11-02-fee-fields-deploy-15382378.md`（本次回执）。

## 0. 一句话

真实账号登录公司 UPMS 拿**公司签发**的令牌 → 直接打 41 的网关本机端口、显式带
`X-Business-Entry: operator` 与 `X-AIOps-Source-Key` → 判据是 `agent_runs` 的
`chat_binding` 与 `order_no`，**不是**回答好不好看。

## 1. 为什么不能只做本地单测

`/v1/assistant/questions` 的 operator 入口要求公司令牌；一次性数据根那个技能
（`verify-aiops-gateway`）碰不到它。2026-11-02 实测：本地全绿 + CI 全绿 + 镜像可导入，
同一句用户原话在线上 **3 次里 2 次整轮失败**（`reason=unsupported_fact`）。**只有真实链路能发现它。**

## 2. 身份：真实账号登录（不是自签）

凭据在库外加密清单，**不在**仓库、不在会话记录：

```bash
age -d -i ~/.config/dev-host/secrets-age/key.txt \
  ~/.config/dev-host/secrets-age/secrets.age | grep '"asset_id"'
```

管家端要的是 `kind == "account+password"`（本次是 `aiops-ulink-account-20261030`，
账号 `ulink`，租户 `1942105476598861824`）。`phone+fixed-code` 是消费者形态，别混用。

登录端点与形状（来自管家 H5 产物 `index.*.js`，不是猜的）：

```
GET  https://api.mall.qushiyun.com/code?randomStr=<11 位>          # 取验证码图
POST https://api.mall.qushiyun.com/upms/token/login
       ?username=&password=&randomStr=&code=&grant_type=password&scope=server&deviceType=shop
```

三个**必须**记住的坑：

| 坑 | 症状 | 真因 |
|---|---|---|
| 口令不加密 | **HTTP 500** | 网关 `PasswordDecoderFilter` 先 AES 解密，明文直接抛异常。这就是"手拼必 500"的真因 |
| 去 `data` 里找令牌 | 有 200 却拿不到 token | 令牌在**响应顶层**：`{access_token, refresh_token, tenant_id, id, type, …}` |
| 以为要输验证码 | 428/`验证码错误` | `/code?randomStr=` 的图与请求来源无关；**把图里的字符原样提交**即可（OCR 或人眼） |

AES：`AES/CBC/NoPadding`，**key == IV == `1234567891234567`**，base64 后放 query。
密钥在**本机**加密，不传到服务宿主。

## 3. 打哪个入口

**直接打 `172.18.0.1:8788`**（网关本机监听），显式带：

```
Authorization: Bearer <真实令牌>
X-Business-Entry: operator
X-AIOps-Source-Key: <值从网关进程环境读，不打印、不落盘>
```

取来源密钥（生产由 nginx 注入，前端不持有）：

```bash
PID=$(systemctl show aiops-gateway-41 -p MainPID --value)
tr '\0' '\n' < /proc/$PID/environ | grep '^AIOPS_GATEWAY_COMPANY_SOURCE_KEY=' | cut -d= -f2-
```

**不要走管家 H5 的 nginx**：`/etc/aiops-41/operator-h5-proxy.inc` 会把 `X-Business-Entry`
硬写成 `operator` 并清空 `X-Third-Session`。在那儿测入口头等于没测。

## 4. 判据：读 `agent_runs`，不是读正文

```python
sqlite3.connect("/var/lib/aiops-41/gateway/gateway.db")   # 以 aiops41 身份读
SELECT run_id, order_no, state, chat_binding, exec_config_state, route_revision, scope_json
FROM agent_runs WHERE qa_id = ?                            # 按 qa_id 取，别取最新一行
```

一次**通过**的订单诊断长这样：

| 字段 | 期望 |
|---|---|
| `chat_binding` | `operator_order_diagnosis:production-41:<租户>`（不是 consumer、不是 0 行） |
| `order_no` | 等于问句里那张单（这条能抓出"日期被当成单号"） |
| `state` / `exec_config_state` | `succeeded` / `matched` |
| `scope_json.caller.user_type` | `1`（租户主账号） |

**两处会骗人**：

- `order_no` 只看不为空不够 —— 站点名里的日期也会填进去，必须**逐字比对**。
- 失败 run 的 `snapshot_json.answer` 是 `null`（模型输出没被保留），所以**不能**靠读库
  复盘"模型到底写了什么"。要看原因就读网关日志的 `reason=`：
  `json_parse`（模型没吐 JSON）/ `unsupported_fact`（事实段外的行出现数字）/ `text_field`。

## 5. 一次完整跑法的骨架

```bash
# ① 登录（凭据与密钥只在本机）
python3 -I path/to/ulink_login.py          # 产物：/tmp/.ul_token（0600，不打印）

# ② 发问 + 轮询 + 取路由证据
python3 -I path/to/probe_order_e2e.py "帮我分析这笔单的电费2116050516255272961"
```

两者都要求：**开发机到公网入口不通时，用 ssh 到 41 发请求**（41 直达 `api.mall`）。
脚本只把令牌经 ssh stdin 传给 41，不写进 argv、不落盘到仓库。

## 6. 已知陷阱

- **裸打公网 CPMS 会得到 `订单不存在`**。网关走的是
  `/v1/internal/operator-runs/{run}/cpms/…` 代理，自带用户令牌与 `tenant-id`；
  这两条腿不能混用。`{run}` 在你复用时已过期，会得到 `OPERATOR_GRANT_REJECTED`。
- **提示词冻结在运行清单里**（`agent_runs.execution_manifest.system_messages`）。
  改 `OPERATOR_OUTPUT` 之类的协议文本，**必须部署 Gateway 并重新发布**才生效；
  只改代码不改发布，线上一点变化都没有。
- **别用几次调用比成功率。** 3 次、6 次、8 次在这个系统上都不够：同一契约
  （rev 12）按小时的实测成功率是 `100% / 0% / 12% / 25% / 44%`，**极差 100pp**。
  波动来自模型/供应商侧，盖过契约差异。要比「改前 vs 改后」，必须**同期对照、
  至少 20 次**，并把两次的时段写进记录。只跑一两次就下方向性结论，
  两次都是错的 —— 本仓库已实测踩过（把 0/8 当罪证、把 1/4 当回滚有效）。
- 别拿"能跑通一次"当"稳定可用"。本轮 `completed` 只证明链路通，稳定性另计。
