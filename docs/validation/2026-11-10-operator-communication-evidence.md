# 订单通讯与充电事件候选元数据验证

工单 #1454，源规格 #1452；分支 `feat/operator-communication-evidence`，基线
`3da9be7f82b6626ddee7c915e4c56d6026213986`，2026-11-10，本地 Python 3.13.13。
验收合同与 COMM-01/COMM-02 计划来自父任务的 `docs/specs/order-feedback.md`；
[本片合同和接续](../agents/operator-cpms/communications.md)记录已核事实与剩余条件。

新增最小检查保护新授权/时间窗口/源分页/字段投影边界：判据来自已批准订单范围、
父会话源观察与 tsdata `f44ea003` 写入/查询实现，既有枪遥测用例不覆盖这些通讯源。
替身数据仅验证程序行为，不将合成记录作为真实报文样本。

## 确定性结果

- 全仓 `uv run ruff check .` 与 `uv run ruff format --check .` 通过（最终1666文件）。
- `uv run pytest tests/operations tests/test_repository_layout.py tests/test_readme_architecture.py
  tests/test_runtime_retirement.py tests/execution/test_node_capabilities.py`：601通过、1跳过，59.57秒。
  跳过公司 GitLab 身份锚点复核（未配置 `AIOPS_GL_CA`）；未执行公司 API 脚本。
- 新模块22项检查最终通过，分支覆盖94%（98语句，32分支）；未覆盖缺设备、未配置源分支及
  某个非tsdata授权异常透传路径。证据 `dist/verification/communications-coverage.json`。
- 五个内存受控变异全部被拒绝：移除设备校验、移除24小时上限、用行数伪装源总数、
  把源失败当空窗、移除读后授权。结果在 `dist/verification/communications-mutation-*.txt`；
  脚本 `.scratch/check_communications_mutations.py` 不改候选源码。
- `uv build --wheel --sdist`、模块编译通过；wheel 内含新模块、工具声明和 SOP。
- `verify-aiops-gateway` 实际启动本地回环 Gateway，health/匿名401/OpenAPI/退役404均通过；
  证据 `dist/verification/communications-gateway/aiops-gateway.json`。脚本关闭自己的进程和临时根。

## 集成补丁检查与失败修复

遵守分工，未编辑 `operator_cpms.py`。`.scratch/operator_cpms-communications.patch` 的
`git apply --check` 通过；将其候选内容仅加载到内存，用真实 FastAPI/Gateway/存储、合成身份、
本地 HTTP 源与合成节点驱动，61项原工具回归与4项通讯入口检查合计65通过（59.05秒）。
入口覆盖空窗、HTTP500总数未知、额外device参数/详情未交付拒绝、缓存回读订单迁站拒绝。
复现：`uv run python .scratch/run_integrated_checks.py`。这不是父会话已整合版本，也不是实际Java。

首次范围测试误用了UTC的Z文字形式，改回既有`+00:00`合同；首次坏字段测试把应丢弃的未知字段
误判为所有源均失败，按字段投影边界订正；新增工具导致既有清单断言失配，已同步。
临时入口脚本回调签名错误已修复。读后授权变异首次漏检，改为最后一次源响应后撤权再验证，
该变异被拒绝；生产代码没有为通过测试放宽条件。另修复一行ruff超长提示。

## 缺口与独立验收

未完成业务验收。未访问生产、未调用真实模型、未运行原Dify/H5，独立验收由父会话另派。
通讯表不存在与三事件源空窗是父会话提供的观察；本分支没有独立复核。
方向/code/decoded、真实交易关联及MySQL历史源仍未证实，代码不查询或透传这些正文。
每条仅为设备窗口候选，最多24小时/每源16行；入库时间不等于设备动作时间。
Java/mvn不在本机PATH；未另行挂载本任务禁读的Maven设置，因此没有新Java构建/运行结果。
未跑全仓pytest、CI、镜像构建或部署。父会话仍需应用集成补丁、核对实际列/时间格式、
配套节点声明/执行模板/应用发布并独立验证入口。本分支仅获准本地提交，未push/PR/merge/deploy。

本轮运维只运行隔离Gateway验证，方法已在仓内技能；没有SSH、公司API或服务重启步骤需要补录。
`dev-worktree audit` 通过（32工作树）；他人工作未动，当前工作树保留供父会话整合。

最终文档与布局复核37通过、1主动排除的公司GitLab联网用例；没有为满足该用例访问公司API。
