# 受控失败的业务验收：开通失败定位在真实页面上成立

日期：2026-11-04。承接 [上线与部署](2026-11-04-setup-failure-visibility-live.md)。
那份记录写的是「**未完成业务验收**」—— 本文把它补上。

## 结论

**业务验收通过。** 页面对一次真实失败显示了「哪一步、哪一类异常」，journal 里有完整栈，
两者由同一次失败产生。

## 怎么造，为什么安全

把 `/etc/aiops-41/operator-self-service/model.json` 临时改名（**内容与所有权不动**），
通过同源桥重跑作业 `33d291dd…`（**只开 operator 端的旧作业**）。

失败点是 `validate_private_file()` —— 一个纯本地检查，**发生在任何外部写入之前**：
不会创建应用、不会申请凭据、不会改路由。约 3 分钟后改回，`controlled-test-hold`
残留 0 个，文件 sha256 与原值一致。

## 三层证据（同一次失败）

| 层 | 证据 |
| --- | --- |
| 作业行（经同源桥读取） | `failed_step=operator:dependencies`，`failure_kind=PrivatePathError` |
| journal | `setup_step_failed job=33d291dd… entry=operator step=operator:dependencies kind=PrivatePathError`，后接完整栈（`validate_private_file` → `self_service_config.py:77`） |
| 页面 | 「优链充 · 优链充－页面管家端验收　管家未完成」／「管家：当前步骤未完成，可以重试；持续失败请联系平台管理员；**准备管家依赖出错（PrivatePathError）**」 |

页面那句话由三段组成，正是 ADR-0200 设计的样子：

1. `SETUP_STEP_FAILED` 的**受控基句**（既有文案，不因新字段改变）；
2. 步骤的中文标签：`operator:dependencies` → 「准备管家依赖」；
3. 异常类名原样：`PrivatePathError`。

异常**正文**没有出现在页面上，也没有进任务行 —— 只有类名。

## 取证通道（记录备查，不是网络修复）

开发机到 `dify.ranlei.work:443` 的 TLS 握手持续被重置，所以页面验收走了一条本地通道。
**可复用的启停步骤在 [`tools/verification/dify-console-tunnel/README.md`](../../tools/verification/dify-console-tunnel/README.md)**，
脚本是同一目录下的 `proxy.py`（只绑回环的 CONNECT 代理）与 `capture.py`（用库外 0600 会话截图）。

要点：隧道 `ssh -N -L 18443:127.0.0.1:443 yidong-36` → 代理 `127.0.0.1:18080`，
浏览器 URL 与 `Origin` 仍是 `https://dify.ranlei.work`，因此桥的同源校验照常生效。

**顺带证伪了一个猜想**：直接用 `https://dify.ranlei.work:18443` 访问会被桥拒绝
（「请从Dify控制台操作」）—— 说明 Origin 校验是真的在工作，不是摆设。

取证结束隧道与代理均已关闭，`pgrep` 无残留。

## 副作用与清理

受控失败给 `33d291dd…` 留了一条 `failed` 行。**先备份再删**：

- 备份：`/var/backups/aiops-41/setup-job-33d291dd-20261104T011436Z.json`（0600，1666 字节，完整行）
- 然后 `DELETE` 该行
- 删后回读：优链充剩余两条作业都是 `completed`；两条正式路由未变
  （`operator a57602c1 active rev 2`、`consumer 548b8483 active rev 1`）

## 交付状态索引的订正（在 #1336，尚未合并）

[当前交付状态索引](../agents/current-delivery-state.md) 里那一行**仍写着「未完成业务验收」**，
一直到 PR #1336 合并为止。那一行不是本记录的产物，也不由本 PR 改：

- **#1336**：改索引那一行 + 上线验收记录与开发进度的结论 + 本记录的截图附件。
  它已推送，但推送守卫要等兄弟工作区 `fix/operator-facts-relevance` 放开
  `current-delivery-state.md` 才能放行（那条分支本身改了该文件）。
- **本 PR**：只把这一段从「待做」改成实际状态，不碰任何被占用的路径。

**更早的推迟原因**（保留为历史）：第一次写这份记录时，索引与另两份同名记录被兄弟工作区
`test/consumer-lifecycle-live` 的一次未完成合并占用（`MERGE_HEAD` 存在、`UU` 冲突未解决），
所以那轮 PR 只新增了记录。

## 仍未验

1. **只验了一种失败形状**（依赖准备阶段的本地校验失败）。发布阶段、Dify 上游失败、
   `SetupConflict` 类仍只有确定性用例覆盖，没有各自的生产证据。
2. 开发机到 `dify.ranlei.work:443` 的 TLS 握手**仍然被重置**；本次是绕开，不是修复。
   该现象在本会话更早（开通重试时）也出现过，不是本次部署引入。
3. 运营能否直接从这句话定位问题，仍需要真实运营反馈；本文只证明"显示了什么"。
