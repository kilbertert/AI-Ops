# 提示词生命周期接手验证

基线c01ab48b，开发worktree，2026-10-30。尚未提交部署，未完成业务验收。

## 本地

- test_prompt_binding_lifecycle：原5个失败（空表误拒/4种授权变化漏判），修复后8项通过。
- test_prompt_publication_lifecycle：原3个失败，修复后3项通过；覆盖取数前、完成前和完成后发布与真实撤回。
- 管家相关+运行存储共137项通过；客户端/管家领域测试通过（既有条件跳过保留）。
- test_prompt_fact_segments：多语言与正常片段原5个失败，修复后加回读篡改共9项通过。
- verify_prompt_lifecycle.py重写为运行真实仓库用例，26项通过，不将页面与模型NOT_RUN计入通过。
- 全量只有test_gateway_context_digest_is_reproducible_and_matches_the_lock失败：源码改动后锁待重建。

## 隔离真实Dify

执行tools/verification/verify_consumer_chatflow.py，真实Dify/Gateway、合成模型/身份/业务数据。
原App三个回合已恢复。先后两次失败为验收脚本要求旧内部来源名与旧账单措辞，实际完成答案与
快照正确；更新为现役中文来源和限制说明后73项全部通过，包含试用/发布/撤回/恢复。
私有证据：~/.cache/aiops-prompt-lifecycle-review-1232/codex-isolated-r3.json；原始根
/tmp/consumer-chatflow-d08tlzy8。前两次失败记录同目录codex-isolated*.json保留。

## 真实模型

使用已配置deepseek-v4-flash，合成业务数据，实际final_answer校验/渲染。
首轮实时快照被模型误判为知识引用，修正协议明确status优先；知识fixture补齐检索ID。
第二轮5场景×2风格10次通过。原始模型输出与usage位于~/.cache/aiops-prompt-live/{r2/,}，
失败不被覆盖。不是生产用户、页面或故障准确率验收。

## 知识自检

dev-host、41模型出口和Dify登录/验收方法已在env-41-runbook与consumer-wf01/integrated-acceptance。
新增方法、最终构建/CI/页面与上线证据在后续里程碑补录。

## 提交前补充

全量pytest最终复跑退出0（详细输出私有full-final2.log；最终数量以CI/JUnit为准）。
ruff check/format、wheel/sdist、实际镜像构建与内容锁通过；临时Gateway全部HTTP检查通过。
语言规则移入系统协议块，两条旧测试同步检查全部SYSTEM消息，语言要求未删减。
旧测试脚本外部输出路径relative_to异常不影响已完成HTTP断言，按skill的dist目录复跑全部通过。
