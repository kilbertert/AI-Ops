# 214. 度量生产点引用词表常量，裸字面量由守卫拦截

日期：2026-11-04
状态：已采纳

#799 的判据（`ROUTE_TYPES` / `check_dimension`，ADR-0206）已经建立，但生产点仍各自写
`route_type="operations_analysis"` 这样的裸字面量。「新增一个路由类型要改哪几行」没有答案，
而三次静默丢指标的事故（`operations_analysis`、`cancelled`、`trial`）都是「生产者与判据
是两份清单」的同一形状。

## 决策与依据

`metrics_store.py` 为每个成员提供 `ROUTE_*` / `OUTCOME_*` 常量，生产点引用它们；
`tests/gateway/test_metrics_dimensions.py` 加两条守卫：

- **不许新增裸字面量**：全量扫 `src/`，除一份白名单外出现 `route_type="…"` 即红。
- **白名单只许减少**：条目数不得增加，防止它变成第二份词表。

白名单保留的四条是独立欠账，不是本决策的同类：两处 `JobRegistration(route_type="trial")`
只是登记「这是哪种作业」，指标行由 `_record_trial_metric` 写；operator 预览路径的
`route_type="qa"` **压根不写指标行**（「没有写入点」，补它会改变 `qa` 口径，需单独决策）。

## 备选方案

- **只加守卫、不加常量。** 被否：守卫会立刻把现有四处判红，而它们是对的。
- **把白名单写成「允许的名字集合」。** 被否：那是第二份词表，会与 `ROUTE_TYPES` 漂移。
- **顺带补 operator 预览的指标写入。** 被否：它改变 `qa` 的统计口径，是产品决策，
  不是本决策能夹带的一行。
