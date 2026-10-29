# 站点搜索说明字段误拒绝修复

基线`cd1f8992`，分支`fix/operator-search-prose`。本次继续产品“没有订单不能询问”的收尾。
已复核Gateway为`dc9e3eac`、Java为`2da807b4`、桥为`3ac6115c`，未重复部署。
原生预览广州天河查询completed；实际站名查询在有效登录后以OPERATOR_INVALID_SEARCH失败。
同清单、同供应商流式探针复现正确name加非空text，非流式对照text为空。

修复只丢弃site_search的说明，不影响真正查询条件或站点来源。两条真Gateway/合成CPMS回归先红；
专项结果：65 passed, 4 warnings in 38.82s；Ruff/格式、内容锁重建通过。真实请求与原始输出仅存私有证据目录，不上传PR。
未完成业务验收；部署后须重新原生预览同站名，并核对同run的站点与独立CPMS。
