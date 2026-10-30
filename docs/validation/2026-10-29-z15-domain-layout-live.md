# 领域目录上线回执

领域片（`operator/`、`health/`、`data/`）已合并 `dc9e3eac` 并部署到 41，
生产版本 `1.0.0+dc9e3eacc4c5`，服务 `active`、`business_mutations=disabled`。

## 部署核对

- `/opt/aiops-41/src/aiops_diagnostics/{operator,health,data}/` 三个包存在且完整。
- 12 个旧包根模块（`operator_cpms.py`、`ops_analysis.py`、`ops_analysis_narrative.py`、`ops_provider.py`、`health_*.py`、
  `rules.py`、`sources.py`、`charging_status.py`、`order_status.py`）在生产上已不存在。
- CD `40862498121` success。

## 业务回归

- 原 C 端 H5 快捷检测：**6 项全部通过**（正常登录、启动当前 QA、完成、语言、非空正文、
  正文在原页面可见），`posts=1`。
- 管家原 H5 订单诊断：**失败**，QA 终态 `OPERATOR_DIAGNOSIS_FAILED`。
  这一步不是本片造成的路径问题——失败原因经固定原因日志定位为 `json_parse`，
  根因是供应商偶发用 ```json 围栏包裹报告，与目录迁移无关。
  修复见[单层围栏解析](../validation/2026-10-29-z11-operator-fenced-answer.md)，
  PR #1161 合并 `c6c712d5`，部署后需重跑本项。

## 限制

本片只移动位置与引用，12 个模块的业务 AST 与迁移前一致。上线回执不包含管家端最终通过，
在该修复部署并重跑真实订单诊断之前，第十片的双端业务回归只能记为 C 端通过。
