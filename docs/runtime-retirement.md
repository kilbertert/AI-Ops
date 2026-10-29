# Python Thin Harness 与诊断 CLI 退役

2026-10-28按用户决定实施，2026-10-29退役版本已部署并通过双端原H5及Dify预览的链路验收。
[验收回执](validation/2026-10-29-runtime-retirement-live.md)保留失败与准确率/稳定性边界；目录迁移在清理验收后逐片进行。

## 执行路线

| 入口 | 退役后的执行方式 |
|---|---|
| C端普通问题 | 已发布 WF01/Dify 绑定，保留本人权限、版本、知识与单轮凭据 |
| C端订单检测快捷动作 | 本人订单校验后走 C端工作流状态分支；不再启动多源 Python 根因推理 |
| 管家单订单 | 公司主体范围、Java 节点 CPMS 两工具；未绑定拒绝 |
| Dify 草稿试用 | 必须给 app_id；按已登记应用选择 ConsumerTrial / WorkflowTrial / NodeTrial |
| 通用 Python 问答、旧 debug-run | 删除；没有业务绑定时不继续回退 |
| 诊断/恢复/交互式/设备 CLI | 删除，设备注册/run/event/evidence HTTP 面同时移除 |
| 运维管理 | 独立 aiops-admin，保留开户、知识同步与发布管理 |

## 删除与保留

删除旧协调器、工具循环、诊断合同与验证器、工作区/证据日志、Codex SDK/launcher、
Responses 兼容代理、旧 HTTP 诊断源、Jev/旧通用分类回退、旧七工具目录及共享租户工具 provider。
删除旧费用/占位费、通信报文、Redis 同步流与 doctor 取证，删除诊断 CLI、设备客户端令牌存储、PyInstaller/便携工作流和仅服务旧链的验证工具。

保留公司鉴权、QueryScope/订单规则、当前 SQL/TDengine 共享读取与 Redis 登录凭据、健康/分析、
知识与发布、Dify/Java 任务账本及历史记录。`agent_debug` 只保留知识库发布校验，
`qa_rag` 只保留开场白所需元数据；命名归位留到目录迁移。
聚合说明和 C端语义分类使用 `model_request` 的一次性有界 HTTP，无 subprocess、会话或工具。
现有 AIOPS_CODEX_* 端点/密钥键名暂保留给已部署配置，运行器、sandbox、turn/恢复配置已删。

## 兼容与部署前置

旧 `/v1/enroll`、`/v1/runs`、`/v1/standard/diagnoses`、`/v1/agents/{id}/debug-run`、
旧 Dify 工具/共享服务凭据端点返回 404；旧调用方须走统一助手或对应业务接口。
Dify 试用不再接受 prompt/allowed_tools 的 Python 草稿。CLI 使用方改用 aiops-admin；
不提供空壳命令、隐藏旧执行分支或 legacy 源码副本。

生产升级前核对 C端/管家的发布绑定及提供方 endpoint：旧 Responses 适配器进程退出后，
现役单次请求须指向真实 provider 的兼容接口。源代码退役不自动关闭生产服务或改环境文件。
已有数据库与证据不删；新库不再建设备专属表。历史结果导出可从备份处理，旧 HTTP 回读已退役。
标准诊断表仍服务分析/试用，沿用原存储与终态安全保证。

## 验证范围

保留现役业务测试，删除旧执行器专属场景；补充旧模块/路由不可达、旧数据保留、
C端检测工作流、跨语言、Dify/Java 试用、模型无工具/拒绝不完整输出与密钥权限检查。
Gateway 技能起真实临时服务验证；远端执行与真实故障准确率单独验收。
退役切换已部署并通过文首链接的真实验收；后续目录片的部署与验收以各片记录为准。
