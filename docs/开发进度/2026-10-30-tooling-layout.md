# 工具与公司集成按用途归位

分支`refactor/tooling-layout`，基线`041a1eb0`。
`tools/`根层按用途分成`content`、`verification`、`experiments/m2_close`、`inventory`；
公司 Java 补丁移入`integrations/cpms-java/`；34个测试分入`tests/tooling`与`tests/integrations`。
被移动文件内容不变，只改引用；包内兄弟导入按新目录解析，`tools/`根层不再散落工具脚本。
全量3430 passed / 9既有跳过，格式、repo-map、ADR与冲突面检查通过；镜像按新树重建并重写内容锁。
待完成CI、CD与生产回归；未完成业务验收。本片之后仓库目录迁移的既定切片全部落地。
