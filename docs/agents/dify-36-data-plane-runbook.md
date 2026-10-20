# 36 上 Dify 栈的数据面：向量库、补丁、迁移（可复跑的手顺）

> **读者**：要在 36 上把 `deploy/dify-36` 的改动落地、或复验这三处修复的人。
> **状态**：2026-10-20 首次成文。部署件在 `deploy/dify-36/`，验收约定在
> `deploy/dify-36/acceptance.feature` 与 `qa-plan.md`；本页只讲**怎么做、怎么判、怎么回滚**。
>
> **纪律**：服务主机只接**制品**。本页所有改动都来自一个已合并的 main 提交，
> 在 36 上执行时只用制品与脚本，不在 `/opt/dify` 里手改运行代码。

## 0. 三句话

1. **向量库是新增的第 10 个容器**，`VECTOR_STORE=weaviate` 必须同时在 `.env` 里，否则
   `documents.error` 会继续是 `Vector store must be specified.`。
2. **补丁不是整文件**：`patches/apply.sh` 从**同一镜像**取原文件、核对 sha256、打补丁，
   产出 `volumes/app/patched-api/`；摘要不匹配就停，绝不挂陈旧补丁。
3. **迁移分 dry-run 与 apply 两步**，孤儿绑定按"目标 dataset 是否存在"判定，不按手写清单。

## 1. 取制品

从**已合并的 main** 打包，不从工作区：

```bash
cd /home/claude/Projects/AI-Ops || exit 1
pwd; git remote get-url origin
git fetch origin --quiet
git rev-parse origin/main                     # 记下这个 sha，验收要用
rm -rf /tmp/stage && mkdir -p /tmp/stage
git archive origin/main deploy/dify-36 | tar -x -C /tmp/stage
tar -czf /tmp/dify36-sync.tgz -C /tmp/stage deploy/dify-36
sha256sum /tmp/dify36-sync.tgz
```

## 2. 备份（必须先做）

```bash
ssh yidong-36 'ts=$(date +%Y%m%d%H%M%S)
  cp -a /opt/dify/deploy /opt/dify/deploy.bak-$ts
  cp /opt/dify/deploy/.env /opt/dify/deploy/.env.bak-$ts
  echo backup=$ts'
```

`.env` 单独备份一次：它在 `deploy/` 里，但下一步会把 `deploy/dify-36` 的**模板**覆盖过去，
而 `.env` 是现场生成、不在仓库里的。

## 3. 传 + 覆盖（只覆盖仓库里有的文件）

```bash
scp /tmp/dify36-sync.tgz yidong-36:/tmp/
ssh yidong-36 'sha256sum /tmp/dify36-sync.tgz'          # 与本地比
ssh yidong-36 'cd /tmp && rm -rf /tmp/dify36 && mkdir /tmp/dify36
  tar -xzf /tmp/dify36-sync.tgz -C /tmp/dify36
  rsync -a --checksum /tmp/dify36/deploy/dify-36/ /opt/dify/deploy/
  chown -R root:root /opt/dify/deploy
  chmod 600 /opt/dify/deploy/.env'
```

`rsync` 不带 `--delete`：`.env`、`volumes/`、`dsl-fixtures/`、`ARTIFACT.sha256` 是现场的，
模板里没有，不能被删。

## 4. 三个变量进 `.env`

`env.example` 是模板；`.env` 是现场的，**手工补三项**（不要在 36 上整份覆盖 `.env`）：

```bash
ssh yidong-36 'grep -q "^VECTOR_STORE=" /opt/dify/deploy/.env || cat >> /opt/dify/deploy/.env <<EOF
VECTOR_STORE=weaviate
WEAVIATE_ENDPOINT=http://weaviate:8080
WEAVIATE_GRPC_ENDPOINT=grpc://weaviate:50051
WEAVIATE_API_KEY=$(openssl rand -hex 24)
EOF
grep -c "^WEAVIATE_API_KEY=.\{48\}$" /opt/dify/deploy/.env'
```

**判据**：最后一行输出 `1`。`WEAVIATE_API_KEY` 必须现场生成、不回显、不进任何证据文件。

## 5. 生成补丁树（在 36 上，root）

```bash
ssh yidong-36 'cd /opt/dify/deploy && ./patches/apply.sh'
```

成功输出 5 行 `ok:` 与一行「5 个文件就位」。**失败即停**：`FAIL: … 与固定的上游版本不一致`
意思是镜像换了（升级），补丁必须按新版本重做 —— 不能跳过。

## 6. 起栈与形状自检

```bash
ssh yidong-36 'cd /opt/dify/deploy && ./check.sh | tail -5 && \
  docker compose -f docker-compose.yaml --env-file .env up -d'
```

