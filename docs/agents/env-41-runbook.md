# 41 环境运维与真实验收手册

> **用途**：让任何 agent/工程师在不依赖会话记忆的前提下，能独立完成 41
> （`api.mall.qushiyun.com`）的**部署、真实资源创建、真实端到端验收**。
> **最后核验**：2026-09-16（对 main `b0b3856` 实测；新增 §1.1 密钥登录）。
>
> **凭据纪律**：本文件只写方法与位置，**不写任何口令/令牌/thirdSession 明文**。
> 凭据读取一律从 41 主机的 root 权限环境变量现场取，不落库、不入仓库、不贴聊天。

## 0. 环境事实（一次核对）

| 事实 | 值 |
|---|---|
| 41 主机 | `47.97.160.153`（root 登录，方式见 §1） |
| 运行源码 | `/opt/aiops-41/src/aiops_diagnostics/` |
| Gateway 数据库 | `/var/lib/aiops-41/gateway/gateway.db`（SQLite） |
| 服务配置 | `/etc/aiops-41/production.env`（含凭据，勿打印明文） |
| 平台清单 | `/opt/aiops-41/ops/environments/env-41.toml` |
| 服务单元 | `aiops-gateway-41.service`、`aiops-36-kb-tunnel.service` |
| 回环监听 | Gateway `127.0.0.1:8788`；KB 隧道 `127.0.0.1:29380`（→ 36 kb-service） |
| 公网入口 | `https://api.mall.qushiyun.com/v1/*`（**不是** `/aiops/v1/*`，后者 404） |
| 会话 Redis | `127.0.0.1:6379`（41 本机），键前缀 `app:3rd_session:` |
| 业务库 | MySQL `192.168.1.45:3306/cloud_charging_pile`（账号 `mall`，只读用途） |

## 1. 访问 41

### 1.1 首选：密钥登录（2026-09-16 起已配置）

```bash
ssh aiops-41 '<命令>'          # 别名：47.97.160.153, user root, key id_ed25519_41_aiops
```

别名定义在 `~/.ssh/config`；私钥 `~/.ssh/id_ed25519_41_aiops`（公钥已装到 41 的
`/root/.ssh/authorized_keys`）。**不需要口令，可直接用于自动化。** 验证：

```bash
ssh -o BatchMode=yes aiops-41 'echo OK; hostname'
```

若 `Permission denied (publickey)`：说明公钥未装或私钥缺失，按 §1.2 用一次性口令装回公钥。

### 1.2 一次性口令（仅用于装回公钥；口令现场从受控来源取，不写进任何文件）

```bash
PUB=$(cat ~/.ssh/id_ed25519_41_aiops.pub)
SSHPASS='<现场从受控来源取得>' sshpass -e ssh -o StrictHostKeyChecking=no root@47.97.160.153 "
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys
  grep -qF '$PUB' /root/.ssh/authorized_keys || echo '$PUB' >> /root/.ssh/authorized_keys
  echo KEY_INSTALLED"
```

**注意**：不要把口令写进命令行参数以外的地方（不写文件、不贴聊天）。装好公钥后一律走 §1.1。

### 1.3 执行约定

- 所有需要读 `production.env` 凭据的命令都用 `root`（`aiops41` 用户读不到 env 明文）。
- 应用进程内执行（reconcile、ShortcutManager）用 `runuser -u aiops41 -- ...`，
  工作目录必须在 `/opt/aiops-41`（否则 `.venv` 找不到 `pyproject.toml`，报
  `PermissionError: /root/pyproject.toml`）。
- **41 上没有 `sqlite3` CLI**；查库用 `/opt/aiops-41/.venv/bin/python -c "import sqlite3; ..."`。

## 2. 部署（源码同步到 41）

生产代码是文件拷贝部署（41 无 `.git`）。流程：**备份 → 传 → 校验 sha → 重启**。

```bash
# 1) 本地打包（在 canonical checkout，确保在目标 commit）
tar czf /tmp/aiops-sync.tar.gz src/aiops_diagnostics/

# 2) 上传
scp /tmp/aiops-sync.tar.gz aiops-41:/tmp/

# 3) 在 41 上：先备份，再解到临时目录核对，最后 rsync 覆盖
ssh aiops-41 '
set -e
mkdir -p /var/backups/aiops-41/backup-$(date +%Y%m%d-%H%M%S)
cp -a /opt/aiops-41/src /var/backups/aiops-41/backup-$(date +%Y%m%d-%H%M%S)/
mkdir -p /tmp/sync-check && tar xzf /tmp/aiops-sync.tar.gz -C /tmp/sync-check
rsync -a --delete /tmp/sync-check/src/aiops_diagnostics/ /opt/aiops-41/src/aiops_diagnostics/
chown -R aiops41:aiops41 /opt/aiops-41/src
systemctl restart aiops-gateway-41.service && sleep 5
systemctl is-active aiops-gateway-41.service
curl -s --max-time 6 http://127.0.0.1:8788/health
rm -rf /tmp/sync-check /tmp/aiops-sync.tar.gz'
```

