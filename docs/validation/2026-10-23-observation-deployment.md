# 2026-10-23 · 观察修复部署复核

**未完成新的业务验收。** 本记录区分本地契约验证、已完成的 CD、公开发布与功能启用。
不包含新的真实用户查询或模型调用。

## 代码与本地验证

- #1031 合并提交：`b35a3dfed82e2dcad8f5c5a91b4bb4a7b35c9ce5`；
  #1032 合并提交：`d535c9e0df1802bd3ead5fc0205c7524ba449caf`。
- 两个 PR 的 `Workflow policy`、`spec-tree lint`、`verify` 均通过；
  Devin Review 显示 pass，但完整模型审查因额度不足被跳过，不算完成审查。
- 独立复验 `tests/test_consumer_order_chat.py`、`tests/test_agent_metrics.py`、
  `tests/test_request_observation.py`：79 passed。
- 本地启动真实 Gateway 进程，经 TCP 驱动：23/23 检查通过；
  11 条结果行为 5 completed、4 refused、2 failed。
  登录校验、MySQL 和 CPMS 使用回环替身，不能据此认定真实业务通过。
- #1031 回执另报全量 3687 passed / 9 skipped，以及 401 分类反向变异检出两条失败；
  本次部署复核没有重复执行全量或变异测试。

## 部署与发布

| 运行 | 制品 | Deploy | Publish release notes |
|---|---|---|---|
| [39949320610](https://github.com/kilbertert/AI-Ops/actions/runs/39949320610) | `d535c9e0df18` | 成功 | 失败，后来人工补发 |
| [39952235209](https://github.com/kilbertert/AI-Ops/actions/runs/39952235209) | `429236d361fa`（#1033） | 成功 | 成功 |

首轮发布失败的根因是 runner 缺少本地 `serverPolicy.publishesVersions` 声明：
生成器跳过、返回 0，stdout 又被隐藏，后续复制才报缺文件。不能据此推断“已写到别处”。
#1033 的[复现记录](2026-10-23-release-step-silent-skip.md)说明了修复与验证。
没有为补备注回滚或手工重新部署已成功的代码，也没有执行生产 `uv sync`。

用户批准固定脱敏文案后，已创建 [prerelease release-d535c9e0df18](https://github.com/kilbertert/AI-Ops/releases/tag/release-d535c9e0df18)，
目标为 `d535c9e0df1802bd3ead5fc0205c7524ba449caf`；GitHub 回读核对目标、标题、预发布标记和正文。
原始历史生成清单未由本次手工操作发布。

## 最新只读回查：2026-10-23 12:20 UTC

健康端点版本为 `0.1.0+429236d361fa`；读取当前进程环境确认个人查询三项配置均未设置。
`request_observation.py`、`gateway_api.py` 与该 Git 提交逐字摘要一致。
`__init__.py` 在部署时会注入版本号，必须先按 CD 规则构造该提交的注入结果，再比对生产摘要；
本次按此规则核对一致，不能用 Git 原文件的摘要代替部署后摘要。

以上证明制品身份与开关状态，不证明生产流量已经产生观察结果；本次没有为取样提交业务问题。
后续开窗须重新读取版本和授权条件，不能沿用这次读数。

## 文档交付检查

本片仅修改状态索引并新增两份记录；不改变 API 或业务行为，因此不新增契约、fixture 或业务测试。
操作知识自检命中 README 和部署手册中的 CD 与手工补发方法；未改写旧归档记录。
本地 `git diff --check`、Ruff 检查与格式校验通过；`tests/test_readme_architecture.py`
5 passed；新增 5 个相对链接均指向现有文件。新增文本未包含内部绝对路径、账号或会话样本。
PR 的确定性检查以托管结果为准。
