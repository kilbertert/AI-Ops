# 接力回执：六片修复逐片交付（2026-10-31）

接手点：Codex 会话 `01a180d5` 中断于第三批推送（并发工作区锁定）。
按该会话已定的交付顺序逐片交付，不打包推送。

## 已合并

| 批 | PR | 合并提交 | 合并前基线 | CI |
| --- | --- | --- | --- | --- |
| 4 知识对客授权 | [#1238](https://github.com/kilbertert/AI-Ops/pull/1238) | `f168bf53` | `25d5b7dc` | verify / Workflow policy / spec-tree 全绿 |
| 5 执行器兼容 | [#1239](https://github.com/kilbertert/AI-Ops/pull/1239) | `5ab4e4cf` | `f168bf53` | 同上 |
| 6 真实工作流页 | [#1240](https://github.com/kilbertert/AI-Ops/pull/1240) | `79acaf4c` | `5ab4e4cf` | 同上 |

每片都在合并前 rebase 到当时的 `origin/main` 并重跑专项；`deploy/m2-live-03/images.lock.json`
的 `source_sha256` 用 `deploy/m2-live-03/context_digest.py` 重算，`--check` 为 match。

本机实测（每片合并前）：

- 知识对客授权：`tests/consumer/test_knowledge_access_management.py` + `tests/dify/test_dual_entry_bridge.py` → 9 passed
- 执行器兼容：`tests/execution/test_node_capabilities.py` + `tests/dify/test_dual_entry.py` + `test_self_service.py` → 65 passed
- 真实工作流页：浏览器 15 项、原生发布组件 29 项；完整 Next 构建（webpack、限内存）退出 0

Devin Review 四次均为 "trial expired and no credits remaining"（跳过），如实记录，未当作审查证据。

## 制品（开发机，未部署）

- `aiops/dify-web-patched:dual-entry-20261031`：由 #1240 合并前的工作树构建。
- 已 `docker save` + zstd 传至 36（`/root/web-dual-entry-20261031.tar.zst`，
  sha256 `c6b168e8…b209f`），36 未 load、未切换 `DIFY_WEB_IMAGE`。

## 未完成，且不是代码缺口

1. **真实页面开通**：需要 `aiops-41-platform-dify` 正常登录 Dify（36 :10018），
   在一个租户上完成双端开通。本会话没有该账号会话，未伪造。
2. **web 镜像部署**：36 现役 `DIFY_WEB_IMAGE=aiops/dify-web-patched:knowledge-1be3f067`。
   切换前需确认镜像由**合并后**修订构建；`dual-entry-20261031` 的构建树与合并提交有
   3 个文件差异（补丁尾部、`aiops-management.ts`、header 单测），因此不直接上线，
   需按合并提交重新构建一次。
3. **桥 / Dify API 补丁 / 节点 jar**：36 与 41 上仍是旧制品，需按
   `docs/agents/operator-cpms/self-service.md` 的同版升级步骤安装。
4. **真实消费者/员工会话**（A–L 的 H/I/J1）：依赖第 1 项。
5. **渠道声明**（J2）：`env-41.toml` 仍无 `[[channels]]`；需要真实登录证据后再写。

## 合并后：节点升级与 CD 恢复

`05e82e8e`（#1232，提示词生命周期）合入后，`5ab4e4cf` 的 CD 先因 runner 被一个孤儿
update-branch 任务占住而排队，释放后执行**失败**——不是排队问题，是 `deploy-41.sh` 的
节点制品闸门：最后一次改节点源码的是 `5ab4e4cf`（#1239），41 上运行的还是 `ae290a0d`。
ADR-0175 没有灰度窗口，网关不能先于节点上线。

按 `deploy/node-runtime/README.md` 处理（全部只落在 41 的既有节点目录与回滚点内）：

1. `git archive 5ab4e4cf services/execution-node` 取干净源码，离线 Maven 构建
   → `aiops-execution-node.jar` sha256 `6ffad741…`（含 `CapabilityReporter`）。
2. 新建发布目录 `releases/5ab4e4cf6a89`（`cp -a` 旧发布，只替换 jar），
   备份并改写 `runtime.env` 的 `AIOPS_NODE_DIST` / `AIOPS_NODE_BUILD`，
   定点 `--no-deps --pull never --force-recreate node`，约 50s 就绪。
3. **回调 nginx 必须同版重渲染**：旧 `gateway-callback.conf` 没有
   `location = /v1/internal/execution-node/capabilities`，节点上报一律 404（节点侧静默重试）。
   按新模板渲染后 `nginx -t` 通过、`systemctl restart aiops-node-callback`，
   从节点网络空间探针由 404 → 422（无凭据拒绝，说明路由已通）。
   渲染时**保留宿主已有的 `lua_package_path`/`lua_package_cpath`**——模板没有这两行，
   直接覆盖会让回调起不来（实测过一次，已按备份恢复）。
4. 节点重启后，Gateway 的 `execution_node_capabilities` 出现
   `production-41 / node-41` 的真实报告，`tools_sha256` 与
   `/etc/aiops-node-runtime/node/operator-tools.json` 的摘要逐字相同。
5. `gh run rerun 41186947981` → **success**。

收尾状态（只读回读）：41 `/health` = `1.0.0+05e82e8ec54e`，
`business_mutations=disabled`，`/v1/openapi.json` 200、`/v1/runs` 404，
`aiops-gateway-41` / `aiops-node-runtime` / `aiops-node-callback` 三个 unit 均 active。
回滚点：`/etc/aiops-node-runtime/runtime.env.bak-20261031-1733` 与
`gateway-callback.conf.bak-20261031`，指向旧发布 `releases/tools-964dbf353f73`
（jar sha256 `4e2e23a6…`）。
