# 本人订单状态验收工具（只读）

三个脚本 + 一条命令链。**它们只做取证，不改任何业务状态**：不开诊断作业、不写订单。

⚠️ `status_probe.py` **会向网关提交一个真实问题**。它保证的是"走确定性路径、不触发
模型循环"，而不是"零网络影响"——真实运行前必须先满足启用与路由前提（见第五节）。
另外两个脚本是纯只读。

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

**三个脚本的运行位置不同，别混写：**

| 脚本 | 跑在哪 | 为什么 | 凭据来源 |
|---|---|---|---|
| `check_identity.py` | **41**，服务账号 `aiops41` | 要读 Redis 会话与业务库，凭据只在那个账号下 | 环境的 `AIOPS_*` + `AIOPS_CHECK_SESSION` |
| `independent_cpms.py` | **41**，服务账号 `aiops41` | 同上（SQL + CPMS） | 同上 |
| `status_probe.py` | **开发机** | 它驱动真实浏览器，且用 `dev-host` 校验主机身份 | `--state-dir` 里的登录态；隧道经 `dev-host` 校验后再开 |

`PrivateTmp` 让 `/tmp` 落盘的文件 `runuser -u aiops41` 读不到，所以**会话走环境变量**
而不是文件。

```bash
# 0) 把**要在 41 上跑的**脚本送上去（产物身份，不是手工 scp）：
#      check_identity.py / independent_cpms.py / **private_output.py**
#    `independent_cpms.py --out ...` 会按同目录加载 private_output.py ——
#    漏拷它会在写证据时 FileNotFoundError（Codex 复现过）。
#    status_probe.py 留在开发机跑（要驱动浏览器），它也从同目录加载 private_output.py。
for f in check_identity.py independent_cpms.py private_output.py; do
  dev-host write 41 "/tmp/consumer-status/$f" \
    --artifact-sha256 "$(sha256sum "tools/consumer-status/$f" | cut -d' ' -f1)" \
    < "tools/consumer-status/$f"
done
#    status_probe.py / verify_tunnel.py / replay_*.py 都在开发机上跑，不用送。

# 1) 前提：身份、范围指纹、本人订单（退出码 0 才继续）
ssh aiops-41 'cd /opt/aiops-41 && runuser -u aiops41 -- env \
  $(tr "\0" "\n" < /proc/$(systemctl show -p MainPID --value aiops-gateway-41)/environ \
    | grep -E "^AIOPS_" | xargs -d"\n") \
  AIOPS_CHECK_SESSION="$SESSION" /opt/aiops-41/.venv/bin/python /tmp/consumer-status/check_identity.py'

# 2) 固定预期：最新本人订单 + 它当前的 CPMS 状态（这一步不比对）
#    ……开受控窗口（只改三项个人查询配置）……

# 3) 两条真实页面正例（**在开发机上**执行；各自从干净上下文发起）
#    主机身份由 dev-host 校验（脚本内部先跑一次 `dev-host exec … hostname`）
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

排序判据是 `(created_time, **id**)` 严格递减，**与生产 `ORDER BY created_time DESC, id DESC` 一致**。
不能用 `order_no` 当第二键：同一秒里 `id=2/order_no=A` 排在 `id=1/order_no=Z` 前面是对的，
按订单号字符串比较会判反。空集合 / 单行 / 缺 `id` ⇒ `unverified`（退 2），
**不声称**多行排序已验证。

`id` 从哪来：`list_orders()` 的公开契约只返回 `order_no/status/site_id/created_time`
（**没有** `id`）。工具侧用同一 scope 的 `get_orders(order_no)` 取回该行的完整列补上，
**不改公开契约**；补不上就让它缺着，走 `unverified`，不伪造。

"同一个 `list_orders` 换个 `page_size` 首行相同"**不算**排序证明 —— 那两次调用走的是
同一个 `ORDER BY`，只能证明选择器是确定的。

### 输出脱敏与私有报告（默认）

终端**只打摘要**：结论、计数、时间、订单号前缀。完整订单号、问题原文、回答正文、
页面文本**只写 `--out` / `--state-dir` 显式指定的库外私有目录**，文件权限 0600。
**没有默认输出目录** —— 不给参数就不落盘。

写盘走 `private_output.write_private_report()`，三件事在**落盘之前**判：

| 判据 | 为什么 |
|---|---|
| 目标**及父目录**不是符号链接 | 旧实现 `write_text` 会打开链接指向的文件，覆盖别人放的内容 |
| 目标不在**任何**仓库/工作树内 | `--out .` 能把完整订单号写进会被提交的地方 |
| 目录属于当前用户、权限 0700，文件创建即 0600 | 先用 umask 创建再 chmod 有一段窗口 |

底层复用 `aiops_diagnostics.private_files.write_private_text`（tempfile + `replace`，
**不跟随**目标链接）；工具侧只补"库外"这一条，不改业务模块。

拒绝与失败都是非零退出；stdout/stderr 保持脱敏。

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

隧道：**实际连接也由 `dev-host exec` 建立**。

```
dev-host exec 41 --allow-service-exec --timeout 60 -- -N -L 127.0.0.1:<port>:127.0.0.1:443
```

`dev-host exec` 把 `--` 之后的参数原样交给它自己的 `ssh`（同一套
`StrictHostKeyChecking` + 项目 known_hosts + 主机指纹校验）。所以**校验与连接是
同一条命令**，不是"先 `dev-host exec hostname` 校验、再另起普通 ssh"——后者那次
校验说明不了实际连接连的是谁。

端口：显式绑定 `127.0.0.1`、动态分配（先探空）。

**监听者不一定等于 `Popen.pid`**：dev-host 是 shell 包装，真正监听的是它的子进程。
判据是"监听 PID **属于本次进程树**"（`_descendants`），不是"等于 Popen PID"。

**清理不能只靠进程组。** `dev-host` 内部用 `timeout … ssh …`，而 `timeout` 会给
`ssh` **换进程组** —— `killpg(我的组)` 打不到它，实测泄漏过一次（`ps` 里两个
`PGID` 明确不同）。所以清理按**端口归属**认：本次端口是唯一选出来的（启动前确认过
没人用），命令行里出现 `127.0.0.1:<port>:` 的 ssh 就是本次的。先 SIGTERM、轮询等
端口真的释放、再 SIGKILL 兜底。**不会碰到别人的隧道。**

真实隧道验证（只读静态资源，不碰业务 API）：

```bash
python3 tools/consumer-status/verify_tunnel.py   # 0 = 隧道通且清理干净
```

2026-10-23 实测：`dev_host_pid=3561921`、监听 `3561939`（**确实不同 PID**）、
`HTTP/1.1 200 OK`、1815 字节 HTML、`business_requests_sent=0`、
清理后端口 `still_listening=[]`。

（第一次跑这条时 `still_listening` 非空 —— 就是上面那个 `timeout` 换组的坑。）

## 部署清单（含新增的同目录依赖）

要在 41 上跑的：`check_identity.py`、`independent_cpms.py`、**`private_output.py`**。
三者缺一不可：`independent_cpms.py --out …` 会按同目录加载 `private_output.py`，
漏拷会在写证据时 `FileNotFoundError`（Codex 复现过）。

留在开发机的：`status_probe.py`、`verify_tunnel.py`、`replay_*.py`（要驱动浏览器/隧道）。

### 助手区域选择器（已对着现役产物校准）

上一版的 `ASSISTANT_SELECTORS` 是猜的（`.msg-list .msg-item` 之类），在现役产物里
**一个都不存在**。现在按 41 上 `static/js/aiPackage-pages-chat-chat.d86b6f8a.js` 的
真实渲染分支写成：

```
"user" === e.role
  ? <view class="user-bubble"><text class="bubble-text">…
  : <view class="ai-row"><view class="ai-msg"><text class="bubble-text">…
