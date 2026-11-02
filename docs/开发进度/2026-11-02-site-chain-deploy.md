# 站点整链合并与部署（2026-11-02）

**完成**：合并 #1302 → `0fce8a58`；节点、Gateway、回调配置从同一修订成套部署到 41，
逐项核验。**未完成**：真实业务验收（卡在既有的 Dify 数据问题，见下）。

## 部署了什么

从 `git archive 0fce8a58` 干净源码构建，不复用任何旧 target：

- 节点 jar `e3ee149b…`（106302208 B）—— 与 `images.lock.json` **逐字节相同**，
  发布目录 `/opt/aiops-node-runtime/releases/site-0fce8a58/`，其余文件由旧 release
  按字节继承（`diff -r` 只有 jar 不同）。
- Gateway 镜像 `site-merged-0fce8a58`，上下文摘要 `1f910bbb…`（`--check` match）。
- 回调配置用同一修订的渲染器重渲染，`--lua-directives` 传 41 的两行 lua 配置。
- 顺序是**节点先、Gateway 后**：CD 的节点闸门要求实际运行的节点不早于最后一次改节点的提交，
  反过来会把整条工具契约卡死（ADR-0175 没有灰度窗口）。

## 遇到的问题（都记进方法，不只记结论）

1. **节点构建标识格式写错**：先写成 `site-0fce8a58-…`，闸门按 `<commit12>-<jar12>` 取第一段
   当 commit 前缀，`site` 在仓库里找不到 ⇒ CD 拒绝。改成 `0fce8a58f725-e3ee149baa75`。
2. **`docker inspect .Args` 是容器内路径**：`/app/aiops-execution-node.jar`，
   必须在宿主上经 `.Mounts` 最长前缀匹配换回 `Source` 才能 `sha256sum`。
3. **`AIOPS_NODE_DIST` 切换后运行中容器仍是旧 env**：所以核验以 Args+Mounts 反推的
   宿主文件与容器内 sha 双读为准，不读 `runtime.env` 断言。

## 未完成与阻塞

`consumer_service` 路由绑定的 Dify app `07d15c42…` 与 workflow `ad84a86e…` 在 36 的
Dify 里**已不存在**（`apps`/`workflows` 查询均 0），key 也不在任何现存 token 里。
consumer 问答因此必然 `DIFY_UNAVAILABLE`。`operator` 入口要求公司令牌通道，
本轮未取得。这是部署前就存在的数据问题，需要单独决定：改绑定指向新 app，或从备份恢复。

因上述阻塞，**业务验收未完成**：站点真实正例、合法空结果、受限范围/跨租户拒绝、
账单、知识、两种风格、两端页面生命周期均未执行。

## 回滚点

`/var/backups/aiops-41/pre-site-0fce8a58-20261102-124431/`（回调配置 + runtime.env +
旧 release 整份）；旧 release 目录 `releases/site-arg-fix` 保留。
