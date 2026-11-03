# JSON 对象后面的装饰字符：定位、宽容与验证

日期：2026-11-03。分支：`fix/operator-json-trailing-junk`；基线 `c9d510f1`；ADR-0198。

## 一、来源

#1326 部署（`1.0.17+c9d510f13f78`）后，用同一张单经真实 Dify 试用入口连跑 20 次：
**16 成功 / 4 失败**（修复前为 26% 成功率）。

日志：

```text
7  operator_resample attempt=1 reason=no_answer_accepted   ← 重派真的触发了
8  json_parse json_syntax
4  operator_run_failed reason=OPERATOR_MODEL_OUTPUT_UNSTABLE
```

## 二、为什么设 max_tokens 不管用

用真实 operator 提示词对比三种配置（每组 12 次）：

| 配置 | 合规 | 坏 | 空 |
|---|---|---|---|
| 不设 `max_tokens` | 6 | 3 | 3 |
| `max_tokens=8192` | 5 | 4 | 3 |
| `max_tokens=16000` | 5 | 4 | 3 |

**设不设上限没有差别**，所以不是"被长度截断"。

## 三、真正的形状

把不合格的原文打出来（脱敏只看首尾）：

```text
head: {"direct_answer":"本次授权订单已读取CPM…
tail: …你先核对哪一项？"}"     ← 多一个引号
```

**JSON 是完整的，末尾多包了一层引号。** 另一个样本是 `…"}` 后再多一个 `}`。
`_object_end` 能正确切出第一个完整对象；此前 `parse_answer` 只做 `json.loads` 与围栏剥离，
于是差一个字符就整轮失败。

## 四、改动

`parse_answer` 加第三道：第一个完整对象之后的残余**只允许装饰字符**
（空白、`"`、`}`、`{`、`,`）时，按该前缀重解析。

**拒绝面没有放宽**：两个对象叠在一起、散文、截断的 JSON 仍然拒绝。

## 五、验证

- 新增两组参数化测试：5 种装饰全部放行；3 种非"一个完整对象"全部拒绝。
- **变异验证**：删掉宽容分支后 5 条装饰用例变红。
- 全量 **4004 passed / 9 skipped**。
- 镜像内容锁真实重建，`context_digest.py --check` 一致；
  `verify-aiops-gateway` 一次性数据根八项全过。

## 六、边界

- **未部署**。生产仍是 `1.0.17+c9d510f13f78`。
- 残余白名单是按这次见到的形状定的。出现新装饰仍会失败——**这是刻意的**：
  预先放宽等于把"什么算一个对象"变成猜测。
- 20 次复测里另有一次是模型在末轮**又要工具**（`finish_reason=tool_calls`），
  不在本次改动范围。
