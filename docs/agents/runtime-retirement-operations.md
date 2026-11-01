# 退役升级与真实验收操作

本手册记录本轮实际执行过的方法；状态与结论见最新validation，不能用历史会话或旧快照替代当次证据。
访问41/36均经本仓`.dev-host.toml`与`dev-host`校验主机身份。敏感输入只从受控文件/服务配置读取。

## 依赖漂移拦住CD时

1. 保持旧服务运行，备份源码、完整虚拟环境、依赖清单和两份环境配置。业务数据库单独保留，不以恢复源码为由倒回业务数据。
2. 在干净合并checkout运行`uv export --frozen --no-dev --no-emit-project --format requirements-txt -o runtime-requirements.txt`，
   按目标Python/平台下载其中锁定的wheel；把清单、wheelhouse、合并`pyproject.toml`/`uv.lock`打包为摘要制品，经`dev-host cp --artifact-sha256`送到声明的备份区域。
3. 现场核摘要。用服务虚拟环境的`uv pip install --python <服务Python> --no-index --find-links <wheelhouse> --require-hashes -r <runtime-requirements.txt>`安装合并锁依赖，
   第一阶段不移除旧进程仍需的包。确认安装成功后把合并`pyproject.toml`和`uv.lock`两份清单同步到服务应用根，再重跑原CD；不要跳过依赖漂移门。
4. CD源码成功后，在原服务目录重新安装editable入口；离线构建同时准备hatchling和editables。
   确认import仍指向部署源码，`aiops-admin`存在，最后移除已退役的SDK/打包依赖，`uv pip check`通过。
5. 私有配置读取必须以服务身份运行。`gateway.env`用于进程配置，`production.env`由项目配置读取器解释；
   不以root交互解释器的默认环境模拟网关，也不因配置校验失败放宽权限。

仅在直连供应商的真实单次请求成功、网关重启后健康/业务验证通过后，观察旧适配器实际连接与流量，
记录观察窗口，再停用旧适配服务。保留单元和配置用于恢复。零包短窗口不等于长期无人使用。

## 正常登录与原页面验收

- **开工前先确认生产没有别人在写。** 多会话并行时，另一个会话可能正在对同一个受管应用做
  发布、回滚或 A/B 演练，而验收读到的状态会在你取证的过程中变化。
  判据是读最近的发布账本，不是猜：

  ```sql
  SELECT id, at, tenant_id, app_id, agent_name, version_no, action, company_user_id
    FROM publish_audit_ledger
   WHERE julianday(at) >= julianday('now', '-30 minutes')
   ORDER BY julianday(at) DESC;
  ```

  **必须用 `julianday()`，不能直接比较文本。** `at` 写的是带 `T` 和 `+00:00` 的 ISO 8601
  （`2026-11-01T07:28:34.387612+00:00`），而 `datetime('now', …)` 返回空格分隔的
  `2026-11-01 07:51:51`；两种格式按字典序比较不按时间排序，跨 UTC 午夜时会把昨天的记录漏掉、
  把当天更早的记录算成近期。2026-11-01 实测：一条已超 30 分钟的记录，文本比较给出 `1`（"在窗口内"），
  `julianday` 比较给出 `0`（正确）。

  有活动写入就等它停，或换一个不受影响的应用/租户再验。**不要**把"正在被别人改"的现象
  当成缺陷。

  **账本只说明"开工前"的状态，不保证验收期间静止。** 验收开始后别人仍可能发布。
  凡是要拿"绑定/版本没变"当结论的检查，取证**前后各读一次**目标绑定并比对：

  ```sql
  SELECT environment, tenant_id, entry, scenario, revision, agent_version_no, state
    FROM chat_route_bindings
   WHERE tenant_id = '<租户>' AND entry = '<entry>' AND scenario = '<scenario>';
  ```

  `revision` 前后不一致 ⇒ 这轮取证期间有人改过，该轮结论作废重来；不要用"我读的那一刻是好的"背书。

