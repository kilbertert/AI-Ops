# M3 正常登录用户入口验收

适用：已有授权测试账号，要证明它能以本人身份查询充电状态；也用于交接 App 接线。
先读 [当前状态](current-delivery-state.md) 与 [验收记录](../validation.md) 首节。
本页给步骤，实际通过范围以当次记录为准。

## 1. 业务目标与当前边界

公司身份体系负责身份和权限，平台负责编排，指定环境的受控节点读取业务数据。
公司租户、平台工作区、执行环境分别处理；一次请求只有一个编排主体。
M3 已证明构造身份下的真实模型工具循环；本片补正常登录身份的验收准备，
不把现有统一助手替换成 M3。

| 链路 | 已有证据 | 仍缺什么 |
|---|---|---|
| H5 登录 → thirdSession | 授权测试账号的登录接口成功；会话与登录主体一致 | 未操作完整 H5 页面 |
| 公网入口 → Gateway | third-session 转 X-Third-Session，服务端注入 Bearer；快捷动作 200 | 本轮走原 HTTPS 源在宿主回环解析，不证明开发机公网可达 |
| 正式解析器 → consumer/self | 正式 RedisThirdSessionResolver 解析一致 | 该账号当天新注册，`user_id` 与 `consumer_id` 两列都 0 单（见下） |
| 本人订单 → M3 委派 | 本地真 HTTP 与正式解析器正负用例 | 真实登录身份下的完整模型链路待样本 |
| 平台 → 节点 → 领取与完成 | 既有构造身份成功 run 的 succeeded/matched | 不冒充正常登录来源 |
| App 页面 → M3 | 已观测 App 调用助手/诊断；指定日志窗口 delegated 零命中 | 当前没有已证明的 M3 页面接线；生产四开关仍关闭 |

服务端 M3 路由是 `POST /v1/orders/{order_no}/charging-status/delegated`，请求体 `{}`。
成功需同一个 run 的回执、快照、记录和执行配置身份全部对上。
工具输出 `confirmed=true` 仅表示回执与记录/日志观测的判据通过，
`login_provenance_verified=false` 始终保留：文件内容不能证明其来自正常登录。

## 2. 先取得明确授权的登录样本

1. 从受控记录确认账号获准用于本次验收；通过该环境的正常 H5 登录接口取会话。
   登录接口路径为 `/mallapi/userinfo/phone/login`；请求形状取部署 H5 的登录脚本或网络面板，
   手机号与验证码运行时读取，不写在命令行、报告或仓库。
2. 只保存该次会话到操作者私有的 0600 普通文件；保留时间、HTTP/业务码、
   登录主体与会话主体是否相同等摘要。不要扫描 Redis 挑选客户会话。
3. 访问主机使用 [dev-host 声明](dev-host-declaration.md)：`dev-host show 41` 后，
   `dev-host exec 41 --allow-service-exec -- '<只读核对命令>'` 会校验主机身份。
   配置位置及服务账号见 [41 runbook](env-41-runbook.md)。以服务账号运行正式解析器，
   带正确 `PYTHONPATH`，只构造解析器；**不构造生产 create_gateway_app**，它会回收在飞作业。
   凭据经标准输入交给核对进程，程序只输出布尔判据。
4. 只解析这条指定会话，确认平台 consumer、scope=self、非 delegated，且登录主体/租户一致。
   本人订单读取用只读事务和参数绑定，谓词必须覆盖现行工单规则：

   ```sql
   SELECT order_no FROM ch_order_info
   WHERE tenant_id = %s
     AND (CASE WHEN user_id = '-1' THEN consumer_id ELSE user_id END) = %s
   LIMIT 1
   ```

   参数来自已解析的会话，不能用请求头或手机号代替。零行即待样本，不改订单归属，
   不切 operator 放宽范围。确认存在后再调用 `/v1/orders/{order_no}/access` 验证授权，
   订单号保存为单独的 0600 文件。

   **零行要先分清是「口径不对」还是「这个账号本就下单」。** 2026-10-21 的复核里两者都查过：
   授权测试账号的登录响应是 `justNowRegister: 1`（当天 11:36 创建），只读核对
   `user_id` 与 `consumer_id` 两列**都是 0 行**，而同租户有 182 张单。这时再查同一个账号
   不会有结果 —— 需要换一个确实充过电的授权账号。同租户那 182 张单属于其他 C 端用户，
   拿它们当正例只会走到 `_require_visible_order` 的 404（那是负例）；
   改订单归属或放宽 scope 是伪造业务数据，不做。
5. 公网请求用 `third-session`；直达 Gateway 用 `X-Third-Session` + 服务端 Bearer。
   前端不持有服务令牌。缺会话须 401，非本人订单须 404，不能自动换身份重试。
   如开发机网络不通，可在入口宿主通过 `curl --resolve <原域名>:443:127.0.0.1`
   保留原 HTTPS 源和证书校验，敏感头经 `-H @-` 输入；报告必须注明这一网络位置。

