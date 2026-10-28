# 动态知识文档的一次迁移与日常维护

## 产品日常操作

将资料上传到Chatflow知识节点已选择的知识库，等待解析/索引完成即可提问。
资料更新、删除由知识库管理界面操作，影响之后的新问题；不编辑content.json，不审批approved字段，不发App版本。
文件名不限KB编号。建议文档写清主题、适用场景和说明正文；视频链接直接放在相关正文里。
已有KB-C-059等带YouTube地址的文档无需另做媒体映射。地址来自文档，不让模型改写或编造。
只上传图片而没有任何文字/索引关联不保证能按文本检索到，图片宜与说明处于同一可检索资料中。

更换所用知识库或改Chatflow图/提示词时，仍需保存并发布工作流；这与库中文档更新是不同操作。
旧会话显示的历史答案保持当时快照，新问题重新检索；删除文档不会追溯抹去已展示给用户的历史内容。
外部视频受站点和访问网络限制；知识库解析失败/未完成时先查看知识库的索引状态。

## 从旧版迁移（工程一次执行）

1. 正常PR/CI合并部署Gateway。备份原应用草稿/已发布DSL、V5路由及发布目标摘要。
2. 用合并工具离线生成候选：

```bash
uv run python tools/upgrade_consumer_flow.py <原草稿.yml> --gateway <可信根地址> \
  --live-documents --out <候选.yml>
```

工具保留模型、CPMS分支、库选择和应用特性，只增加动态知识模式、回答输出协议和对应提示词。
`knowledge_answer_mode=documents`为迁移标识，不要求产品每次操作它；新协议callback_version=4在发布时生成。
旧版本保留显式恢复能力，不批量修改不可变历史版本。

3. 按live-release.md的原Dify草稿同步API保存，原hash作并发检查；从既有试用入口验证新文档和视频。
   新模式frozen_settings只持环境/租户身份，不读取旧内容文件；目标文件可去掉content_file（兼容旧恢复时保留文件备份）。
4. 发布原应用，核callback_version=4、所选库、能力开关和workflow身份。新知识文档之后不用再发布。
5. 正常登录原H5，提问“有介绍印尼充电桩软件兼容性的视频吗？”，正文应基于KB-C-059且视频URL与其原文一致；
   问常见指导、本人状态和账务，核原API/blocks；私有保存QA/run、来源及实际Dify节点。
6. 发布目标与旧content文件摘要前后核对，证明本次资料无需加入旧审核包；从视频卡片核原地址。
   真实外部视频播放不能只靠HTTP200判断，浏览器播放器加载、站点限制应分别记录。

## 回归与恢复

复用`tools/verify_consumer_chatflow.py --knowledge-plugin ... --plugin-public-key ... --out ...`及
已部署同版DIFY_TEST_WEB_IMAGE。隔离栈发布后删本地审核文件，新增/更新/删除合成资料，再验知识和CPMS，
整段不修改工作流版本或服务器目标。真实供应商验证使用当前baoyun、已存在的本租户资料，凭据不离开受控进程。

失败时通过原restore恢复V5，再按保存前hash恢复草稿；不整库覆盖、不删除新增QA/run、不改租户权限。
仅保留旧文件和旧分支作为回滚兼容，不将旧FAQ答案作为动态检索失败时的自动回落。

迁移工具新增的模式变量不带description和selector，Dify保存时会补充它们。保存前给候选该变量补上
`description: ""`和`selector: [env, knowledge_answer_mode]`，再同步并逐字段核对；若保存后仍有差异，
只接受已确认的这两个补充值，不直接忽略整组环境变量差异。视频验收先检查卡片，再点击进入原youtube/player页核videoId及iframe源；
聊天页本身没有iframe不代表视频未交付。

## 多语言修复迁移

使用同一`--live-documents`命令从当前草稿生成候选，会同步start.language的11种选项，
并让回答提示显式使用App传入的语言。保留当前模型参数、库选择和CPMS能力，不覆盖并行草稿修改。
保存前后对照完整图，正常试用后发布；旧版本保留可恢复。新知识材料仍无需发布。

原H5验收路径为`/shopPackage/pages/user/language-select/index`：点语言、页面确认，再点切换确认弹窗。
只选条目不确认不会保存；刷新App首屏之前写localStorage也可能被初始化覆盖，不能用它证明点选行为。
用真实请求的Accept-Language与响应language核对。现役9项对应zh-CN/en/portuguese/spanish/th/vi/mn/km/zh-HK；
后端归一为zh/en/pt/es/th/vi/mn/km/zh-Hant。语言只控制展示，不改变本人订单授权。

隔离栈须同时使用同一签名对的插件/公钥及当前页面镜像：
`DIFY_TEST_WEB_IMAGE=aiops/dify-web-patched:knowledge-picker-20261027`，
`--plugin-public-key plugins/consumer_knowledge/signing/public.pem`。
错误公钥导致插件上传400；原版web不能渲染当前多选控件，不能据此判断语言修复失败。
