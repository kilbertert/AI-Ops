# 双端开通规划的证据核查

日期2026-10-30；源码基线`02d11c3f944a0285934506645e3e019b5883f77e`。

## 核查结果

- 管家限定来自catalogue筛选及开通/发布生成逻辑，不是Dify无法复制客户端。
- 客户端已有按环境/租户/场景选路和应用key申请，但发布目标、数据端点、key就绪和试用仍有旧登记依赖。
- 固定H5业务API不带Dify应用ID；现有渠道换智能体不等于每次重建前端。新客户专属域名/包仍须验证接入。
- 现有知识检查主要证明租户归属，双端共用空间还须补对客/内部的应用可检索范围。
- 核读Dify官方API/导入说明及1.17.1固定源码；区分新版文档与现役API。

## 自动化与边界

本轮命令：

```bash
uv run pytest -n 0 tests/execution/test_chat_route_bindings.py \
  tests/consumer/test_consumer_publication.py tests/dify/test_dify_app_registry.py
```

结果：53 passed，1个既有依赖弃用警告，7.75秒；原始日志保存在本机私有cache下
`aiops-dify-self-service-research/dual-entry-existing-contracts.log`。
未新增运行代码或测试，未做变异/覆盖率评估；这次是规划与现有契约核查。

**未完成业务验收**：未真实新开客户端客户、未迁移key、未验证所有客户渠道、未改公司权限。
新能力验收用例已列入[规划](../agents/dual-entry-onboarding-plan.md)，不得把上述53项计成双端新功能已通过。
本轮没有服务宿主运维写操作，操作方法没有新增库外步骤。

## Claude派遣前复核

用户已授权按规划派遣开发。交接基线更新为`4af0989362c26ea92920bda2191380950f0359af`，
同一命令重新运行：53 passed、1警告、7.73秒，日志为私有cache下`claude-dispatch-baseline.log`。
执行任务书限定4000字符以内，已核字符数、内部文档链接、policy和diff检查。
任务书给出的A-L是Claude必须补成可执行步骤的新验收合同，不是已存在的测试通过记录。
