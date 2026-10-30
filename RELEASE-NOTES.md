# 发布说明

面向用户的发布说明，由人撰写。格式沿用 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)。

> **这个文件不是 `CHANGELOG.md`，两者不要合并。** 实测 2026-10-23：用
> `dev-changelog` 从 Conventional Commits 生成的结果是纯 commit 标题，而本文件的条目
> 讲的是「用户会感知到什么、为什么、有什么代价」，两者**零交集**——它们记录的是不同的
> 事实。`CHANGELOG.md` 是生成物（gitignored，发布时由 `cd.yml` 产出）；本文件是人写的。
>
> **从 `0.2.0` 起，每一版都由人写面向用户的说明。** `0.1.0` 一节此前是 509 条
> commit 标题与中文条目的混合转载，已冻结为指向 git 历史的指针——见该节。

## [0.2.0] - 2026-10-23

### Fixed
- 管家端订单被拒时向公司实时店铺确认一次：令牌里的 `shop_ids` 是登录快照，而公司列表
  接口每次实时查；登录后店铺绑定被改会出现「列表有、判定无」。现在快照命中直接放行，
  仅在判不可见时确认一次（ADR-0088）。
- 隔离栈改绑智能体后同时核对模型、指令、应用与成功信封，避免只改了模型就误报整次改绑成功。
- 工单订单（`ch_order_info.user_id='-1'`）的主人写在 `consumer_id`，`self` 范围此前只认
  `user_id`，于是这类订单对**它的主人自己**也回「这个订单不属于当前账号」。判据改为
  `(CASE WHEN user_id='-1' THEN consumer_id ELSE user_id END) = <会话 C 端 id>`，
  计费模板的存在性检查同步收口。空白/NULL 的 `user_id`（渠道/OCPI 单据）仍不算本人。
- 整轮成功通知的省略/null 原因不再匹配已记录的非空原因；冲突返回 409，既有无原因重试仍返回 200。

### Added
- 消费者委派成功响应包含同 run 充电状态快照，交付前复核本人授权、绑定、状态与期限；不二次读取业务。
- M3 正常登录入口验收工具：私有会话文件、零网络预检、一次派发、同 run 证据核对；补齐 App 接线与验收手册。
- 隔离栈的**预检**不再按 `basename` 判「两侧同一份清单」。判据改为按渲染后的 compose
  把两侧的容器内路径解析成宿主路径，再要求指向同一个真实普通文件；文件缺失、符号链接、
  目录、组/他人可读都各自拒绝，0400 与 0600 都在契约之内。此前「节点开关缺失」与
  「节点路径没有挂载」两种坏态都会印 PASS（实测反例见
  `tests/tooling/test_m2_live03_manifest_preflight.py` 的两条回归）。
- 隔离栈的**复跑材料**入仓：`deploy/m2-live-03/compose.identity.yaml`（只换 Gateway 入口
  进程的覆盖层）、`identity.env.template`（身份三件套键名）、
  `tools/experiments/m2_close/m3live/live_replay.py`（一次派发 / 按 run 采证 / down 后清理与生产面对照）。
  握手令牌只从宿主的 0600 文件读，不接受命令行参数；失败不重试，同一输出目录只允许发一次。
- 95 环境（`api.qumall.qushiyun.com`，主机侧键 `qy-gw-120`）有了事实页 `docs/agents/env-95-facts.md`，并在 `.dev-host.toml` 声明为可只读访问的目标。
  记录内容包含：代号与 41/36 的判别、它的数据面为空这一观测、以及**没有** `env-95.toml`
  这一事实——此前 agent 只能从散落三处的半句拼出这些。
- Gateway 可在签发时固定执行配置身份，并在领取及最终放行事务内核对身份、终态与有效期。
  配置默认关闭；启用后缺失或不匹配的身份被拒绝，已拒绝记录不能通过重试恢复。
  数据库幂等新增三列，历史行保持 NULL；未启用时保留原回执字段。
- 执行节点接入执行配置守卫（M3-NODE-GUARD-01，节点侧）：受控清单
  （`AIOPS_EXEC_MANIFEST`，默认不配置）为期望值的唯一来源，身份＝清单原始字节 sha256；
  启用后在**调用模型之前**按面拒绝配置漂移 —— 派发体的 provider/adapter/模型/endpoint、
  构造出来的模型的标识与有效选项、解析出来的工具集合的 name+description+schema、
  最终 `Prompt` 的完整 system 集合与消息形状（固定无历史模式）。
  清单缺失/权限过宽/非法 JSON/schema 不受支持/`history_policy` 非 `none` 在装配阶段拒绝，
  不降级；未启用时行为与今天逐字一致。
- Gateway 新增整轮终局端点 `POST /v1/internal/charging-status/completions/{run_id}`：
  启用执行配置清单的 run 在领取并缓存快照后保持 `running`，只有节点显式确认（身份逐字相等、
  仍在运行、已有快照、未过期）才转 `succeeded`；失败与成功同一事务、同一 TTL 原语；
  未启用清单时该端点不改任何列。路由总数 65 → 66。
- 执行节点回传本 run 实际验证过的 `X-AI-Ops-Exec-Config`，并在整轮结束时按
  「末轮 `finishReason=STOP` + Gateway 回执校验」确认成功；同 JVM 内相同 run 的重入在本地拒绝。
- 节点侧合成验收工具 `tools/experiments/m2_close/m3node/`（`run.sh` 单命令复跑；缺锁定依赖即非零退出），
  用真实适配器 + 真实工具管理器 + 本机合成上游驱动真实装配的**流式**工具循环。

### Notes
- 本条目为节点侧实现，**未接 Gateway**：签发时固定期望、领取时对照并持久化、
  `agent_version` 回执留到下一片。本地合成结果不证明真实供应商、真实订单或生产装配。

## [0.1.0] - 2026-10-18

项目起点。这一版**没有**逐条发布说明 —— 此处此前放着 509 条（266 条英文 commit 标题 +
243 条中文条目）的混合转载，与本文开头「面向用户、由人撰写」的声明矛盾：它们记的是
「谁提交了什么」，不是「用户会感知到什么」。

详细提交见 git 历史（`git log --oneline v0.1.0` 或该 tag 之前的提交）。从 `0.2.0` 起，
每一版都由人写面向用户的说明。
