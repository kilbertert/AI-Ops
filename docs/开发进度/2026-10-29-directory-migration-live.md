# 目录迁移前三片收口

目标是在退役真实验收后按职责归位，避免同时改变业务行为。回执分支`docs/directory-migration-live`，基线`921174b1`。

已合并：Java节点→services/execution-node；模型代理→services/model-proxy；C端12模块及语言JSON→consumer包，23测试→tests/consumer。
三片PR均通过必需CI与Devin审查闭环，分别为#1128、#1129、#1130；迁移任务分支与工作树已清理。
C端CD成功，原H5正常登录/选单/检测/完成答案可见6项通过，源码摘要与新版本一致，独立SQL/CPMS对照通过。
Java包等字节无需重启；模型代理仅完成仓内制品归位，生产旧发布包未切换，不能把仓内目录等同运行容器路径。

下一步为知识、身份等Python职责分包与对应测试整理；已知节点装配循环留待独立拆解。
本次原H5回归不代表历史故障根因与长期稳定性，相关业务准确率仍未完成业务验收。

最新验收：[前三片交付回执](../validation/2026-10-29-z03-directory-migration-live.md)。
