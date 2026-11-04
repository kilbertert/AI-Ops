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
| `tests/tooling/test_m2_live03_deploy.py` | 读 | 校验"锁与当前源码一致" |
| `tests/test_node_runtime_deploy.py` | 读 | 节点摘要与变更单一致 |

也就是说：**这个锁只服务于 `m2-live-03` 那套合成栈。**

---

## 四、那套栈现在的状态

| 事实 | 结论 |
|---|---|
| `deploy/m2-live-03/README.md` 自述是「41 同宿主，隔离」的受控试运行栈（隔离 Gateway `:8794`） | 是部署件 |
| 41 上 `docker ps -a \| grep m2live` → **0 个容器** | 未部署 |
| 41 上 `/opt/aiops-m2-live` **不存在**（`M2LIVE_DIST` 的默认根） | 从未在这台机器上跑过 |
| 生产 `aiops-gateway-41.service`（`:8788`）独立运行，不读锁 | 与它无关 |
| 但锁文件在 2026-11-04 当天被 5 个 PR 更新过（#1339/#1340/#1341/#1342/#1347） | 仍在被每个 `src/` 改动拖累 |

**这一点需要接手者核实**：这套栈是"暂时没跑"还是"已经废弃"。
文档里没有找到明确的退役记录。

---

## 五、三个可选方向（未执行任何一条）

### A. 锁只在真正要跑试运行栈时重建

- 做法：把 `test_gateway_context_digest_is_reproducible_and_matches_the_lock`
  从"必须与当前 main 一致"改为"仅当锁被显式标记为 current 时才校验"，
  或改成 `skip` 并附条件
- 保留：`context_digest.py --check` 本身不变，部署试运行栈前照常用它
- 代价：试运行栈的锁会**落后于 main**，下一次真要用它时必须先重建 + 单独提交

### B. 让测试只校验「可复现性」，不校验「与当前 main 一致」

- 做法：测试里跑一次完整构建、比对两次构建是否逐字节相同，但不比对仓库里的锁值
- 保留：防"摘要不可复现"这个真实缺陷（历史上真的发生过：`sort -z` 的 locale 依赖）
- 代价：CI 里跑一次完整镜像构建，慢；且"锁落后于源码"这件事变得没人管

### C. 归档 `deploy/m2-live-03/`

- 前提：确认这套受控试运行栈**已经废弃**（41 上从未部署、也没有部署计划）
- 做法：按仓库惯例移入归档，锁、checks.sh、相关测试一并处理
- 效果：冲突源彻底消失
- 代价：如果它其实还要用，这一步代价很大

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
