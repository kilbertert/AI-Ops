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
   WHERE at >= datetime('now', '-30 minutes')
   ORDER BY id DESC;
  ```

  有活动写入就等它停，或换一个不受影响的应用/租户再验。**不要**把"正在被别人改"的现象
  当成缺陷。

- **每条失败响应都要先证明它问的是哪个租户，再判成因。** 同一个出口返回的
  `409 CHAT_ROUTE_UNAVAILABLE`（业务执行路由未配置）在两种租户上含义完全不同：已开通租户
  说明绑定丢了，未开通租户说明本来就该拒绝。2026-11-01 实测过这个错法——按手机号从
  Redis 里匹配到的会话属于另一个**未开通**租户，于是把一个正确的拒绝当成了回归。

  取证方式：读 `chat_route_bindings` 与 `operator_apps` 里该租户的行数（都为 0 即未开通），
  不要只看会话"有没有效"。有效会话只证明身份，不证明该租户被开通。

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
