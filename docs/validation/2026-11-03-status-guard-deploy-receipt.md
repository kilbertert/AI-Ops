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


## 五、这批 12 次的标识与复现

**批次口径**：同一问题、同一入口（Dify 应用 `137cc417…` 原生预览）、
同一版本 `1.0.17+24184f75b224`，UTC `10:43:32 – 10:56:05` 连续 12 次，无重试替换。
按 `created_at` 落在该窗口且 `order_no` 命中即可圈定，不需要额外批次号：

| # | qa_id | 状态 |
|---|---|---|
| 1 | `qa_685a98a29edb4d4a94da6f7df40721f0` | succeeded |
| 2 | `qa_7aa99441aaab4976802cbd9e5c4f5065` | succeeded |
| 3 | `qa_a589a68011b44e098dc6463f9c28ac38` | **failed** |
| 4 | `qa_a498a56a730e4df191ef9e9912ce9fcd` | **failed** |
| 5 | `qa_4e87e07d6f4040cb8df7853a04a23650` | succeeded |
| 6 | `qa_2b8aa94a6bc84552bb8739274c3cc8e4` | succeeded |
| 7 | `qa_f378f82fe3e94928ab5ccf89060ec6d0` | **failed** |
| 8 | `qa_cbc7bfa4fac14d36a9774b7d03248bb9` | **failed** |
| 9 | `qa_522ecf79038f442bb7e44b0402af5ce9` | succeeded |
| 10 | `qa_3cd3ca371dc9401eb4bd898b9687d16f` | succeeded |
| 11 | `qa_91525785cb6141acb77c4aee5fe2292e` | succeeded |
| 12 | `qa_7c5acf49e4ec4f78a2348b6e0af1ab73` | succeeded |

**把失败的 4 次与网关日志对上**：`agent_runs` 只有结论，成因在日志里，两边靠**时间戳**对齐
（日志是北京时间 = UTC+8）：

```bash
dev-host exec 41 --allow-service-exec --timeout 40 -- \
  "journalctl -u aiops-gateway-41 --since '2026-11-03 18:38' --until '2026-11-03 19:06' --no-pager \
   | grep -oE 'reason=[a-z_]+( detail=[a-z_]+)?|plain_text:[0-9]+' | sort | uniq -c | sort -rn"
```

本批次实际输出：`plain_text:1504 / 1313 / 1248` 各一次（对应上表 3/4/7/8 里的三次 `json_parse`），
外加一次 `unsupported_fact detail=notice line=4 len=62`（另一类，未在本轮处理）。
**`detail=status` 计数为 0** —— 这是本轮要证明的那一点。

```bash
dev-host exec 41 --allow-service-exec --timeout 60 -- /opt/aiops-41/.venv/bin/python - <<'PY'
import sqlite3
c=sqlite3.connect('file:/var/lib/aiops-41/gateway/gateway.db?mode=ro',uri=True);c.row_factory=sqlite3.Row
for r in c.execute("""SELECT qa_id,state,gateway_version,created_at FROM agent_runs
 WHERE order_no='2116050516255272961'
   AND created_at BETWEEN '2026-11-03T10:43:00' AND '2026-11-03T10:57:00'
 ORDER BY created_at"""):
    print(dict(r))
PY
```
