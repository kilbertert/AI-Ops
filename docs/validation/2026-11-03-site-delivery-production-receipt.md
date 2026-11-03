# 站点清单独立交付：合并、部署与两端只读验收

执行时间 2026-11-03（UTC 08:19–09:0x，北京时间 16:19–17:0x）。环境：41 生产。
PR：#1320 `fix/site-evidence-delivery`，squash 合并为 `07717e66`；补丁版本 `1.0.17`。

## 一、合并前必须处理的一件事：基线落后

PR 在合并前是 `BEHIND`（主线已被 #1321 推进到 `2eceaee5`）。三条确定性检查在旧基线上全绿，
但 Ruleset 要求分支当前。先 `git merge --no-ff origin/main` 并入主线，推送后在新 head
（`1b21c922`）上重跑，三条检查（Workflow policy / verify / spec-tree lint）全部 SUCCESS，
Devin Review SUCCESS，无未解决讨论，`mergeStateStatus=CLEAN` 后才 squash 合并。

合并时 `docs/agents/current-delivery-state.md`、两份 H5 撤回回执出现自动合并冲突以外的
内容差异（#1321 订正了同一段归属表述）；合并后按主线版本为准，未回退他人订正。

## 二、CD 被一个已合并 PR 的 update-branch 任务挡住

合并 21 秒后 CD run `41602457576` 创建，但**一直 `queued`**。按 `env-41-runbook.md` §5.4 的三步查法：

- `deploy-41` job：`status=queued`，未分配 runner；
- runner `server-dev-runner-AI-Ops`：`online`、`busy=true`（仓库只有一个槽位）；
- 占着它的是 run `41597864159`（`Agent Update Branch`），08:07:52 起已跑 25 分钟，
  其分支 `fix/operator-route-migration` 对应的 **PR #1318 早已 MERGED**。

同批还有两个排队中的同类 run（`docs/consumer-receipt-correction` → #1316 MERGED、
`docs/site-note-incident-receipt` → #1317 MERGED）。三个都指向已合并 PR，没有产出价值。
取消这三个后 runner 释放，CD 在 08:32 启动并成功：

```text
pre-version=1.0.0+7116edd0aeb6
{"ok":true,"service":"aiops-gateway","version":"1.0.17+07717e66d32c",...}
/health version 含 07717e66d32c ✓      逐文件 sha 一致 ✓
```

这不是 CD 缺陷，也不是新问题：runbook §5.4 已记录过同一形状。本轮未改
`agent-update-branch.yml`（属策略管理 workflow，改它要走评审）。

## 三、部署后基线：只换代码，没换路由

部署前后各取一次只读快照（`before.private.json` 与部署后同样字段）：

| 项 | 部署前 | 部署后 |
|---|---|---|
| `/health` version | `1.0.0+7116edd0aeb6` | `1.0.17+07717e66d32c` |
| 路由绑定行 | 4 条 | 4 条，逐字段相同（含 `revision` 17/19/1/2） |
| 近 30 分钟发布操作 | 0 | 0 |
| 在飞运行 | 0 | 0 |

部署的 4 个源文件与 `origin/main` 逐字节相同：

```text
OK   common/model_request.py          4896c09ced2263cd
OK   operator/operator_conversation.py 13648c7d0204622a
OK   operator/operator_cpms.py         598dca3e3d75b82f
OK   operator/site_note_review.py      c818b7e87c4f01d0
```

## 四、管家端（原 H5 聊天页）真实会话验收

用库外加密清单里的 `aiops-ulink-account-20261030` 真实登录
`ulinkmanage.h5.mall.qushiyun.com`，在聊天页发问并按其返回的 `qa_id` 回读运行快照。
**判据是运行记录里的 `site_note_review.verdict`，不是答复措辞**：

| 用例 | qa_id | 审核结论 | 交付形状 | 页面可见 |
|---|---|---|---|---|
| 列举「我有哪些站点？」 | `qa_6e33f343…` | `allow` | 说明 + 系统清单（161 个，展示 10，另有 151） | 是 |
| 无匹配「QZXVNOVTHREE」 | `qa_498c59ba…` | **`reject`** | **说明已隐藏**，系统清单照常交付 | 是 |

第二行正是本次改动的目标行为：说明被判不合格，`answer.text` 被清空，
`rendered_result` 里的清单仍完整交付，运行状态仍为 `succeeded`。
两条运行的 `gateway_version` 均为 `1.0.17+07717e66d32c`。

