---
title: faq-catalog
desc: 固定问答目录——中文权威 + 五种翻译
hue: 60
related:
  - src/aiops_diagnostics/faq_catalog.json
  - src/aiops_diagnostics/faq_recommendations.json
  - src/aiops_diagnostics/faq.py
---
# faq-catalog

平台隔离的固定问答目录：中文为权威，`en/de/fr/es/pt` 五种翻译由它派生。

## raw source

目录是**数据制品**，不是代码分支。把它写进代码意味着每次改文案都要动逻辑；
把它当数据意味着它可以被工具校验（`tools/` 下有合并/派生/校验器）。

## 不变量

- 中文是权威来源，其余语言是派生——不得反向修改翻译来"对齐"中文。
- 目录里的语言集合必须与 `i18n` 的语言表保持一致；两者不一致会让某语言在目录里存在
  却在语言判定里不存在（或反之）。
- 推荐展示清单与答案目录分开：前者可以被替换，后者是内容本身。

本节点不 `code:` 拥有实现文件：目录是数据，`faq.py` 只是它的读取者，
读取逻辑归 [[l4-inference]]。