- **每条失败响应都要先证明它问的是哪个租户、哪个入口、哪个场景，再判成因。**
  助手入口的"没有可用路由"有**两个不同的状态码**，含义不能混：

  | 响应 | 含义 | 判据 |
  | --- | --- | --- |
  | `409 CHAT_ROUTE_UNAVAILABLE` | 这一端这一场景**从来没有**活动绑定 | 该 `(environment, tenant_id, entry, scenario)` 在 `chat_route_bindings` 里没有行，或该行不是 `active` 且**不存在已撤回的行** |
  | `410 CHAT_ROUTE_RETRACTED` | 这条绑定**被撤回过**（撤回是显式决定，不回落直读） | 同 key 有一行 `state=retracted` |

  **410 的前置是该端的能力开关已打开。** 以客户端为例，只有 `consumer_chatflow_enabled`
  为真时请求才会走进 `select_binding()` 去看那条已撤回的行；开关关着时请求根本不查它，
  最终仍是 409。所以看到 409 时要连开关一起看，不要把"没开能力"读成"绑定丢了"。

  409 本身还要再分三种情况，它们的处置完全不同：

  | 情况 | 判据 |
  | --- | --- |
  | 该租户从未开通 | `chat_route_bindings` 与 `operator_apps` 里该租户都没有行 |
  | **只开通了另一端**（如只开了管家端，消费者来问） | `operator_apps` 有 `entry=operator` 的行，但 `consumer` + `consumer_service` 没有活动绑定 |
  | 该端有登记但指针丢了 | 该端在 `operator_apps` 有行，但对应场景没有 `active` 行、也没有 `retracted` 行 |

  **不能用"该租户在这两张表里有多少行"推断当前端的运行能力**——单端开通的租户会因此被误诊成
  "管家端开通后路由丢了"。正确做法是先确认这条请求的租户、入口与场景，再查
  `chat_route_bindings` 里 `(environment, tenant_id, entry, scenario)` 那一行的 `state`；
  `operator_apps` 里的登记只说明"有过应用"，草稿行不构成可运行绑定，已撤回的行构成的是 410 而不是 409。

  2026-11-01 实测过这个错法：按手机号从 Redis 里匹配到的会话属于另一个**未开通**租户，
  于是把一个正确的 409 当成了回归。有效会话只证明身份，不证明该端已开通。

- C端按原H5手机号验证码流程登录，用浏览器实际存储的third_session请求；有效会话回读仍需结果在有效期内。
- 验收资产的历史“固定码”描述不能替代当前验证码。通过原页面正常发码后，按受控配置读取
  `ver_code_default::<该资产手机号>` 的当前值及 TTL，只在内存填回同一页面；不写 Redis、不输出验证码。
  登录必须由页面原请求取得真实会话，失败时保留脱敏响应，不用另一端的登录结果推断本端。
- 管家App按原tenant-app密码流程登录；Dify运营页是独立公司登录，图形验证码由用户提供，禁止自签会话或注入代理缓存。
- 管家助手默认可能是语音输入；点击键盘切换按钮后再填问题。`input` 数量为零本身不代表页面不可用。
- Dify页面提交后会跳转，Playwright的旧Response.body可能不可读；以登录HTTP状态、跳转后有效页面、浏览器storage_state和正常原生预览共同验证。
- 原生预览是SSE流。在response事件里同步读完整正文会阻塞驱动；记录状态/请求，等待页面出现完整Java报告，
  再按本轮问题、时间、QA和run关联服务端证据。不要只取数据库“最新成功”行。
- 只读SQL/CPMS比较运行在服务身份下，复用仓库配置与授权解析器，**不要构造第二个create_gateway_app**，避免触发在飞作业恢复。
- 精确取本轮响应的 `qa_id`：管家查 `agent_runs` 的 `chat_binding/route_revision/exec_config_state`；
  客户端查 `chat_dify_runs` 的 `binding_key/context_json`。按该绑定核应用与版本，不能用数据库最新任务替代。
  管家比较`project_evidence`的业务字段，剔除本次读取必然变化的`observed_at`，不是忽略业务差异。
- 真实跨主体反例先用现有合法会话解析身份并确认同租户不同主体、SQL不可见；新建A的QA后立即做A回读200/B订单与QA拒绝。
  410过期不能算权限拒绝；无登录401不能算合法另一主体拒绝。
- 公司H5安装遵循[部署手册](operator-h5-deployment.md)，原首页点击入口后按业务选单器操作，
  仅API成功不能替代页面正文可见。无工程师确认的真实根因，仍不得宣称故障准确率验收。

证据目录0700、文件0600，记录只包含脱敏结论。原始会话、账号、订单、截图和日志不上传公开PR。
本次QA/审计按正常业务生命周期保留；清理浏览器和本次隧道，不清空真实数据根。

管家run已取到证据却未完成时，按精确QA核created_at/completed_at/expires_at及平台完成时刻。
若派发先于run期限结束，检查SnailDispatcher的deadline_at是否来自同run；不能只加浏览器等待或重新生成成功样本。
