# WF01的知识库选择随节点发布，正文仍使用单轮授权

日期：2026-10-27
状态：已采纳

用户确认保留已审核FAQ交付规则，要求在“检索当前用户适用知识”节点直接换库。
采用Dify工具插件的dynamic-select/multiple参数，选库作为常量写入DSL，试用/发布核验当前租户目录，
与图及审核内容一起冻结进原Agent不可变版本。callback_version=3使用节点选库；文件旧字段仅兼容v1/v2。
保存不切正式版，试用使用草稿，发布才生效，恢复取历史选库；普通发布不允许降回固定HTTP节点。

目录凭据用既有internal_token_secret按环境/工作区作独立HMAC派生，仅能获取该租户库的ID和名称。
正文仍需原data_grant，并核运行主体、能力、发布绑定和节点库列表一致；目录凭据不能取正文。
新检索、缓存返回、检索完成、最终答案和历史回读分别按现有运行守卫核验；目录不可用拒绝交付知识答案。
CPMS分支不依赖知识目录。凭据无单独有效期，撤销工作区映射或轮换主密钥使其失效；暂不新增凭据管理表。

依据为Dify官方[工具插件说明](https://docs.dify.ai/en/develop-plugin/dev-guides-and-walkthroughs/tool-plugin)
与现用1.17.1源码：dynamic-select支持multiple，工具参数常量支持数组。
该版工具“设置”区未传currentProvider/currentTool，不会加载动态选项；补齐这两个属性沿用官方多选控件。
不能移到form:llm输入区：现用Graphon执行器将constant数组转为字符串，与multiple校验冲突。
参数保留form:form及tool_configurations常量数组，输出用knowledge.text汇入既有聚合。
授权与图片线索使用固定mixed模板引用；直接variable引用会被该版编辑器保存为空，不能据API执行成功推断可编辑。

备选方案未选原生knowledge-retrieval：现版本外接知识接口只带静态provider key与query/knowledge_id等字段，
无法直接携带单轮data_grant。也不向网页注入选库按钮或把静态整租户密钥交给取数节点。
插件按项目签名安装，额外公钥与Dify官方公钥并存，FORCE_VERIFYING_SIGNATURE保持true。

后果：每轮知识答案增加目录读取；删除库后旧答案也暂不可读。新库需召回现有审核清单中的KB-C-NNN，
普通文档不会自动获得自由回答权限。Dify仍可能保存运行输入中的短期grant，终态撤销不能追回此前披露。
