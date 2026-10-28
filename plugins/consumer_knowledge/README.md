# WF01知识库选择节点

知识库参数使用Dify动态多选，本工作区目录凭据仅能列库；正文读取需要本轮data_grant。
当前WF01直接从所选库的已索引文档回答，引用和媒体来自本轮资料；不要求审核FAQ副本。
模型不选择库、不接收凭据。旧发布版审核行为仅兼容，见[当前WF01手册](../../docs/agents/consumer-wf01/README.md)。
构建使用官方dify-plugin CLI，SDK固定0.10.2；独立运行环境不增加Gateway依赖。