`check.sh` 是零网络的形状自检（发布端口、服务集、向量库变量、补丁挂载集合），
在开发机上也能跑，且 CI 里的 `tests/test_dify36_deploy_assets.py` 覆盖同一组断言 ——
**两层都要有**：check.sh 是宿主上的最后一道，CI 是合并前的第一道。

## 7. 跑迁移（先 dry-run）

```bash
ssh yidong-36 'docker cp /opt/dify/deploy/migrate_20261020.py deploy-api-1:/tmp/ \
  && docker exec -u root deploy-api-1 chmod 644 /tmp/migrate_20261020.py \
  && docker exec -w /app/api deploy-api-1 python /tmp/migrate_20261020.py'
```

`docker cp` 的默认属主是 root、模式 600 —— 容器里的 api 以 uid 1001 跑，直接跑会
`Permission denied`（实测踩过），所以有那条 `chmod`。

**顺序是硬约束**：迁移必须在**打完补丁的容器已经起来之后**跑。反过来的话，
库里是密文而运行中的旧代码把它当明文发出去 —— 外部检索会在下一次调用时开始 401，
而且报错看起来像"供应商拒绝了凭据"，与本次改动毫无关联的样子。
`§5 打补丁 → §6 起栈 → §7 迁移` 这个顺序不是建议。

**判据**：报告里"孤儿绑定 6 条"、"明文 api_key 2 条"。与 `qa-plan.md` 的 BIND-02 / SEC-02
比对后再 `--apply`。迁移脚本本身**不换密钥值**；轮换是另一件两侧同步的动作。

## 8. 重跑索引（VEC-02）

控制台里对那个 vendor 数据集点「重新索引」；等 `documents.indexing_status = completed`。

```bash
ssh yidong-36 'docker exec deploy-db_postgres-1 psql -U dify -d dify -c \
  "select indexing_status,error from documents;" -c "select count(*) from embeddings;"'
```

**判据**：`completed`、`error` 为空、`embeddings` 计数大于 0。1564 个分段的向量化要打
embedding 供应商，耗时以分钟计；先看 worker 日志再判断卡住。

## 9. 验收外部检索（RET-01）

服务 API 要一把 **dataset 类型**的 key（`api_tokens.type='dataset'`），不是 app 的 key：

```bash
ssh yidong-36 'T=<dataset key>
  curl -s -w "\n%{http_code}\n" -X POST \
    -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
    -d "{\"query\":\"云快充协议\"}" \
    http://127.0.0.1:10018/v1/datasets/df0f1d4a-3d6e-404e-9656-c88da6df3d1e/retrieve'
```

**判据**：200，且 `records[]` 每项有 `content` 与 `metadata`，**没有** `segment` 字段。
补丁前这里返回的是 `records: []`（"命中为空"的成功响应）。

## 10. 回滚

| 场景 | 动作 |
|---|---|
| 补丁导致 api/worker 起不来 | 注释掉 compose 里 5 条 `*pm_*` 挂载 → `up -d api worker`。容器回到未打补丁的上游行为（external 检索会退化为空结果） |
| 向量库配置有问题 | `VECTOR_STORE` 留空 + `docker compose stop weaviate`。vendor 索引退回不可用，其余不受影响 |
| 整栈回退 | 把 `deploy.bak-<ts>` 覆盖回 `/opt/dify/deploy`，用备份的 `.env` 起 |
| 迁移写错 | 迁移在单事务里；用 §2 备份的 DB 卷快照恢复（没做快照时，孤儿绑定可重建、加密值可解回明文，**不会丢数据**） |

**回滚不改事实**：加密是可逆的（密钥在租户里），删除的孤儿绑定本来就不指向任何数据集。
唯一不可逆的是**密钥轮换** —— 那一步不做回滚，只做"换新的"。

## 11. 已知的坑

1. **`docker run --entrypoint cat IMAGE file` 是取原文件的唯一干净办法**。`docker cp` 从
   运行中的容器取也可以，但那是**运行中**的文件 —— 如果之前有人手工改过，sha 就永远对不上。
2. **挂目录会把 `/app/api` 整个遮住**。补丁必须逐文件挂，`check.sh` 与 CI 都断言这一点。
3. **`WEAVIATE_API_KEY` 缺失时 compose 直接拒绝解析**（`${VAR:?}`），这是刻意的：
   静默起一个无鉴权的向量库比不启动更糟。
4. **weaviate 镜像里没有 curl**，健康检查用 busybox 的 `wget`（实测）。
5. **控制台读 external API 模板时 CSRF 要 cookie 与 header 同时带**（`__Host-csrf_token`），
   只用 `Authorization` 会 401。手工 curl 复验时容易漏。