### 回读冻结（TTL 内）

新发一次「我有哪些站点？」（`qa_7d8c0524…`），交付完成后立刻连读两次：
两次均 HTTP 200、正文逐字相同（705 字符）。同一 `qa_id` 不因回读而重算正文。

### 一个需要写清楚的非缺陷

更早的两个 `qa_id`（08:37 那批）在约 2 分钟后回读得到
`410 OPERATOR_RESULT_RETRACTED`。查明原因是 `run.expires_at` 到达
（运行 TTL 180 秒，`operator_cpms.result()` 里既有的判据，`git log -S` 指向
`dc9e3eac`，**不是本次改动引入**）。因此上面这组回读冻结用的是 TTL 内的新运行，
而不是拿过期运行反推「回读坏了」。

## 五、Dify 原生预览验收

控制台会话已过期（`OPERATOR_TRIAL_LOGIN_REQUIRED`），按登录代理重新登录：
验证码走 `/__aiops/login/captcha?randomStr=<页面自己的 randomStr>`，用 ddddocr 识别。
**踩到的坑**：最初自己生成 randomStr 去取验证码，再提交另一个 randomStr，会被判
`COMPANY_LOGIN_REJECTED`；连续 5 次触发 `AIOPS_LOGIN_PROXY_BACKOFF_THRESHOLD=5/300s`
的 429 退避窗口。改用页面 `<img>` 上的 randomStr 后一次通过。

在应用 `137cc417…` 的 Dify 原生预览里发同一组问法，读 `/__aiops/operator-preview`
的 SSE：

| 用例 | 运行 verdict | 预览正文 |
|---|---|---|
| 列举 | `allow` | 说明 + 清单（161 个，展示 10，另有 151） |
| 无匹配（第 1、2 次） | `allow` | 说明 + 无匹配文案 |
| 无匹配（第 3 次） | **`reject`** | 只有系统文案 + 「查询结果已交付；说明未通过审核，已隐藏。」 |

第三行证明试用路径的隐藏提示确实落在客户端可见的正文里（ADR-0195 提到的
「Dify 预览只读首个文本块」在这里不构成问题，因为提示写在正文尾部同一块内）。

## 六、失败样本（不隐去）

- `/v1/dify/trial` 两次 `operator_failed`（`qa_3c9f6e51…`、`qa_af1d850e…`）：
  网关日志分别为 `operator_answer_rejected reason=unsupported_fact detail=status line=3`
  与 `reason=json_parse json_syntax`。这是**模型输出被判不合格后整轮拒绝**，
  与本轮改动设计的边界一致（结构/事实不合格仍拒绝），不是审核器拒绝说明。
- 首次跑原生预览脚本时 assertion 失败（403 `OPERATOR_TRIAL_LOGIN_REQUIRED`），
  保留该次记录；重新登录后的结果不覆盖它。

## 七、边界

- 本次只做**只读**验收：未改现役应用、提示词、路由、知识授权；未新增业务权限；
  未在生产写任何业务数据。产生的只有查询类运行记录。
- 审核结论本身是**模型判断**，`allow` / `reject` 同源失效风险仍在（ADR-0195 已记）。
  本轮没有做人工业务金标准对照，不能据此宣称准确率。
- 双端提示词完整真实生命周期、双租户 T13、结算状态与关联占枪费订单仍待完成，
  不在本回执范围内。

## 八、可复现步骤

```bash
# 0. 合并前确认分支当前、检查全绿
gh pr view 1320 --json mergeStateStatus,statusCheckRollup

# 1. CD 排队时先看 runner 被谁占着（runbook §5.4）
gh api repos/kilbertert/AI-Ops/actions/runners -q '.runners[] | "\(.name) busy=\(.busy)"'
gh run list --status in_progress --limit 10 --json databaseId,workflowName,headBranch

# 2. 部署后只读快照（只读连接，不写）
dev-host exec 41 --allow-service-exec --timeout 60 -- /opt/aiops-41/.venv/bin/python - \
  <<'PY'   # 见 §三 的 SQL；库按 mode=ro 打开
PY

# 3. 两端验收脚本（凭据只从库外 age 清单读进内存，不落盘、不打印）
python3 ~/.cache/aiops-resume-20261103/live/formal_verify2.py    # 管家 H5
python3 ~/.cache/aiops-resume-20261103/live/native_login_verify.py  # Dify 原生预览
```

登录代理的会话寿命与退避窗口见 ADR-0136 / `docs/agents/env-41-runbook.md`。
