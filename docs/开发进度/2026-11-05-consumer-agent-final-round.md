# 客服只交付Agent最终回答

安卓真实单轮查询正确，但官方Agent累计text包含英文过程说明，混入中文最终答复。
已实读官方0.0.51源码及真实Service API SSE，agent_log携带根轮次llm_response与tool_responses。
复用该结果仅交付最后成功无工具轮次，正式和预览共用；不改变模型或业务工具。
候选1.2.5，分支fix/consumer-agent-final-answer。真实上线后复验待完成。
