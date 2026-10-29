# 目录迁移第二片：模型代理归位

目标：把常驻模型出口从工具目录移入services，并让部署与测试使用同一路径。
分支`refactor/model-proxy-layout`，基线`1c1b7316`。Java节点第一片已完成。

完成源码整体移动、两套Compose入口、镜像构建上下文、测试导入和当前文档同步；原文件未改业务逻辑。
专项32项、新镜像真实TLS烟测6项、镜像内容锁和wheel/sdist通过，源码摘要前后相同；全量3318 passed / 9 skipped，ruff/policy/spex通过；远端CI待验证。新Compose/镜像必须成对交付；旧生产包保持原样，未完成业务验收。
下一步验证新制品并交付本片，然后按consumer/knowledge/identity职责整理Python包及测试。
