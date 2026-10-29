# 管家输出契约本地验收

日期：2026-10-29（北京时间）。分支`fix/operator-output-contract`；
基线`b86be95f1831e24a5611ba300aeac7aa29b0b625`。本记录为提交前的本地验收，远端CI/PR/部署结果另记。
验收契约见[output-contract.feature](../agents/operator-cpms/output-contract.feature)，
执行步骤见[QA计划](../agents/operator-cpms/output-contract-qa.md)。

## 已复现缺陷与修复

真实Java节点、Spring AI适配器、Gateway和临时存储；模型、CPMS、身份和Dify草稿为合成。
未访问41、未修改业务数据、未调用真实模型。报告与日志位于本checkout的`dist/verification/`，
目录gitignored，仅存本地，不作为已上传CI制品。

1. `operator-output-red`：模型未收到`response_format=json_object`时返回散文。
   `verify_operator_cpms.py`退出1，Gateway记录`json_parse/OPERATOR_INVALID_ANSWER`，
   问答终态`failed/OPERATOR_DIAGNOSIS_FAILED`。清单已有五字段提示词，故排除提示词漏接。
2. 只接JSON模式后通过；`operator-output-progress-red`再加入工具轮进度文本即同样失败，
   证实框架`fullText()`混入前面模型轮。节点改为每轮清空、逐块收集末轮正文后通过。
3. 验证发现`BaseAdvisor.after`在实际流式路径按块调用，不能只保存最后一个chunk。
   已用分块内容与独立finish帧钉住；空输出/超长/缺失STOP均拒绝。

## 最终证据

| 要求/场景 | 结果 | 证据 |
| --- | --- | --- |
| OC-01/02/03 正式入口、原生未保存草稿、普通已保存草稿；工具进度＋分块JSON | 通过，试运行未改正式指针 | `operator-output-final/report.json`，12场景，35次合成模型请求 |
| OC-04 供应商错误、截断、散文、错字段、假证据、虚假退款声明、空/超长正文 | 8种均失败且无结果 | 同上；假数据哨兵未出现在节点日志 |
| OC-05 call/stream每轮覆盖TEXT为JSON；C端协议；模型/工具/提示词防篡改 | 通过，205＋108条断言，22条反向控制 | `operator-output-guard/guarded.log`与`counter-proof.log`，均`failures=0/STATUS=PASS` |
| 管家授权、原生预览、发布、完成通知、部署清单专项Python测试 | 154 passed | 本次会话执行记录，47.96秒 |
| Gateway真实启动、健康、匿名鉴权、OpenAPI及退役路由 | 8项通过 | `operator-output-gateway/aiops-gateway.json` |
| Ruff静态/格式、差异空白 | 通过 | `uv run ruff check .`、`uv run ruff format --check .`、`git diff --check` |
| JDK21/现有Maven离线构建 | BUILD SUCCESS | `operator-output-build.log`；Maven无JUnit测试，不计作测试 |

整链最终完成于15:47:04（报告保存UTC），源码SHA-256逐文件与最终Java源码一致。
JDK镜像`eclipse-temurin@sha256:5d90b26c0784ccf55af55f97cbcf7f3969c66a06299c93bdc72649219942a48f`。
113个依赖jar按文件名排序后的名称/内容摘要组合SHA-256为
`388c8549cefcc08c6f9e1fee2f38116e4cdf4ddac7b598978ab6b1a854155d21`。
构建产物`services/execution-node/target/aiops-execution-node.jar`的SHA-256为
`e38da6f0556a83796a81e2c9810eac0444aed22bd74fa2dcad59b5484e95e260`。

风险检查：改动新增少量输出分支，无新增依赖/安全权限。当前Java验收为main/assert工具，
无已配置的JaCoCo、PIT或CRAP工具；未声称覆盖率或完整变异测试。两项根因都已先红后绿，
另外用TEXT漂移、C端对照和8类恶意/异常输出验证边界。编译的getDefaultOptions弃用提示
来自原有代码。未重跑全量Python套件，因生产改动仅在Java且相关154项及Java整链已覆盖。

## 交付与未完成项

本地修复可复核；尚无合并、远端CI或线上恢复证据。不能从旧会话中Dify可读文本的截图推断
具体某次运行原文是散文或JSON；本轮证明的是可复现的协议缺陷，不宣称读取了那次原始响应。
必须按合并制品流程更新Java节点，复测真实ulink账号的原生预览、AI-Ops试运行和正式入口。
供应商若拒绝或忽略JSON参数会继续失败，不静默降级。未完成业务验收。

操作知识自检：本次仅运行开发机临时容器、编译与测试，步骤已在QA计划和现有节点验收手册。
无生产SSH、服务重启或配置修改。新worktree创建因其他任务已集成未退役而被拒，使用干净
canonical checkout内的独立任务分支；未删除/修改其他任务worktree。收尾audit已通过（19个worktree）。
期间`origin/main`推进至`db6bda192d7f213538b160a803feb576d7ea1480`，仅为自助开通部署文档，
含同一状态索引的新条目；本地未提交候选保留原基线，后续PR前须整合该文档变化。

交付准备：用户已授权PR、合并后部署节点和ulink验收。提交钩子要求独立worktree，已迁移至
`fix/operator-output-contract`（base `db6bda192d7f213538b160a803feb576d7ea1480`），
完整保留两份状态索引记录；Java源码与已验收候选相同。

PR #1149首轮CI为3343 passed / 10 skipped / 1 failed：验收驱动属于部署上下文，
源码摘要锁尚旧。已通过`build-image.sh --update-lock`真实重建并更新锁；部署专项18 passed。
Devin与Codex自动审查因额度不足未完成，无待处理线程，不标作审查通过。
