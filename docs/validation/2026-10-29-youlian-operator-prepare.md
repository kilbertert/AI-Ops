# 优链充管家多租户准备回执

日期：2026-10-29（北京时间）；代码基线 `1b3eec15`，现场 Gateway `2662c3fa`。
本记录是准备阶段，不代表下述待验证项已完成。

## 实际完成

| 对象 | 结果 |
|---|---|
| 公司租户 | 用户指定优链充，`1941847786056323072` |
| 工作区 | `9d2ccbac-5842-4b8a-8d02-36c5598528df`，`aiops-41-优链充-56323072` |
| 应用 | `349a637c-8f86-41b9-b932-e9818fb39a97`，`优链充-管家订单诊断`，agent-chat |
| 工具 | 本工作区 cpms-node，两项工具均开启、无模型参数、无共享业务凭据 |
| Dify模型 | 与源应用相同，v4.1模型；4096预算、tool_call/supported已回读 |
| Java候选 | 实际 Dify 导出可生成双工具清单；模型仍是平台 deepseek-v4-flash |
| 源保持 | 源 model_config、名称和三条正式路由与复制前快照一致 |

MT-01：新应用独立 UUID，本工作区 provider UUID；源提示词SHA-256逐字相同，源无变化。
MT-04部分：目标仅一个 app；重复 ensure_node_provider 返回 unchanged。
应用隔离补证：用A workspace读B app、B workspace读A app均404；将A工具UUID塞入B候选被拒绝。
以上为真实管理 API 与候选验证，不是合法A/B用户会话的业务越权验收。

## 自动化与失败记录

- `ruff check .` 与 `ruff format --check .`通过（941文件）。
- 相关集合165 passed：manifest、workspace、ready、node-publication、native-preview、operator-cpms。
- 临时目录中使用真实 AgentManager 建立空旧工具初始版本、加载环境清单并重复登记，通过。
- 只读检查脚本曾因slots对象、SQLite列名、UUID序列化不匹配退出，已按实际结构修正后重跑；
  没有据这些错误重建工作区或修改业务数据。
- 业务库账号读取另一个UPMS库被拒，本轮未提升数据库权限。租户ID来自用户，真实登录身份待验。
- 无新增生产逻辑或安全规则，不新增重复实现的测试；复杂度/突变分析不适用于本次声明与手册变更。
  `971685bf`的CI为3318 passed / 10 skipped；后续文档补充的最终CI以PR为准。
- 按 `verify-aiops-gateway` 在一次性根启动真实服务，8项启动/HTTP检查通过；命令为
  `uv run python .claude/skills/verify-aiops-gateway/scripts/observe.py --out dist/verification/youlian-gateway`。
  首次使用库外输出路径触发技能日志relative_to异常，HTTP均通过但进程退出1；改用技能支持的
  库内dist路径复跑退出0，未修改技能实现。
- 注册专项另起真实Gateway、临时数据库和合成Dify HTTP服务：无管理key401、未登记app404且
  不访问上游；新app请求实际带目标workspace，合成无效DSL返回502后停止，不执行真实节点。
  这证明运行中的租户登记解析，不能替代远端Java/公司身份整链。
- 目标工作区真实模型冒烟：4096预算、无业务工具、message_end；保存配置未变。
  工作区重跑返回ready且零写入；无节点任务授权直接调两工具均403。
- PR审查补齐了初始版本显式publish和可执行登记/目标追加片段；目标追加片段已现场dry-run。

## 尚未完成

Gateway目标登记、节点目标和新路由要在清单合并后应用；本提交阶段不宣称已开放。
MT-02、MT-03、MT-05待验证：真实正常公司登录/订单、合法跨租户会话反例及无订单原生交互。
**未完成业务验收**，更不代表故障根因准确率。用户表示已有账号、稍后提供订单。

私有备份记录源应用/DSL、目标候选、模型掩码配置、节点目标与路由；无令牌或业务原件入仓。
操作知识自检：本次dev-host、开户、插件/模型配置、provider创建、导入和候选验证步骤已写入
[多租户手册](../agents/operator-cpms/multitenant.md)。不回滚整库、不改变原租户业务入口。
