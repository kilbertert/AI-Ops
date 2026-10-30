# 网关/节点同版判据的自检

日期：2026-10-30。分支 `feat/node-artifact-gate`，基线 `425b5f79`。

## 已执行的检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 全量 | `uv run pytest` | **3551 passed, 9 skipped** |
| 静态 | `ruff check .` | 通过 |
| 节点源码摘要 | `python deploy/node-runtime/source_digest.py --check` | match |
| 网关上下文摘要 | `python deploy/m2-live-03/context_digest.py --check` | match |
| 脚本语法 | `bash -n deploy/deploy-41.sh` | 通过 |

## 新增判据各自被证明

| 判据 | 断言 |
| --- | --- |
| 闸门按「最近一次改节点」判 | `test_the_gate_keys_on_the_last_node_change_not_this_commit`：脚本里用的是 `git log -1 … -- services/execution-node`，且闸门在打包之前 |
| 方向没写反 | `test_the_gate_accepts_equal_or_newer_nodes_and_rejects_older_ones`：用真实 git 验「相等放行 / 更晚放行 / 更早拒绝」 |
| 源码摘要与 lock 一致 | `test_node_source_digest_matches_the_lock` |
| 摘要真的依赖源码 | `test_the_source_digest_actually_depends_on_the_source`：改一个字节就变；`target/` 里的文件不影响 |

第二条是重点：这条判据的全部风险就在 `git merge-base --is-ancestor <目标> <节点>` 的
**参数顺序**上，写反了会变成「节点越新越拒绝」。读脚本文本证明不了方向，所以用真实 git 跑。

## 一处刻意的取舍

`services/execution-node/**` **没有**加进 `cd.yml` 的 `paths`。加了会让「只改节点」的合并
拉起 CD、重部署同一份网关并重启生产，而那时并没有不同版的风险（网关没动）。风险来自
「网关动了而节点没动」，闸门只需要在网关真要部署时说话。

## 未完成业务验收

- 本片是流程判据，不产出业务结论。
- **CI 不构建 Java** 这一点没变；变的只是「节点源码改了但记录没更新」现在会红。
- 闸门的远端读会让 `--dry-run` 碰一次 41 —— 与既有的依赖漂移检查同一行为。dry-run 的
  文档写着「不碰 41」，这处不一致是既有的，本片没修。
