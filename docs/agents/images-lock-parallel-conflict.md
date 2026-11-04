# images.lock.json 的并行冲突：现状梳理与待决策

日期：2026-11-04。写给接手者（Codex/其他会话）。**本文只陈述事实与取舍，不含已执行的决定。**

---

## 一、现象

同一天里，`git push` 被同一个文件挡下了**三次**：

```
SERVER POLICY: push overlaps uncommitted changes in another worktree.
overlap worktree=/home/claude/Projects/.worktrees/AI-Ops-operator-diagnosis-failure
   branch=fix/operator-diagnosis-failure path=deploy/m2-live-03/images.lock.json

overlap worktree=/home/claude/Projects/.worktrees/AI-Ops-consumer-free-question
   branch=fix/consumer-free-question path=deploy/m2-live-03/images.lock.json
```

原因是**任何改动 `src/` 的 PR 都必须重建这个锁**，于是两个并行会话必然在它上面相撞。
策略禁止用 `--no-verify` 绕过，只能等或换分支名重推。

今天为此付出的实际成本：

- 三次 push 被拒，三次改分支名重开 PR
- 其中一次 GitHub 侧持续返回 `Internal Server Error`，旧 PR（#1344）只能关闭、新开 #1348

---

## 二、这个锁到底是什么

`deploy/m2-live-03/images.lock.json` 记录**受控试运行栈**的制品身份：

| 字段 | 含义 | 何时变 |
|---|---|---|
| `gateway_image.source_sha256` | `src/` + tools + requirements + Dockerfile 的**归档摘要** | **任何 `src/` 改动** |
| `gateway_image.uv_lock_sha256` | 依赖清单摘要 | 改 `uv.lock` |
| `gateway_image.built_at` | 构建时刻 | **每次构建** |
| `node.sha256` / `source_sha256` / `source_commit` | Java 节点制品 | 改 `services/execution-node` |
| `base_images.refs` | 基础镜像 image ID | 换基础镜像 |

`image_id` **刻意不写进仓库**（每次构建都不同），它住在宿主上的 `dist/.../identity.json`。

**它是「内容身份」，不是「版本号」。** 源码一变，摘要必然变——这是设计意图，不是缺陷。

---

## 三、谁在读它（关键）

| 消费者 | 是否读 | 说明 |
|---|---|---|
| **生产 CD**（`.github/workflows/cd.yml`） | **不读** | 生产部署走 `deploy/deploy-41.sh`，与该锁无关 |
| **CI**（`.github/workflows/ci.yml`） | **不读** | 只跑 pytest |
| `deploy/m2-live-03/checks.sh` | 读 | **受控试运行栈**的 preflight |
| **`deploy/node-runtime/source_digest.py`** | **读** | **正式节点运行**的源码摘要核对（`--check`） |
| **`deploy/node-runtime/README.md`** | **读** | 明写「平台、节点和镜像身份**沿用** `../m2-live-03/images.lock.json`」 |
| `tests/tooling/test_m2_live03_deploy.py` | 读 | 校验"锁与当前源码一致" |
| `tests/test_node_runtime_deploy.py` | 读 | 节点摘要与变更单一致 |

⚠️ **这条要先纠正一个容易得出的错误推论**（初稿就写错了）：
**锁不只为合成栈服务 —— `deploy/node-runtime/` 这条正式节点运行链路也在沿用它的制品身份。**
所以"把 `m2-live-03` 整个归档"会连带打断节点运行链，不能只凭"41 上没跑合成栈"就下结论。

不过这两条链**读的是不同字段**：节点链路读 `node.*`，Gateway 镜像读 `gateway_image.*`。
`gateway_image.source_sha256` 随任何 `src/` 改动而变，而节点链路并不消费它 ——
**冲突压力的来源是 Gateway 那个字段，不是整个文件。**

---

## 四、那套栈现在的状态

| 事实 | 结论 |
|---|---|
| `deploy/m2-live-03/README.md` 自述是「41 同宿主，隔离」的受控试运行栈（隔离 Gateway `:8794`） | 是部署件 |
| 41 上 `docker ps -a \| grep m2live` → **0 个容器** | 合成栈当前未运行 |
| 41 上 `/opt/aiops-m2-live` **不存在**（`M2LIVE_DIST` 的默认根） | 合成栈未在这台机器上部署过 |
| **但 `deploy/node-runtime/` 正在 41 上跑**（`aiops-node-runtime` / `aiops-node-callback` 均 active） | **节点链路是活的** |
| 生产 `aiops-gateway-41.service`（`:8788`）独立运行，不读锁 | 与 Gateway 无关 |
| 锁文件在 2026-11-04 当天被 5 个 PR 更新过（#1339/#1340/#1341/#1342/#1347） | 仍在被每个 `src/` 改动拖累 |

