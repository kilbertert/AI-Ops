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

## 合并后补充观察

`05e82e8e`（#1232，提示词生命周期）合入后，上一提交的 CD（`5ab4e4cf`）长时间排队未执行；
`05e82e8e` 的 CD 在队列中。41 上实测 `/health` 在本次收尾时仍为
`1.0.0+f168bf53abe1`（#1238），即 #1239/#1240 的 Gateway 变更**尚未**部署到线上。
这不是失败，是排队；但"已合并"不等于"已部署"，回执不把二者混为一谈。
