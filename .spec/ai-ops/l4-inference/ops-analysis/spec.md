---
title: ops-analysis
desc: 运维分析面——数字由确定性计算得出，模型只写两段散文
hue: 60
code:
  - src/aiops_diagnostics/operator/ops_analysis.py
related:
  - src/aiops_diagnostics/operator/ops_analysis_narrative.py
---
# ops-analysis

与单订单诊断是**两个面**：诊断回答「这一条为什么坏」，本面回答「这一段范围内坏得多不多、
集中在哪」。输入因此是**站点集合 + 时间窗**，没有 `order_no`；输出是第三份对外契约。

## raw source

**数字不由模型生成。** 所有计数、比率与风险等级来自确定性计算，模型只写成因叙述与建议两段散文。
两道墙：结构化输出 schema 里**没有数字字段**；装配时**按名字只读两个散文键**。
因此即使 provider 无视 `additionalProperties`，一个模型写出的数字也没有路径进入数字字段。

第二道墙的位置是被生产事故决定的：零阶工作区的护栏写的是「回答问题、返回 `text`」，
而这一轮要的是 `{"main_cause", "recommendation"}`——护栏与轮次提示词各说各话，
模型照护栏回了 `{"text": …}`。`answer_contract` 参数把形状差异交给调用方，护栏其余部分逐字共用。

## 不变量

- 空范围 / 窗外无单 ⇒ `unavailable`，**不是 0**。零与"不知道"必须是不同结果。
- `RULE_VERSION` 与每张卡自己的 `definition` 一起落进契约：读的人据此知道数字按哪条规则算的。
- 三张卡（失败率 / 离线桩 / 故障码）共用入口门、散文路径、语言守卫与 `reason_code` 词汇。
- 本面是输出语言守卫的**第 4 个模块**（第 5 个面）——语言守卫漏掉一个新面，
  是这个仓库已经发生过一次的失败形态。