**结论要分开说**：

- **合成栈**：41 上没在跑，也无部署痕迹 —— 但**不能**因此说它已废弃，需接手者确认它的定位。
- **节点运行链路**：**活的**，正在 41 上运行，且读这个锁的 `node.*` 字段。
  任何涉及 `services/execution-node` 的改动仍然必须更新它。

---

## 五、三个可选方向（未执行任何一条）

### A. 锁只在真正要跑试运行栈时重建

- 做法：把 `test_gateway_context_digest_is_reproducible_and_matches_the_lock`
  从"必须与当前 main 一致"改为"仅当锁被显式标记为 current 时才校验"

⚠️ **不能简单用 `pytest.mark.skip`**：`.github/workflows/ci.yml:245` 有一道闸门
—— 跳过数**上限 10**，而基线已经是 9。多一跳就红：
如果要在 A 方案里跳过一条测试，**必须同一个 PR 里把上限或基线一起调整**，
否则 pytest 全绿、CI 仍然失败（而且失败信息会指向"跳过数超过基线"，与真正原因无关）。
- 保留：`context_digest.py --check` 本身不变，部署试运行栈前照常用它
- 代价：试运行栈的锁会**落后于 main**，下一次真要用它时必须先重建 + 单独提交

### B. 让测试只校验「可复现性」，不校验「与当前 main 一致」

- 做法：**分两件事说清比较对象**
  1. **比什么**：比 `context_digest.py` 算出的**构造上下文摘要**（`src/`+tools+requirements+
     Dockerfile 的路径+内容 sha256），**不是**镜像 ID。镜像 ID 每次构建都不同
     （构建时间戳进镜像元数据），本来就不在锁里 —— 见锁文件里那条 `_comment`。
  2. **和谁比**：同一份源码**跑两次** `context_digest.py`，两次结果必须逐字节相同
     （这才是"可复现"的判据，也是历史上真的坏过的地方：`sort -z` 依赖 locale）。
- 保留：防"摘要不可复现"这个真实缺陷
- 代价："锁落后于源码"这件事变得没人管 —— 需要另一处（部署时）来管

### C. 只把 `gateway_image.source_sha256` 从"随 PR 更新"里摘出来（**修正后的建议**）

- 前提：`deploy/node-runtime/` 只消费 `node.*`，不读 `gateway_image.source_sha256`
  （`source_digest.py:44` 读的是整个文件，但比对用的是节点字段 —— 需接手者复核）
- 做法：让 Gateway 镜像的"内容身份"记录在**部署时**产生（像 `identity.json` 那样），
  而不是随每个 `src/` 改动的 PR 落进仓库
- 保留：节点摘要仍随 `services/execution-node` 改动更新，且仍需评审
- 效果：**冲突源是 `gateway_image.source_sha256` 这一个字段**，摘掉它冲突就消失；
  不必动整份锁，更不必归档合成栈
- 代价：需要改 `build-image.sh` 与那个 pytest 的契约

> **原 C 方案（归档整个 `m2-live-03`）已撤回**：它会把仍在运行的节点链路一起打断。

---

## 六、需要谁回答什么

1. **`m2-live-03` 还打算用吗？** —— 这一条决定选 A/B 还是 C
   - 证据：41 上既无容器也无 `/opt/aiops-m2-live`；文档说它是"部署件"却没有任何一次部署记录
2. 如果要用：它下一次部署在什么时候？—— 决定 A 里"锁落后"能容忍多久

---

## 七、不属于本议题的（避免混淆）

- 生产 Gateway 的部署身份由 `/health` 的 `version`（`1.0.19+<sha>`）与
  `deploy/deploy-41.sh` 的版本一致性闸门保证，**与 `images.lock.json` 无关**
- `uv.lock`、`VERSION`、`pyproject.toml` 的三处版本一致性由
  `tests/test_release_version.py` 保证，同样与这个锁无关
