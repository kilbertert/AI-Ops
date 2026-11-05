# 原控制台客服Agent预览身份验证

现场：1.2.0+714d4ffdb95a，原Dify预览问题“How can I stop charging safely?”。
工具回调因公司all范围被consumer self门拒绝，run没有取数记录但模型仍给了建议，业务验收失败。
正式路由仍Ulink v18/revision21及优链充v1/revision1，未发布候选。

修复判据：原运营预览可检索同租户对客知识；只能读取明确选定且已授权的订单，
orders及其他对象拒绝；App正式问答继续只允许self。测试复用真实HTTP/试用/回调，
模拟公司all范围，检索与选中单成功、列单和换单拒绝、撤权回读拒绝。
运行结果见PR与本轮私有日志；真实新候选仍待部署复验，未完成业务验收。
