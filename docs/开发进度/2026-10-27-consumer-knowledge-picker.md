# WF01节点选库实施

日期：2026-10-27；分支：feat/consumer-knowledge-picker；初始基线4066ab42。

目标：在“检索当前用户适用知识”节点按名称选择本租户知识库，草稿/试用/发布/恢复贯通，保留已审核FAQ规则。
已完成Gateway发布冻结与范围校验、只读目录凭据、签名插件、Dify设置面板两属性补丁、离线迁移工具和隔离验收脚本。
旧callback_version=1/2继续兼容；新版为3。公司App接口、CPMS路线和模型配置不变。

原API隔离整链88/88通过，覆盖选A、草稿B不切正式、试用B、发布B、恢复A；浏览器配置验收在本轮继续。
全量首轮4113 passed/9 skipped，1项因镜像内容锁未更新而失败；已标准构建更新，相关集合60 passed。
新增模块分支/语句100%，五组关键守卫变异均被测试检出；恢复后43项专项通过。
Gateway临时数据根技能验证、ruff、格式与policy检查通过。Dify候选web镜像完整构建通过。

已知缺口：尚未生产部署、安装插件、迁移现役图或原App复验；CI/PR状态待交付时核验。
下一步先完成真实编辑器选库后的执行验证，再提交同一PR，按已合并制品安装并核验。
详见[验收记录](../validation/2026-10-27-consumer-knowledge-picker.md)与[部署手顺](../agents/consumer-wf01/knowledge-picker-deploy.md)。
