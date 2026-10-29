# 知识目录迁移上线回执

回执分支`docs/knowledge-layout-live`，基线`2662c3fa`。第四片PR #1132已合并并由CD部署。
6模块+初始化文件摘要与线上一致，旧根模块清除；原H5正常登录、知识问答、正文与引用显示9项通过，页面错误0。
本地3319 passed / 9 skipped，CI3318 passed / 10 skipped，Devin无意见；源码归包未改变业务AST。

四片目录迁移完成，下一步整理身份/范围模块及测试。模型代理生产旧包仍未切换；
历史故障根因和长期稳定性未完成业务验收。最新证据见[上线回执](../validation/2026-10-29-z04b-knowledge-layout-live.md)。
