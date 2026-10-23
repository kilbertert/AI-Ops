# 源库没了就退休 + 主页列表的缓存不一致：验收记录

**范围**：`dify_knowledge_datasets.py`、`dify_console_admin.py`、`admin_cli.py`、
`gateway_api.py`、前端补丁 `02-web-sync-on-knowledge-picker.patch`、`build-web-image.sh`、
`.gitignore`。**未做真机验收**（本片只到代码层 + 一次部署验证），见文末。

## 一、运营报的两个问题

2026-10-24 真机，同一轮里：

1. 在 RAGFlow 删掉「11」→ **Dify 的卡片还在**（主页和编排页都没有同步删除）。
2. 在 RAGFlow 新建「22」→ **编排页能看到，主页看不到**。

## 二、问题一：删除从来没被实现过

ADR-0122 的约束 3 明写「绝不删，`orphans` 只进日志」。所以「RAGFlow 删库 → Dify 卡片消失」
**从来不存在** —— 不是坏了，是没做。

按用户决定改成自动删，四道保护（ADR-0123）：

| # | 保护 | 判据 |
|---|---|---|
| 1 | 默认 `retire_orphans=False` | 默认路径不发任何删除 ✅ |
| 2 | 只删 `tenant-<id>:<库>` 派生形状 | `kb-canary-2` / `tenant-<id>` 永远不是孤儿 ✅ |
| 3 | 上游读失败不得进 `run()` | 网关在读失败时就 return ✅ |
| 4 | `--dry-run` 永不删 | ✅ |

**变异复验**：

| 变异 | 转红 |
|---|---|
| 默认就删（去掉开关） | 2 条 |
| `dry-run` 也删 | 2 条 |

## 三、问题二：不是同步问题，是同一次同步两个页面策略不同

真机证据是决定性的：

```
API 返回：22, 11, 41(1), aiops-41-kb-prod-kefu   ← 22 在
Dify DB：  4 条，permission 全 all_team_members，maintainer 全一致
桥日志：   knowledge sync: tenant=-1 created=1
```

所以数据是写进去了，看的那个页面没刷。

**根因**：`web/app/get-query-client.ts` 的全局 `staleTime = 5 分钟`。
主页的 `useDatasetList` 没覆盖它；编排页的选择器显式写了 `staleTime: 0`。

修法：同步成功后调 `useInvalidDatasetList()` —— **Dify 自己建完库用的就是它**
（`create/empty-dataset-creation-modal`）。原来只 `refetch()` 本弹窗。

## 四、顺带修的两个构建脚本缺陷

| 缺陷 | 后果 |
|---|---|
| 没删 `COPY … ./targets/vinext` | 构建直接失败（`dist/standalone` 不存在） |
| 构建前不清理工作树 | **补丁被静默跳过**（`1 out of 1 hunk ignored`）——比失败更危险，产出缺改动的镜像 |

第二个是真机部署时踩到的：第二次构建时 `web/service/knowledge-sync.ts` 已存在，
`patch` 在非交互环境默认答"不"，那条补丁被跳过而构建继续。

## 五、判据与结果

```
./tools/test.sh  →  3757 passed, 9 skipped, 0 failed
ruff check / format / adr check  通过
```

前端补丁：`patch -p1 --fuzz=0` 与 `git apply --check` 都通过。

## 六、未完成的部分（明说）

- **未做真机验收。** 删除功能与主页缓存修复都只到代码层。
- **未重建 web 镜像。** 前端补丁的改动需要重新构建并换容器（约 4 分钟 + 短暂不可用）。
- **删除的端到端未跑过**：RAGFlow 删库 → 打开弹窗 → Dify 卡片消失，这条完整动作没验。
- **`delete_dataset` 只在删 `orphans` 时被调用过**，没在真实 Dify 上跑过一次删除。

**结论：未完成业务验收。**
