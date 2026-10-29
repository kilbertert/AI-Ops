# 目录迁移第一片：现役Java节点归位

退役清理、部署和真实双端/Dify链路验收完成后，开始本片。分支`refactor/execution-node-layout`，基线`19f09c82`。
将16个Java节点文件整体移到`services/execution-node/`，同步实验驱动、清单身份、打包路径、gitignore与现役手册。
历史验收/ADR保留当时路径，不修改过去证据；原路径不留复制或空壳。
Java守卫、反向控制、CPMS整链、Maven打包及Gateway镜像构建已通过，全量3317 passed / 9 skipped、跨语言清单与wheel/sdist通过；前后jar摘要一致。
下一步交付本片；后续按consumer、knowledge、identity等职责拆Python包及测试，不与行为修改混做。

本片未完成业务验收：只验证目录迁移无语义变化，未重复生产故障验收。旧工作树构建残留仍忽略并从镜像排除。
