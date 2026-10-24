# 页面自同步（A+B）：验收记录

**范围**：新补丁 `03-web-sync-on-page-visible.patch`、`02` 瘦身、`build-web-image.sh`、
`knowledge_dataset_sync_guard.py` 重写（ADR-0125）。
**已做真机验收**，且验收抓出了一个只在真机上才看得见的 bug。

## 一、运营的原话与真实痛点

> 在 RAGFlow 页面上进行了知识库相关的操作之后能否丝滑地体现回 Dify？不然每次都需要
> 强制硬刷新 Dify，对用户完全不友好。

ADR-0122 的同步只在「打开添加弹窗」时触发。从 RAGFlow 切回 Dify 时页面**已经挂载**，
不会重新拉列表 —— 同步是通的，但运营看不见。

## 二、做了什么

`web/hooks/use-knowledge-sync.ts`：

| 触发 | 覆盖 |
|---|---|
| 挂载 | 打开知识库页那一刻 |
| `visibilitychange` / `focus` | 从 RAGFlow 切回来那一刻（主路径） |
| 每 60s，**仅当页面可见** | 页面一直开着、别处改的 |

有变化才 `invalidDatasetList()`；没变化不失效（列表不闪）。

**为什么没做 RAGFlow 回调**：运营最初倾向它，但那条路要改上游 RAGFlow 的三个写入口
（官方镜像 + 只挂三个配置文件），而**回调丢一次 Dify 就永远不同步**，补偿要一条有状态
的重试链。轮询的失败模式是延迟，回调的失败模式是永久不一致。理由写进 ADR-0126。

## 三、顺带修掉的三个构建脚本问题

2026-10-24 一天内连续踩到，每个都是"构建报成功、产出不对"：

| # | 问题 | 症状 |
|---|---|---|
| 1 | `02` 补丁包含 `01` 的全部内容 | 每次被判"已应用"，`--batch` 自动 `-R` **反向打回去** |
| 2 | `.orig` / `.rej` 残留 | `patch` 误判为"已应用过" |
| 3 | 没有"改动真的进了源"的判据 | 补丁静默跳过，构建照样出镜像 |

修法：`02` 瘦身为两个文件；`patch` 加 `-N`；`git clean -fdq` 清全部；**打完按三个标记
grep，缺一个 `exit 1`**。

## 四、真机验收（Playwright + 真实浏览器，生产 36）

用了构造身份登录 Dify（`system`），在真实页面上看网络请求与列表变化。

### 4.1 抓到的第一个 bug：hook 挂错了组件

补丁把 hook 挂在 `LegacyList` 上。而本环境 `knowledge_fs_enabled=true`，`List()` 分发到
`KnowledgeFsList` —— **hook 根本没执行**。真机表现：打开 `/datasets` 时
`knowledge-sync` 请求数为 **0**。

修法：挂到分发器 `List()`，并补上挂载时 `run()` 一次（原版只注册监听，不跑）。

### 4.2 抓到的第二个 bug：闸把「切回来」那一次丢掉了

修完 4.1 后：

| 步骤 | 结果 |
|---|---|
| 打开 `/datasets` | 1 次 `knowledge-sync`(200) ✅ |
| 用 kb-service API 建 `e2e-验证-20261024`，切走再切回 | 自动出现在 Dify ✅ |
| 建 `e2e-切回-20261024`，切走再切回 | sync 发了，**库没出现** ❌ |

网关日志原文（36 上的 bridge）：

```
08:16:52  knowledge sync: tenant=-1 created=0
08:17:01  knowledge sync: tenant=-1 created=0   ← 距上一次成功 9 秒
08:18:14  knowledge sync: tenant=-1 created=1   ← 等了 73 秒才出现
```

08:17:01 是运营切回来的时刻，被 `RETRY_FLOOR_SECONDS = 15` 的**成功下限**吞掉。
网关侧 `libraries_for` 当时已经能产出这条库，所以**不是同步逻辑漏了它，是闸把它丢了**。

修法（ADR-0125）：闸改成「在飞时排队，跑完再跑自己这一次」，**删掉时间下限**。
下限在断言"上游这 15 秒不会变"，而这恰好是整条链路的前提的反面。

### 4.3 修复后

| 判据 | 结果 |
|---|---|
| 连续两次 sync 都真的执行（无时间下限） | ✅ 单测 `test_every_call_runs_the_work` |
| 并发调用排队后仍然执行 | ✅ 单测 `test_a_concurrent_caller_waits_and_then_runs` |
| 上游卡死时排队有上限 | ✅ 单测 `test_a_wedged_pass_is_skipped_after_the_wait_bound` |
| 闸失败不抛 | ✅ |
| 闸测试 + `test_dify_knowledge_datasets` + `test_transport_convergence` | ✅ 32 passed |

## 五、明说的限制

- 闸的修复要随 41 网关 CD 部署才在生产生效；本文档记录的是代码 + 单测层。
- `kb.mall.qushiyun.com` 从开发机不可达（HTTP 000），从 41 可达（200），所以"点卡片
  跳进 RAGFlow"这一步无法在开发机上驱动；测试库改用 kb-service API 建，语义等价。
- 60 秒轮询的实际体感未验：运营觉得"够快"还是"还是慢"，只有他用过才知道。

## 六、测试数据清理

两条 `e2e-*` 库已在 RAGFlow 侧删除（`DELETE /kb/knowledge-bases/{id}` → 200）：

| 库 | RAGFlow id | 结果 |
|---|---|---|
| `e2e-切回-20261024` | `3b9236b2…` | 200 已删 |
| `e2e-验证-20261024` | `1fa22854…` | 200 已删 |

RAGFlow 侧剩余 `11` 与 `41(1)`，都是真库。

Dify 侧那两条对应 dataset **没有手工删**：它们现在是孤儿，`retire_orphans` 会在下一次
同步（页面 60 秒轮询）时自动退休并留一条 `retire <knowledge_id> -> dataset <dataset_id>`
日志。这也顺带验证了 ADR-0122 的退休路径在真实数据上工作。
