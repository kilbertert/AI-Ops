# Dify集成Python包迁移

基线`d040f01d`，分支`refactor/dify-package-layout`。25模块+模板归dify包，29测试归tests/dify。
独立桥打包与ExecStart配套迁移；安装失败恢复代码、静态资源、单元。登录代理同版本切换方法已更新。
Dify专项764 passed，桥/模板/架构32 passed。25模块业务AST一致（仅资源包名变化），wheel/镜像新包与模板读取成功、旧模块不存在，临时Gateway通过。
ruff/policy通过，spex 0错误/49既有警告；全量3320 passed / 9 skipped；从提交打出的真实桥制品隔离导入通过。远端CI和CD待验证。
本片未完成业务验收；Gateway上线与独立服务上线分开判断。管家此前间歇失败仍未关闭根因。
