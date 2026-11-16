# 手工环境目录规划：研究与文档验证

2026-11-16；分支 `docs/manual-environment-onboarding-plan`；基线 `a9427e70`。
**未完成业务验收**。本轮为规划，不改变运行代码、部署配置或生产数据。

## 证据范围

- 上游研究会话 `snail_onboarding_research` 读取固定 GitHub v1.1.1源码、官方文档与内置网页制品；
  Gitee API确认master修订，部分源码请求超时。范围及逐项来源见[研究文档](../agents/snail-manual-onboarding-research.md)。
- 主会话复核了 App token/编辑接口、两个方向的 gRPC 建连，以及本仓目录、管理API、页面、登录映射、
  开户账本和执行节点绑定，未运行上游服务或登录其演示站。
- 读官方 RFC8725、RFC9110 和 SQLite事务文档，核对身份、条件更新与持久化设计的适用范围。
- 当前上线状态引用11月14日回执，本轮没有新的生产复验，也没有独立业务 Hacker 验收。

## 本地检查

- `uv run pytest tests/test_repository_layout.py tests/test_readme_architecture.py -q` 全部通过，退出码0。
- 11个改动Markdown文件的191处本地链接均可解析；研究文档31个官方引用无缺失定义。
- `git diff --check` 通过；`dev-worktree audit` 通过，5个工作树已核，其他任务和主目录未改动。
- 独立读回Gitee API修订及LICENSE，确认研究记录修订准确、两份LICENSE逐字相同；README附加说明差异仍保留。
没有新增长期测试；既有检查只证明目录与链接等可机械核对的部分，不证明规划可运行。

## 留给实施阶段的验证

先证明两环境身份/网络与最小部署容量，再以原页面完成新增环境、租户开通、异常订单/离线分析/客服、
追问、跨环境/跨租户拒绝、重复执行和断线恢复。完整判据见[规划P0～P5](../architecture/2026-11-16-manual-environment-onboarding.md#10-执行顺序与完成判据)。
当前未选择新生产目标，未执行安装、凭据或路由切换；不把节点在线、fixture或模型回复记为真实故障准确性。