## 3. 文件预检（零网络）

工具复用 `live_replay.py` 的私有文件检查、发送占位与证据白名单，服务令牌从
隔离配置根的 `gateway/gateway.env` 读取，非空时优先于 `gateway/runtime.env`。
工具不连接 Redis 自选会话、不生成 JWT、不替换 Gateway 解析器。

```bash
python3 tools/m2_close/m3live/user_entry.py preflight \
  --session-file <0600会话文件> --order-file <0600订单文件> \
  --stack-config <隔离配置根> --out <新私有证据目录>
```

预检只证明输入文件合法，不能证明会话仍有效、栈已起或订单属于本人。
退出码：0 本命令判据通过，1 运行判据不成立，2 前置/观测不完整，3 安全拒绝。
凭据不接受命令行明文；base URL 只接受回环源，拒重定向且不使用环境代理。

## 4. 有样本后执行一次真实链路

前提：部署件来自合并修订；隔离配置和状态根已批准；按
[隔离栈 README](../../deploy/m2-live-03/README.md) 完成制品、TLS、两侧清单预检。
**正式 `compose.yaml` 已使用 gateway_server；清空 M2LIVE_COMPOSE_EXTRA，不能加载
compose.identity.yaml 或 synthetic 覆盖层。** 从 `compose config` 核对最终入口、挂载及
配置根，确认服务令牌与 Redis 指向已批准的环境。不要靠“日志没有 substituted”代替这一步。

在宿主的部署制品目录，用同一套根和项目名执行（占位值须先由受控记录填好）：

```bash
(
set -euo pipefail
export M2LIVE_CONFIG='<隔离配置根>' M2LIVE_STATE='<隔离状态根>' M2LIVE_PROJECT='<隔离项目名>'
unset M2LIVE_COMPOSE_EXTRA
M3_EVID='<新私有证据目录>'
M3_SESSION_FILE='<0600会话文件>'
M3_ORDER_FILE='<0600本人订单文件>'
python3 tools/m2_close/m3live/user_entry.py preflight \
  --session-file "$M3_SESSION_FILE" --order-file "$M3_ORDER_FILE" \
  --stack-config "$M2LIVE_CONFIG" --out "$M3_EVID"
python3 tools/m2_close/m3live/live_replay.py baseline --out "$M3_EVID/prod-before.json"
finish() {
  rc=$?
  trap - EXIT
  bash deploy/m2-live-03/checks.sh down || rc=1
  python3 tools/m2_close/m3live/live_replay.py cleanup --out "$M3_EVID" \
    --prod-baseline "$M3_EVID/prod-before.json" || rc=1
  exit "$rc"
}
trap finish EXIT
bash deploy/m2-live-03/checks.sh up
python3 tools/m2_close/m3live/user_entry.py run \
  --session-file "$M3_SESSION_FILE" --order-file "$M3_ORDER_FILE" \
  --stack-config "$M2LIVE_CONFIG" --state "$M2LIVE_STATE" --out "$M3_EVID"
)
```

`run` 在网络前原子占位，同目录最多一条 POST。失败、超时或连接中断均不自动重发。
失败回执可能没有 run_id；按隔离栈 README 的唯一归属核对方法找记录，不能取“最新一条”。
工具校验 HTTP 200、同 run 的 succeeded、快照存在、expected/observed/回执身份逐字相同、
matched、日志读取成功且无构造身份声明。它不证明快照业务正确性或精确模型调用数。
快照须再按同订单的只读业务查询核对；真实拒绝用例须留零派发/领取证据。
完成后保存制品身份、时间、网络位置、判据与清理结果；公开材料只写脱敏结论。

## 5. App 接线的最小待办

| 归属 | 工作 | 完成判据 |
|---|---|---|
| 业务验收方 | 指定授权测试账号及本人订单 | 正常登录后 access 200；不借他人会话 |
| AI-Ops / 部署负责人 | 正式身份的隔离链路验收，随后单独决定生产启用 | 同 run 成功、身份一致、快照正确、清理完成 |
| App / BFF | 为本人充电状态增加明确入口，转发现有登录态与 consumer 入口 | 页面实际请求 M3，显示结构化状态；401/404/失败可辨认 |
| 平台负责人 | 后续编辑隔离、发布恢复与多环境交付 | 分别验收；现有单次成功不替代它们 |

App 仍走 `/v1/assistant/*` 时，不宣称它已经调用 M3。本片不改统一助手默认路由，
也不把后台运营商权限借给 consumer 场景。浏览器点击、App 展示与公网启用各自留证。
