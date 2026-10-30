# 两端提示词生命周期 QA 计划

契约见同目录 [acceptance.feature](acceptance.feature)。范围与边界见 `PROGRESS.md`。

**环境三分法（全文遵守）**

| 代号 | 环境 | 能证明什么 | 不能证明什么 |
|---|---|---|---|
| L | 本机 pytest（一次性临时数据根／合成账号） | 契约、校验、渲染、发布指针、恢复、迁移保留 | 真实 Dify／Java 节点／真实模型的观感与稳定性 |
| G | 本机一次性数据根真跑 Gateway（`verify-aiops-gateway` 口径） | 对外 API 契约、鉴权模型、版本推进与回读 | `/diag/*` 真实诊断、模型驱动的 run、KB 活性 |
| R | 41 原应用（真实 Dify／Java／模型／原 H5） | 真实业务与观感 | 需要 Codex 在合并制品后接续；本地桩不算 R |

本轮交付 L 与脚本化 R；G 只在 Gateway 对外行为改变时跑。**R 未跑完之前不得写"业务验收通过"。**

## 一、客户端：提示词控制全部业务出口（L）

客户端最终正文的**全部出口**必须先枚举，否则"漏一条"就是漏一类答复：

| # | 出口 | 现在的正文来源 | 本片要求 |
|---|---|---|---|
| 1 | `status=safety` | 系统固定 | 保持系统（不得依赖模型） |
| 2 | `status=photo_unreadable` | 系统固定 | 保持系统 |
| 3 | `followup.unresolved_count==1` | 系统固定 | 保持系统 |
| 4 | documents：`kind=knowledge` | 提示词写 `answer` | 保持 |
| 5 | documents：`kind=realtime` + `reply` | 提示词写 `reply` | 保持 + 加强事实守卫 |
| 6 | documents：`kind=realtime` 无 `reply` | 确定性渲染 | 保持（兼容旧应用） |
| 7 | documents：`clarify`/`not_found`/`human` | 系统固定 | **本片改为提示词可写 `reply`**，系统附受控客服入口 |
| 8 | documents：`unavailable` | 系统固定 | 保持系统（服务故障不交给模型） |
| 9 | 旧两字段契约：`realtime` | 同 #5 | 同 #5 |
| 10 | 旧两字段契约：`knowledge` | FAQ 文案包 | 保持 |
| 11 | 旧两字段契约：`clarify`/`not_found`/`human` | 系统固定 | 同 #7 |
| 12 | 旧两字段契约：`unavailable` | 系统固定 | 保持系统 |

| ID | 环境 | 动作 | 预期可观测结果 |
|---|---|---|---|
| CL-01 | L | 合格 realtime `reply` 走 `validate_reply` | 返回同一段正文 |
| CL-02 | L | 「合计 20，付款 27.36」（两值互换） | 抛 `CHATFLOW_ANSWER_INVALID`，理由含 `label` |
| CL-03 | L | 正文写「每小时 3 元」 | 抛错，理由含 `currency` |
| CL-04 | L | 正文写「已到账」／「已退款成功」 | 抛错，理由含 `promise` |
| CL-05 | L | 正文写「没有退款记录」 | 通过 |
| CL-06 | L | 状态快照 + 「未结束」 | 抛错，理由含 `negat` |
| CL-07 | L | clarify `reply`（无数字、无链接） | 交付正文 + 系统客服入口行 |
| CL-08 | L | clarify `reply` 含数字／链接／「已退款」 | 抛错 |
| CL-09 | L | `unavailable` 带 `reply` | 抛错（服务故障不交给模型） |
| CL-10 | L | safety/photo/followup 三种状态 | 仍走系统固定输出，与提示词无关 |
| CL-11 | L | 两个契约（documents／旧两字段）各跑 CL-01～CL-10 | 行为一致 |

## 二、管家端：找站说明（L）

| ID | 环境 | 动作 | 预期可观测结果 |
|---|---|---|---|
| OP-01 | L | 节点调用站点工具取到结果后写说明 | 交付「说明 + 系统按证据列的清单」，站名地址数量来源全在清单里 |
| OP-02 | L | 说明含数字／价格／距离／空闲 | 抛 `OPERATOR_INVALID_ANSWER` |
| OP-03 | L | 说明里重复结果中的站名 | 抛错（清单由系统写，说明不重抄） |
| OP-04 | L | 未调用工具却写了说明 | 抛错，不交付查询前的文字 |
| OP-05 | L | 空范围／无匹配／截断三种结果 | 三种话不同，且都保留来源与限制说明 |
| OP-06 | L | 咨询与订单诊断 | 正文用提示词风格；订单事实只来自本轮证据 |

