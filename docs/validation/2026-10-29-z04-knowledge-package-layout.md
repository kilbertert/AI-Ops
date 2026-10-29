# 知识Python包迁移验证

基线`3aec6b2d`，分支`refactor/knowledge-package-layout`。6个模块归入`knowledge/`：
检索、知识源、三元绑定、同步守卫、租户开户、RAGFlow控制台；9个知识相关测试归入`tests/knowledge/`。
保持文件名与业务定义，Dify协议适配、C端编排及静态页面入口不变，不保留旧模块壳。

- 六模块移除导入后业务AST一致；调用者仅更新导入和monkeypatch路径。
- 知识/C端/Dify专项545 passed；本地全量3319 passed / 9 skipped。
- 安装后的wheel与禁网只读镜像能导入新包并读取原知识页面资源，旧模块不存在。
- 真临时Gateway启动/鉴权/退役路由通过；镜像160文件、内容锁一致；ruff/policy通过，spex 0错误/45项既有覆盖或漂移警告。
- 远端CI及部署后知识问答待验证。
本片合并前未部署，**未完成业务验收**；生产原H5知识问答需CD后单独验证。
此前C端包已部署并通过原H5订单检测，见[前三片回执](2026-10-29-z03-directory-migration-live.md)。
未新增业务分支、鉴权规则或数据格式，不追加复杂度/变异分析，沿用现有安全行为测试。