**逐文件 sha 校验（必做）**：

```bash
# 本地
for f in $(git ls-files src/aiops_diagnostics/); do echo "$f $(sha256sum "$f" | cut -c1-16)"; done | sort > /tmp/local.txt
# 41（排除 __pycache__）
ssh aiops-41 'cd /opt/aiops-41 && for f in $(find src/aiops_diagnostics -type f -not -path "*__pycache__*" | sort); do echo "$f $(sha256sum "$f" | cut -c1-16)"; done' | sort > /tmp/remote.txt
diff /tmp/local.txt /tmp/remote.txt && echo "41 == main"
```

回滚：`rsync -a --delete /var/backups/aiops-41/backup-<时间戳>/ /opt/aiops-41/src/ && systemctl restart aiops-gateway-41.service`。

## 3. 资源创建（走生产代码路径，禁止手工插库）

### 3.1 Agent（清单驱动 reconcile）

清单追加 `[[agents]]`（字段见 `ops/README.md`）后：

```bash
ssh aiops-41 '
install -o aiops41 -g aiops41 -m 0640 /tmp/env-41.toml /opt/aiops-41/ops/environments/env-41.toml.new
cd /opt/aiops-41
runuser -u aiops41 -- /opt/aiops-41/.venv/bin/python -m aiops_diagnostics \
  --config /etc/aiops-41/production.env admin reconcile \
  ops/environments/env-41.toml.new \
  --db /var/lib/aiops-41/gateway/gateway.db \
  --kb-url http://127.0.0.1:29380 --dry-run'
```

- `--dry-run` 先看动作（`created`/`unchanged`），确认后去掉 `--dry-run` 实跑。
- **幂等验收**：连续实跑第二次应全部 `unchanged`。
- `--db` 必须显式给（不 source env 时 `from_env()` 回退 XDG 会**静默建空库**）。
- 发布前 KB 活性校验会真实调用 kb-service；供应商欠费时（embedding 502）会误报
  "知识库不存在"，**这是误报**，充值后重试即恢复，不要据此删绑定。

### 3.2 快捷动作（ShortcutManager，无 CLI）

`admin` CLI 没有 shortcut 子命令；用生产类在 41 上创建，**不要**直接 `INSERT INTO shortcuts`：

```text
# runuser -u aiops41 -- /opt/aiops-41/.venv/bin/python 执行
from pathlib import Path
from aiops_diagnostics.shortcut_lifecycle import ShortcutManager, ShortcutStore
from aiops_diagnostics.scope_context import DataScope, ScopeContext, SubjectRecord

store = ShortcutStore(Path("/var/lib/aiops-41/gateway/gateway.db"))  # 必须 Path，不是 str
manager = ShortcutManager(store)
subject = SubjectRecord(b_user_id="B-admin-41", tenant_id="<租户>")
admin = ScopeContext.build(
    caller=subject, subject=subject, delegated=False,
    effective_tenant_id="<租户>", data_scope=DataScope(type="self"),
    roles=frozenset({"ROLE_AGENT_ADMIN"}),
    permissions=frozenset({"aiops:shortcuts:manage"}),
)
s = manager.create(admin, {
    "business_entry": "consumer", "code": "case_exploration", "intent": "case_exploration",
    "requires_order": False, "sort_order": 10,
    "labels": {"zh": "客户案例", "en": "Customer Cases"},
    "descriptions": {"zh": "...", "en": "..."},
    "question_templates": {"zh": "我想看看客户案例", "en": "..."},
    "target_agent_version": "agt_xxx#v1",   # 宣传类才填，其余 None
})
manager.publish(admin, s.shortcut_id, expected_revision=s.revision)
```

创建后**必须**用公网 `GET /v1/shortcuts` 验收（见 §5）。

**改造一条已发布动作为跳转动作**（不新增行，`jump_path` 非空即跳转）：

```text
# 派生草稿 → 改 jump_path → 发布。旧发布版本快照保留，可 rollback。
draft = manager.fork_draft(admin, shortcut_id, expected_revision=current.revision)
manager.update(admin, draft.shortcut_id, {
    "expected_revision": draft.revision,
    "intent": "report_fault", "requires_order": False, "sort_order": 30,
    "labels": {"zh": "故障上报", "en": "Report a Fault"},
    "descriptions": {…}, "question_templates": {…},   # 保留原值
    "jump_path": "/charge/pages/faultReport/faultReportList",
})
manager.publish(admin, updated.shortcut_id, expected_revision=updated.revision)
```

- `jump_path` 必须 `/` 开头、≤512 字符；留空/省略即提示动作。
- **平台默认行与租户行都要改**：`list_effective` 中租户已发布行覆盖平台默认行，只改一条会分叉。
- 改前先做精确 SQLite 备份（见 §5 的备份纪律）。

