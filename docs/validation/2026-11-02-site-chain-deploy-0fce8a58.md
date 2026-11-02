# 站点整链部署与验收（0fce8a58，2026-11-02）

合并：#1302 → `0fce8a58`。本文件记录**实际执行**的部署与验收，不含预期。

## 一、制品身份（三者对齐，逐项核）

| 项 | 值 | 核验方式 |
|---|---|---|
| 合并修订 | `0fce8a58f725` | `git log origin/main` |
| 节点源码摘要 | `d8e30493…` | `source_digest.py --check` → match: true |
| 节点 jar | `e3ee149b…`，106302208 B | 从 `git archive 0fce8a58 services/execution-node` 干净构建，与 lock **逐字节相同** |
| Gateway 上下文摘要 | `1f910bbb…` | `context_digest.py --check` → match: true |
| Gateway 镜像 | `aiops-m2-live-03-gateway:site-merged-0fce8a58`，image_id `sha256:eed91bc5…` | 从合并修订 `build-image.sh` 构建，base 固定为锁里的 `python@sha256:70729b46…` |
| operator-tools.json | `99d18590…` | 仓库声明 == 41 上配置 == 容器内挂载，三处相同 |

## 二、节点部署（先于 Gateway，闸门要求）

- 新发布目录 `/opt/aiops-node-runtime/releases/site-0fce8a58`，由旧 release
  `cp -a --reflink=auto` 复制，**只有 `node/aiops-execution-node.jar` 与旧版不同**
  （`diff -r` 其余逐字节一致）。
- `AIOPS_NODE_DIST` / `AIOPS_NODE_BUILD` 更新为 `site-0fce8a58` /
  `0fce8a58f725-e3ee149baa75`。**首次写成 `site-0fce8a58-…` 是错的**：
  CD 的节点闸门按 `<commit12>-<jar12>` 取 `AIOPS_NODE_BUILD` 的第一段当 commit 前缀，
  `site` 在仓库里找不到 ⇒ 闸门拒绝（实测一次 CD 失败）。
- 定点重建：`docker compose --project-name aiops-node-runtime --env-file … up -d
  --no-deps --pull never --force-recreate node`；42 秒后 8081 应答，`restarts=0`、无 OOM。
- 其余三个容器 ID 未变：platform `8ef3cc6ec4ce`、model-proxy `539aabd0a3b3`、
  store `cb963492a046`。

**实际运行 jar 的核验（四步）**：`docker inspect .Config.Cmd` 得容器内路径
`/app/aiops-execution-node.jar` → `.Mounts` 最长前缀匹配得宿主
`/opt/aiops-node-runtime/releases/site-0fce8a58/node/aiops-execution-node.jar`
→ 宿主 sha `e3ee149b…` → 容器内 sha `e3ee149b…`，与构建产物三处相同。

## 三、回调配置

从合并修订的渲染器渲染（`--lua-directives` 传 41 的两行 lua 配置，普通 nginx 留空），
先在宿主 `nginx -t` 通过再替换。重启后逐条实测：

| 请求 | 期望 | 实测 |
|---|---|---|
| 声明的站点授权路径 | 到达网关 | 422（无 body，网关拒绝） |
| `tools/shell/authorization` | 拒绝 | 404 |
| `cpms/chSite/pageBOGUS` | 拒绝 | 404 |
| `cpms/chSite/page/ORDER-1` | 拒绝 | 404 |
| `cpms/chOrderInfo/getOrderInfoDetail`（缺单号） | 拒绝 | 404 |
| `cpms/chOrderInfo/getOrderInfoDetail/ORDER-1` | 放行到网关 | 403（无凭据） |
| 明文打同一端口 | 拒绝 | 400 |

## 四、Gateway 部署

CD `41389671185` 第 5 次 attempt success。`/health` = `1.0.0+0fce8a58f725`；
`/opt/aiops-41/src` 与合并树 **143/143 文件**逐一比对 sha：142 个逐字节相同，
唯一差异是 `__init__.py` 的版本号（部署脚本注入，预期内）。

## 五、真实业务验收：**未完成**

尝试以真实会话（tenant 1942105476598861824，operator 与 consumer 两个入口）发起
站点问答，结果：

- **operator 入口** → 403 `OPERATOR_COMPANY_TOKEN_REQUIRED`。该入口要求公司令牌通道
  （`X-Third-Session` 会被显式拒绝），本次未取得可用公司令牌。
- **consumer 入口** → 202 后 `failed / DIFY_UNAVAILABLE`。

后者追到根因：`chat_route_bindings` 里 consumer 的 `consumer_service` 行指向
Dify app `07d15c42-c2a1-5432-8081-30e5f5dd95b0` 与 workflow `ad84a86e-…`，
而 36 的 Dify 库里这两个对象**都已不存在**（`count=0`），对应 key 文件也不在任何
现存 api_token 里。41→36:10008 网络正常（返回 401 说明请求到达 Dify）。

**这是部署前就存在的生产数据问题**：本次改动没有一处写 `chat_route_bindings`
或操作 Dify app。证据见私有目录
`~/.cache/aiops-site-acceptance-20261102/{BLOCKER.md,dify-app-check.txt}`。

因此：站点真实正例、合法空结果、受限范围/跨租户拒绝、账单、知识、两种风格、
两端页面生命周期 **全部未验收**。

## 六、回滚点

- 部署前快照：`/var/backups/aiops-41/pre-site-0fce8a58-20261102-124431/`
  （`gateway-callback.conf` sha `b3ff42e9…`、`runtime.env` sha `8d043aa5…`、
  旧 release 整份）。
- 旧 release 目录保留：`/opt/aiops-node-runtime/releases/site-arg-fix`
  （jar `43ce81ab…`）。
- Gateway 回滚用 `deploy-41.sh --rollback-to`（上一部署修订 `6a2c6e43b9cd`）。
