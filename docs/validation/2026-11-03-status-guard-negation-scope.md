# 状态判据误拒：定位、修复与验证

日期：2026-11-03。分支：`fix/operator-status-guard-scope`；基线：`2a9f8b3f`；ADR-0196。

## 一、症状与真实面

用户报告「帮我分析这笔单的电费 2116050516255272961」时好时坏，界面报
`试运行结束，状态: failed (OPERATOR_DIAGNOSIS_FAILED)`。

该订单在 41 上的全部运行（只读查询 `agent_runs`）：

| | 次数 |
|---|---|
| succeeded | 19 |
| failed（`operator_failed`） | 53 |

不是偶发：**成功率约 26%**。失败与成功交替出现，与用户描述一致。

## 二、定位过程（第一性原理，从证据往上推）

1. **失败在 `_worker` 的兜底 except** → `update_assistant_question(status="failed",
   error_code="OPERATOR_DIAGNOSIS_FAILED")`。这个码不说明任何成因，必须往上一层找日志。
2. 网关日志按成因归并（2026-11-02 起）：

   ```text
   52  operator_completion_refused code=OPERATOR_INVALID_ANSWER
   20  operator_answer_rejected reason=unsupported_fact       ← 裸码，老版本
   13  operator_answer_rejected reason=unsupported_fact detail=status
    6  operator_completion_refused code=OPERATOR_EVIDENCE_MISSING
   14  operator_answer_rejected reason=json_parse（其中 5 次 json_syntax）
   ```

   **`detail=status` 是最大单项**，且全部落在 `line=3`（reply 的第三行，即模型写的收尾句）。
3. **失败 run 不保存模型原文**（`snapshot_json.answer` 恒为 `null`），这是本次取证的最大障碍。
   改用**重放**：从 `agent_runs.execution_manifest` 取该 run 真实的模型/提示词/工具，
   经节点自己的 model-proxy 调同一个上游，用快照里的真实工具证据喂第二轮。
4. 重放 10 次，把结果喂给**生产同一批校验函数**：**4/10 被拒**，日志形状
   `detail=status line=3 len=81/116/57/100` —— 与线上 `len` 分布同形。

## 三、根因

`consumer_facts.check_status_prose` 的否定分支是**同子句共现**判定。被拒的四句分别是：

| 句子片段 | 共现判据命中 | 实际语义 |
|---|---|---|
| 我这边暂**未**取到本次**充电**的过程样本 | 未 + 充电 | 否定「取到」 |
| 这次查询**没有**取到**充电**过程监测样本 | 没有 + 充电 | 同上 |
| 我**无法**仅凭现有记录判断费用是否**异常** | 无 + 异常 | 否定「能判断」 |
| 本次**没有**取到**充电**过程明细 | 没有 + 充电 | 同上 |

而管家提示词明确要求「监测0条必须明确缺证，不能推断无故障」。
**提示词要求写的内容，被判据判成编造状态** —— 这是两个规格互相矛盾，
用户看到的就是概率性失败（模型措辞每次不同，撞上约四成）。

## 四、修复

判据从「否定词与状态词同子句」改为「否定词后面（允许状语）**紧跟**状态词」。

```python
# 否定后允许的状语，不改变判断
_NEGATION_ADVERBS = r"(?:仍|还|再|也|都|就|已|曾经|完全|根本|一直|始终|暂时|尚|并)*"
# 否定词 + 状语 + 状态词（紧邻）才算改写状态
re.finditer(re.escape(negation) + _NEGATION_ADVERBS + r"(?=.)", stripped)
```

## 五、验证

**真实模型样本（同一 manifest 重放）**

| | 修复前 | 修复后 |
|---|---|---|
| 10 份真实样本被拒 | 4 | **0** |

**真状态改写必须仍然拒绝**（9 例全拒）：`订单还没结束`、`该订单目前未结束`、
`这个订单一直没充电完成`、`订单尚未完成`、`这笔订单尚未完结，仍在供电`、
`The order has not finished and is still charging` 等。

**必须放行的缺证说明**（重放的 4 句真实样本全部通过）。

**回归**：全量 `3987 passed / 9 skipped`（修前基线 3977，本轮新增 10 条）。
镜像内容锁按 `local-gateway-build.md` 真实重建，`context_digest.py --check` 一致，
镜像内 `create_gateway_app` 可导入。

**一处保留的误拒**：`费用核对尚未完成，缺少充电样本。` 中「尚未完成」的主语是
**我们的核对**，判据仍按订单状态拒绝。该形态未在真实样本中出现，本次不扩大改动；
写进 ADR 的代价一节，不隐瞒。

## 六、顺带补的取证缺口

`json_parse json_syntax` 此前只记形态，无法区分「上游截断」与「模型拼错」。
现在多记 `len=` 与 `closed=`（是否以 `}` 收尾），仍不记原文。

## 七、边界

- **未部署**。本次只有代码、测试与文档；生产仍是 `1.0.17+07717e66d32c`。
- 重放证明的是「同一 manifest 下模型措辞分布」，不是人工业务金标准。
- 判据仍然是维护中的词汇表，不声称覆盖所有自然语言。

## 八、可复现

```bash
# 1. 取失败 run 的真实 manifest（只读）
dev-host exec 41 --allow-service-exec --timeout 60 -- /opt/aiops-41/.venv/bin/python - <<'PY'
import sqlite3
c=sqlite3.connect('file:/var/lib/aiops-41/gateway/gateway.db?mode=ro',uri=True);c.row_factory=sqlite3.Row
r=c.execute("SELECT execution_manifest FROM agent_runs WHERE qa_id=?", ("qa_af1d850e9207426e9dcd914fef6809fe",)).fetchone()
print(r['execution_manifest'])
PY

# 2. 在节点同款 model-proxy 容器里重放（凭据只在容器内读，不落盘）
#    见本回执引用的重放脚本结构：两轮工具循环 + response_format=json_object

# 3. 把重放结果喂给生产校验函数，看被哪条规则拒
uv run python -c "
import json,sys; sys.path.insert(0,'src')
from aiops_diagnostics.operator import operator_cpms as oc
ev={'cpms_order_detail':{},'cpms_charging_samples':{}}
oc.validate_answer(json.loads(open('/tmp/sample.json').read()), ev)
"
```