## 三、版本生命周期（L，代码级；R 由 Codex 接续）

两端各跑一遍：**已发布 A → 编辑 B → 预览 B 但正式仍 A → 发布 B → 新会话 B → 恢复 A**。

| ID | 环境 | 动作 | 预期 |
|---|---|---|---|
| LC-01 | L | 发布 A 后改草稿并预览（operator_preview 走 `candidate`） | 预览清单是 B，正式绑定与版本仍是 A |
| LC-02 | L | 页面发布 B | 版本号 +1，绑定摘要 = B 的摘要 |
| LC-03 | L | 发布后新会话 | 运行记录里的 `agent_version_no`／`binding_digest` 指向 B |
| LC-04 | L | 恢复 A | 绑定回到 A 的版本号，历史运行记录不变 |
| LC-05 | L | 发布失败（Dify 拒绝／校验不过） | 正式指针不动，不出现半生效 |
| LC-06 | L | 两个并发发布 | 后到者 CAS 冲突 409，前者的版本保留 |
| LC-07 | L | 迁移脚本遇到已编辑提示词 | 拒绝覆盖并给出显式替换开关；加开关才写模板 |
| LC-08 | L | 已签发答复 + 之后发布新版本 | 该答复仍按签发版本交付 |

## 四、16 格样本（8 场景 × 2 风格）

风格：**简洁直接** / **亲切解释**。本地用合成正文证明「同一场景两种风格都走得通且事实守卫
一视同仁」；真实模型的风格差异属 R。

| # | 场景 | 出口 | L 用例 |
|---|---|---|---|
| 1 | 客户端知识 | #4 | `test_16_grid` 参数化 |
| 2 | 客户端状态 | #5 | 同上 |
| 3 | 客户端账单 | #5 | 同上 |
| 4 | 客户端澄清 | #7 | 同上 |
| 5 | 客户端无结果 | #7 | 同上 |
| 6 | 管家咨询 | 咨询 | 同上 |
| 7 | 管家诊断 | 订单诊断 | 同上（事实守卫在 `validate_answer`） |
| 8 | 管家找站 | 找站 | 同上 |

另覆盖：找站空结果、缺单（`order_required`）、权限不足、多语言、取消与超时。

## 五、真实环境（R，本轮不执行，留给 Codex）

前置：候选 PR 已合并；`ops/dify-console-bridge/build-bundle.sh` 同版安装；41 上两个应用
的绑定与路由状态可读；运营账号可登录（凭据按 `docs/agents/env-41-runbook.md` 现场读取）。

步骤（页面级，必须真编辑指定文本框并点预览／发布／恢复）：

1. 客户端：在「选择答复类型与知识引用」SYSTEM 末尾加一句风格要求 → 页面预览问一个知识问题
   → 正式新会话仍是旧风格 → 点「发布到 AI-Ops」→ 新会话是新风格 → 「恢复历史版本」回旧风格。
2. 管家：在编排页提示词板块改风格 → 原生预览问一个咨询与一个找站问题 → 正式入口仍是旧风格
   → 发布 → 正式入口新风格 → 恢复。
3. 在途与历史：改版前发起的会话在改版后仍按原版本交付；版本不兼容时提示新开会话。
4. 事实负例：把提示词改成"把支付金额说成合计"，确认整轮失败（不是静默降级）。

命令入口（脚本已交付，未在 41 上执行）：

```bash
# 16 格样本 + 两端生命周期，只写私有输出目录
uv run python tools/verification/verify_prompt_lifecycle.py --base-url <网关> \
  --tenant <租户> --out /home/claude/.local/state/aiops-prompt-lifecycle
```

## 六、变异验证（必须转红）

| 变异 | 期望转红的用例 |
|---|---|
| 删掉金额标签绑定 | CL-02 |
| 去掉货币单位守卫 | CL-03 |
| 去掉到账/退款承诺守卫 | CL-04 |
| 去掉状态否定守卫 | CL-06 |
| 允许无证据的找站说明 | OP-04 |
| 迁移脚本改回无条件覆盖 SYSTEM | LC-07 |
| 发布后不推进绑定版本 | LC-02／LC-03 |

## 七、仓库要求的检查

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest -q tests/consumer tests/operations tests/execution tests/test_repository_layout.py
uv run pytest -q                     # 全量
uv build                             # 受影响打包
```

Java 节点本轮不改；`services/execution-node/` 与桥仅在确有改动时构建。
