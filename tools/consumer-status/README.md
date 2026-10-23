# 本人订单状态验收工具（只读）

三个脚本 + 一条命令链。**它们只做取证，不改任何业务状态**：零模型调用、
零诊断作业、不写订单。

> 本目录的第一版有四个**误报**（判据比事实宽，证据不足时仍退出 0）。
> `tests/test_consumer_status_tools.py` 逐条钉住它们，并做过变异复验
> （把判据改回旧形状 ⇒ 对应反例转红）。这组测试是**工具自检**，
> 不是真实业务验收，也不是真实越权验收。

## 退出码（三个脚本统一）

| 码 | 含义 |
|---|---|
| `0` | 通过 |
| `1` | 判据不过（跑了，但结论是否定的） |
| `2` | 前提缺失 / 未验证（拿不到证据） |

**`2` 不是 `0`。** "没验成"与"验过了"是两件事，这是这套工具存在的理由。

## 命令链

全部在 **41 主机上以服务账号**运行（`PrivateTmp` 让 `/tmp` 落盘的文件
`runuser -u aiops41` 读不到，所以会话走环境变量）：

```bash
# 0) 把脚本送上去（产物身份，不是手工 scp）
dev-host write 41 /tmp/consumer-status/check_identity.py \
  --artifact-sha256 "$(sha256sum tools/consumer-status/check_identity.py | cut -d' ' -f1)" \
  < tools/consumer-status/check_identity.py
#    …independent_cpms.py / status_probe.py 同法

# 1) 前提：身份、范围指纹、本人订单（退出码 0 才继续）
ssh aiops-41 'cd /opt/aiops-41 && runuser -u aiops41 -- env \
  $(tr "\0" "\n" < /proc/$(systemctl show -p MainPID --value aiops-gateway-41)/environ \
    | grep -E "^AIOPS_" | xargs -d"\n") \
  AIOPS_CHECK_SESSION="$SESSION" /opt/aiops-41/.venv/bin/python /tmp/consumer-status/check_identity.py'

# 2) 固定预期：最新本人订单 + 它当前的 CPMS 状态（这一步不比对）
#    ……开受控窗口（只改三项个人查询配置）……

# 3) 两条真实页面正例（各自从干净上下文发起）
python tools/consumer-status/status_probe.py <订单号> latest    --state-dir <库外私有目录>
python tools/consumer-status/status_probe.py <另一订单号> specified --state-dir <库外私有目录>

# 4) 独立核对：把页面给出的订单号/来源/状态码交回去比对
python tools/consumer-status/independent_cpms.py \
  --expect-order <订单号> --expect-source cpms.chargingInfo --expect-status-code <码>

# 5) 恢复配置，再验历史快照回读被拒
```

`$SESSION` 是真实 thirdSession（**凭据本身**）。不要打印、不要写进仓库、
不要重定向进会被提交的文件。

## 每个脚本判什么

### `check_identity.py`

1. 生产 `RedisThirdSessionResolver` 接受这个会话；
2. `scope_type == "self"` **且** `delegated == false` **且** `subject.c_user_id` 非空；
3. `aiops:diagnoses:write` 与 `aiops:orders:read` 两次解析的 **`scope_fingerprint` 相同**；
4. 该主体有本人订单。

1–3 任一为假 ⇒ **退出 1，且不进业务查询**。第 4 条为假 ⇒ **退出 2**
（没有本人订单就没有验收前提，不能报通过）。

### `independent_cpms.py`

两件事分开说：

- **固定预期**（`--expect-*` 都不给）：只取"最新本人订单 + 当前 CPMS 状态"，输出
  `comparison.consistent = null`，**不声称验过**。
- **独立核对**（给了 `--expect-*`）：独立再读一次 CPMS，比对订单号、来源、状态码。
  不一致 ⇒ 退出 1。

端点走 `charging_endpoint(settings.charging_status_base_url, tenant_id)`，**按租户解析**；
缺映射是 `TENANT_ENDPOINT_UNRESOLVED` ⇒ 退出 2，**绝不回退运维 OPS 地址**。
用户身份取 `caller.subject.c_user_id`（不是不存在的 `caller.user_id`）。

排序判据是独立的：对本人可见行比较 `(created_time, order_no)` 是否**严格递减**。
"同一个 `list_orders` 换个 `page_size` 首行相同"**不算**排序证明 —— 那两次调用走的是
同一个 `ORDER BY`，只能证明选择器是确定的。

### `status_probe.py`

判据在 `judge()` 这个纯函数里，因此合成反例能直接驱动它：

| 判据 | 为什么 |
|---|---|
| `exactly_one_post` | 重复提交会产生两个作业 |
| `request_has_no_order_no`（latest） | 带了号，"最新"就会被那张单覆盖 |
| GET **必须**是本次 POST 返回的那个 `qa_id` | 旧版回退到任意 GET ⇒ 读到上一轮也算过 |
| 200 + JSON + `no-store` | 个人业务结果不缓存；HTML 200 不是成功 |
| 回答块里有订单号、`cpms.chargingInfo`、观察时间 | **从回答块取，不从整页取** |
| 助手区域文本与回答块一致 | 整页文本会命中**用户自己刚发的提问** |
| 无"无法查询"类话术 | 那是回落到普通模型路径的形状 |

隧道：别名取自 `dev-host show <target>` 的 `ssh alias` 字段（主机身份的唯一来源），
端口先探空、起来后再确认**是本进程在听**，结束时只清理自己启动的那个。

### 一处仍未做到的

`status_probe.py` 的助手区域选择器（`ASSISTANT_SELECTORS`）是**按类名猜的**，
没有对着已部署产物逐一核过；取不到就判失败（不退回整页）。真实复跑前应先用
`--state-dir` 跑一次并看 `page_excerpt_tail`，确认选择器命中。
