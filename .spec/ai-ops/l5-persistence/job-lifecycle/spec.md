---
title: job-lifecycle
desc: 异步作业生命周期的唯一定义——四张表共用它
hue: 30
code:
  - src/aiops_diagnostics/async_job_lifecycle.py#claim_guard
---
# job-lifecycle

四张表（`health_report_jobs` / `standard_diagnoses` / `assistant_questions` / `runs`）实现同一条
异步作业生命周期规则，此前给出的是**三个半**答案：五份手写的同一句过期语句、同一条 claim guard
的两种极性拼写、两套常量族（问题表借用诊断表那套），以及一个仓库自己已经记录为错误的启动清扫
（构造一个 store 竟然终结了在途工作）。

## raw source

本模块是那条规则的唯一定义。四张表按**命名 profile** 声明各自差异，而不是各写一份语句。
差异必须显式可见——藏在副本里的差异不是差异，是缺陷。

## 不变量

- 状态集、截止时间、保留期分层、claim guard、以及进程死亡的三种成因，只有一处定义。
- **构造一个 store 不得终结任何在途工作。** 启动清扫必须只清扫真正过期的，且判据来自本模块。
- 极性（"可领取"还是"不可领取"）只有一种拼写。