```

助手在 `.ai-msg`、用户在 `.user-bubble`，两者不重叠。本地回放验证（**不经生产**，
API 全部 route 拦截并返回合成数据）：

| 选择器 | 命中数 | 含订单号 |
|---|---|---|
| `.ai-msg .bubble-text`（现用） | 1 | 是 |
| `.ai-msg`（现用） | 1 | 是 |
| `.msg-list .msg-item`（旧猜测） | **0** | — |

区域若落在 `.user-bubble` 里，`region_is_not_user_bubble` 直接判失败；
选择器命不中时整段视为缺失，**不退回整页**。

⚠️ 回放用的是**与现役产物同构的合成 DOM**，不是公司构建产物的真实运行。
它证明选择器对得上类名结构，**不构成**新的真实业务验收。

回放脚本已入仓，可随时复跑（不需要网络、不碰生产）：

```bash
python3 tools/consumer-status/replay_selector.py   # 0 = 选择器符合预期
```

### 仍未做到的

- 真实存在的**他人订单被拒**仍未验证（见验收记录）。
- `status_probe.py` 的完整链路（dev-host 校验 → 隧道 → 浏览器 → 判据）本片
  只跑到了选择器与判据这一段，端到端未重跑（那会需要开生产窗口）。

## 五、运行前提与位置（别混写）

| 脚本 | 跑在哪 | 需要什么 |
|---|---|---|
| `check_identity.py` | **41**，`aiops41` 服务账号 | Redis 会话 + 业务库凭据（只在那个账号下） |
| `independent_cpms.py` | **41**，`aiops41` 服务账号 | 同上 + 个人 CPMS 地址已按租户登记 |
| `status_probe.py` | **开发机** | 真实浏览器；主机身份经 `dev-host` 校验后开隧道 |
| `replay_drive.py` | **开发机**（离线） | 只要 playwright；**不连任何生产** |

`status_probe.py` 的真实运行**必须先满足**：
1. `AIOPS_GATEWAY_CONSUMER_ORDER_CHAT_ENABLED=true` 且进程里生效；
2. `AIOPS_GATEWAY_CHARGING_STATUS_BASE_URL` 已按租户登记（映射形式）；
3. `AIOPS_GATEWAY_EXECUTION_ENVIRONMENT` 已设；
4. H5 站点的 `/v1/` 转发在位（#992）。

缺任一条件，它会跑但判据不过（`queued`/`running` 或模型拒绝话术）——**那不是工具坏了**。

## 六、浏览器驱动回放（不需要生产窗口）

`replay_drive.py` 用**同一个 `_drive()`**（同一套请求/响应捕获、按 `qa_id` 轮询、
助手区域读取、用户气泡判定），只把 `steps` 换成离线版本：

- 页面由本地 HTTP 服务器托管（合成 DOM，与现役产物同构）；
- 所有 `/v1/` 在路由层拦截并返回合成数据；**未声明的业务接口直接 abort**；
- 因此 `production_business_requests` 恒为 **0**。

三个场景：`normal` / `progressive`（**异步**分两段渲染）/ `failed`（错误 QA）。
前两个判据必须全过，第三个**必须至少一项不过**。

`progressive` 的时序是刻意设计的：第二段在 1200ms 到，而回放把区域等待压到
2.0s —— 二者**真实重叠**。`_drive()` 必须等到**完整**回答；"第一次出现非空文本
就收工"的实现会拿到半截、判据当场不过（这条已做变异复验）。

助手消息在回放里是**同一个节点**、文本就地增长（真实流式渲染就是这样）。
若每段新插一条，`locator.last` 永远读到最新的那条，时序缺陷就测不出来了。

```bash
python3 tools/consumer-status/replay_drive.py    # 0 = 三场景都符合预期
```

## 七、已知的存量文档敏感信息（待处理）

下列文件里有**既有**的账号/订单明文，不是本片引入，**未擅自改动**：

- `docs/adr/0090-diagnosis-contract-has-no-unsatisfiable-constraints.md`
- `docs/validation.md`（两处历史条目）

它们已在 Git 历史里。**清掉工作树不等于历史里没有** —— 需要单独决定是否处理，
本片不重写历史。

## 八、复跑步骤（按实际运行结果整理）

见文末「复跑记录」一节，那里记了构建身份、命令、退出码与证据位置。

## 九、复跑记录（2026-10-23，实际运行结果）

基线 `2a2fe4e`（`main`）。以下数字是本片在开发机上**实际跑出来**的，不是估计。

| 步骤 | 命令 | 退出码 | 结果 |
|---|---|---|---|
| 默认适配路径 | `PYTHONPATH=$PWD/src uv run pytest tests/test_consumer_status_default_path.py -q` | 0 | 4 passed |
| 全部自检 | `PYTHONPATH=$PWD/src uv run pytest tests/test_consumer_status_tools.py tests/test_consumer_status_default_path.py` | 0 | **37 passed** |
| 浏览器驱动回放 | `python3 tools/consumer-status/replay_drive.py` | **0** | `verdict=pass`，`production_business_requests=0`，三场景：normal/progressive `all_checks=true`、failed `all_checks=false`（预期不过） |
| 选择器回放 | `python3 tools/consumer-status/replay_selector.py` | 0 | `.ai-msg` 命中 1、旧猜测命中 0、用户气泡不含订单号 |

证据位置：回放的 JSON 打到 stdout（合成数据，无生产内容）；
真实运行的完整证据只写 `--out` / `--state-dir` 指定的库外私有目录（0600）。

**这一片没有开生产窗口**，所以上表**没有**任何真实查询作业或模型调用。
`status_probe.py` 的真实端到端仍未重跑（缺的是"受控窗口 + 真实会话"，
不是"离线无法验证"——离线驱动回放已经证明了）。

## 十、本片（两个运行边界）

| 边界 | 之前 | 现在 |
|---|---|---|
| 实际隧道 | 先 `dev-host exec hostname` 校验，再另起普通 `ssh` | **实际连接就是 `dev-host exec`**，`-N -L` 在 `--` 之后 |
| 端口归属 | 假定 `ss` 的 PID == `Popen.pid` | 判"属于本次**进程树**"（dev-host 是 shell 包装） |
| 私有报告 | `mkdir → write_text → chmod`，跟随符号链接、无库外限制 | 落盘前判符号链接 + 库外 + 目录权限 |

对应测试：`tests/test_consumer_status_boundaries.py`（13 条）。
变异复验：把写入改回 `mkdir+write_text+chmod` ⇒ 5 条转红；把隧道改回裸 `ssh` ⇒ 2 条转红。
