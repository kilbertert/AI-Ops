# 状态判据修复：上线与同一张单的复测

日期：2026-11-03。PR：#1323，squash 合并为 `24184f75`，CD 部署为 `1.0.17+24184f75b224`。
被验收的对象：用户报「帮我分析这笔单的电费 `2116050516255272961`」时好时坏。

## 一、修复前后对比（同一张单、同一入口、真实 Dify 试用）

修复前按库内全量统计，修复后按同一入口连跑 12 次：

| | 样本 | 成功 | 失败 | 成功率 |
|---|---|---|---|---|
| 修复前（本轮修复上线前的全部运行） | 72 | 19 | 53 | **26%** |
| 修复后（12 次真实试用，`1.0.17+24184f75b224`） | 12 | 8 | 4 | **67%** |
| 其中：修复前的**同一成因** | — | — | 13（`detail=status`） | **0 次** |

`detail=status` 这一族在修复后**一次都没再出现**。这不是"变少了"，是消失了。

## 二、剩下 4 次失败是什么（换了一个成因）

全部是 `reason=json_parse plain_text:1248 / 1313 / 1504`：
模型在最后一轮**根本没有输出 JSON**，而是写了一段 1200–1500 字的中文分析正文。

要点：

- 节点在每轮最终发送点**强制** `response_format={"type":"json_object"}`（`GuardedChatModel.outputContract`），
  所以这不是"我们没要求 JSON"，是这个供应商对 `json_object` 的遵守是**概率性**的。
- 这与 ADR-0196 修掉的那个成因**不同**：那个是判据误拒合规输出，这个是模型不吐合规输出。
  两者的用户可见症状相同（`OPERATOR_DIAGNOSIS_FAILED`），所以第一反应很容易把两者混为一谈。

**本轮没有修它**，只补了取证：`json_syntax` 现在多记 `len=`/`closed=`/`depth=`/`in_string=`，
下次能分开「截断」与「语法错」。`plain_text:` 已经带长度，成因待定。

## 三、失败样本原样保留

修复后 12 次里失败的 4 次全部留在验收数据里（没有重试覆盖首次结果）：

```text
qa_a589a680…  failed  gateway_version=1.0.17+24184f75b224
qa_a498a56a…  failed
qa_f378f82f…  failed
qa_cbc7bfa4…  failed
```

成功的 8 次同样是 `1.0.17+24184f75b224`，说明不是版本混跑。

## 四、边界

- **67% 不是终点**。剩下的 33% 是模型不吐 JSON，需要另一条修法（例如对纯文本做一次
  有界重采样，或在节点侧把"没有 JSON"当作可重试的上游失约）。本轮未做。
- 12 次样本量小，67% 的置信区间很宽；它证明的是"这一族误拒没了"，不是"稳定在 67%"。
- 验收走的是 Dify 试用入口（`operator-trial:`），正式管家入口只做了部署前基线核对，
  未在本轮重跑 12 次。
- 判据仍是维护中的词汇表；ADR-0196 记录了保留的一处误拒。

## 五、可复现

```bash
# 修复后同一张单连跑 N 次（真实试用入口，只读）
# 见本回执引用的 trial 批处理脚本结构：Playwright → /__aiops/operator-preview → 读 SSE error 事件

# 核对某次运行的结论
dev-host exec 41 --allow-service-exec --timeout 60 -- /opt/aiops-41/.venv/bin/python - <<'PY'
import sqlite3
c=sqlite3.connect('file:/var/lib/aiops-41/gateway/gateway.db?mode=ro',uri=True);c.row_factory=sqlite3.Row
for r in c.execute("""SELECT qa_id,state,gateway_version,created_at FROM agent_runs
 WHERE order_no='2116050516255272961' AND created_at>='2026-11-03T10:40' ORDER BY created_at"""):
    print(dict(r))
PY
```