## 4. 真实会话与数据（验收前置）

### 4.1 取有效 thirdSession

会话键在公司会话 Redis，41 本机 `127.0.0.1:6379`，前缀 `app:3rd_session:`：

```bash
ssh aiops-41 '
PW=$(grep -oE "^AIOPS_REDIS_PASSWORD=.*" /etc/aiops-41/production.env | cut -d= -f2)
for K in $(redis-cli -h 127.0.0.1 -p 6379 -a "$PW" --no-auth-warning --scan --pattern "app:3rd_session:*"); do
  V=$(redis-cli -h 127.0.0.1 -p 6379 -a "$PW" --no-auth-warning get "$K" | tr -d "\000-\010")
  TEN=$(echo "$V" | grep -oE "\"tenantId\":\"[0-9]+\"" | head -1)
  TTL=$(redis-cli -h 127.0.0.1 -p 6379 -a "$PW" --no-auth-warning ttl "$K")
  echo "$K | $TEN | ttl=$TTL"
done'
```

- **thirdSession 令牌 = 键名去掉 `app:3rd_session:` 前缀后的部分**。
- **请求头 `tenant-id` 不能覆盖会话身份**：以会话 `tenantId` 为准，租户必须匹配，
  否则拿不到该租户的数据（实测：某演示会话实际解析为另一租户）。
- 先冒烟验证再使用：`GET /v1/faq/recommendations` 返回 200 即有效。
- 会话是业务方提供的敏感凭据：**不落仓库、不贴文档、不写聊天记录**。

### 4.2 找该会话可归属的真实订单

订单归属校验查 `ch_order_info`（**不是** `ch_occupy_order_info`），列为
`order_no / user_id / tenant_id`：

```text
# runuser -u aiops41 -- /opt/aiops-41/.venv/bin/python 执行（口令从 env 现场取）
docker_mysql = dict(host="192.168.1.45", port=3306, user="mall",
                    password=os.environ["PW"], database="cloud_charging_pile")
cur.execute("SELECT order_no FROM ch_order_info WHERE tenant_id=%s AND user_id=%s LIMIT 5",
            (tenant_id, session_user_id))
```

- `session_user_id` 来自会话值的 `userId` 字段。
- 表内多为历史/演示数据；**没有订单就如实记录"当前会话无可归属订单"，不要编造**。

## 5. 公网验收（真实端到端）

统一头（`third-session` 全小写连字符，§2.1 of frontend-api-brief）：

```bash
curl -s -H "third-session: <会话>" -H "tenant-id: <会话租户>" \
  -H "X-Business-Entry: consumer" -H "Content-Type: application/json" \
  https://api.mall.qushiyun.com/v1/shortcuts
```

关键断言与对应契约见 `docs/agents/frontend-api-brief.md` 场景 D 与
`qa-plan.md` 的 INTENT-01..08；完整可复跑步骤见 `docs/validation.md`。

### 5.1 轮询纪律

- `qa`/`diagnosis` 创建返回 `202` + `retry_after_ms`，按它节流轮询。
- `failed` 是**终态**：停止轮询，`error` 带 `{code, message, retryable}`。
- 诊断是长任务（实测 1-8 分钟），用后台轮询，不要短超时。

### 5.2 故障排查入口

| 症状 | 查哪里 |
|---|---|
| 路由/回答异常 | 41 `gateway.db`：`assistant_questions`、`standard_diagnoses`、`agent_run_metrics`（`route_type` 区分 faq/qa/diagnosis/clarification/promo） |
| 模型/工具行为 | `/var/lib/aiops-41/gateway/runs/run-*/events.jsonl` + `/opt/aiops-41/codex-home/sessions/**/rollout-*.jsonl` |
| 检索为何 0 命中 | 36 RAGFlow 容器日志：`docker logs ragflow-kb-ragflow-cpu-1 --since 20m \| grep match_text`（**能直接看到模型实际发出的检索词**） |
| 服务无响应 | `journalctl -u aiops-gateway-41.service`、`systemctl status`、`/health` |

**经验**：`match_text` 日志是排查"检索质量"问题的最快路径——本轮即由此发现
模型把整段 `reason` 当检索词导致 0 命中。

## 6. 真实验收边界（不得逾越）

- 本地 fixture / 替身模型 / SQLite 直查 / 模型单次调用，**都不是**真实业务验收。
- 宣传卡片需该租户有**已发布宣传 Agent + 绑定 KB + 至少一条素材**；无素材时
  "无可用案例"是正确行为，不要宣称卡片验收通过。
- 涉及写操作（工单/退款/配置）保持禁用；本手册所有步骤为只读或经生产代码路径
  的资源创建。
- 报告状态只能用：`draft_ready` / `pr_open` / `merged_waiting_deploy` / `live` /
  `blocked`；未实测一律写"待验证"。
