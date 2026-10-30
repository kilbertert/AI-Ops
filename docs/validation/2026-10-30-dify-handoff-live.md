# 交接补漏部署回执

日期：2026-10-30（北京时间）。本记录续接[独立复核](2026-10-30-dify-handoff-acceptance.md)。

## 已部署制品

| 对象 | 当前制品 | 证据 |
| --- | --- | --- |
| Gateway | 3a6bd97ac03fced85ee449124bd588318d7df896 | #1178合并，CD 40988782795 success，health 1.0.0+3a6bd97ac03f |
| 登录代理36 | 同一合并修订3a6bd97a | 源码sha256 11a452de4e75f4ef47280b7a7524686141cf59b74cfc9e3e366ffe51bd81a383；服务active |
| Java节点41 | 6bcc7fda-51b249810095 | JAR摘要与合并源码构建一致，running/restart=0/OOM=false，其他3容器ID不变 |
| 41环境清单 | 6bcc7fda中的env-41.toml | sha256 6d1b5b44f728041065f965bcca8252fef900237a28e5e547250bc51349913bbf；登记守卫冲突0 |
| Dify web | knowledge-1be3f067 | 镜像ID 0ac260feb0f13c5458ccb1cdb669ceadd48c2beefbb6d71ee9ce1d45317d27ef，沿用知识修复版 |

登录代理归档摘要63e2a4e50adf06cf3e059e9cf0362d8e9ddcc1c8e73fff0f18ca3d1a60c2832a，
先备份、完整包替换、保留服务身份aiopslogin；备份`/opt/aiops-login-proxy.bak-handoff-3a6bd97a`。
内存公司授权随重启清空，部署后走正常公司验证码登录；不注入会话或修改公司权限。

## 验收与检查

- #1178 CI：3429 passed/10 skipped；本地3430 passed/9 skipped（CI缺少dev-host的约定环境差异）。
  Workflow policy/spec-tree lint通过。AI审查因额度耗尽未执行，无未处理审查线程。
- Java升级前后同账号同订单：failed → completed；原生面板完整报告、run succeeded/matched，双CPMS证据。
- 平台与普通优链充账号均可打开本租户RAGFlow；普通账号改成Ulink workspace明确拒绝。
- 10月29日真实建库解析检索、Dify同步与检索、跨工作区403的原始资料已复核；测试库已删除，未重建业务数据。
- 部署登录代理后，平台账号正常验证码登录，开通/知识按钮可用，进入优链充RAGFlow成功。
  普通优链充账号同样正常重新登录并进入本租户RAGFlow，未注入会话。

## 交付与恢复

PR #1176因旧提交缺少No-ADR说明关闭，由#1178替代；未force-push或修改CI规则。
#1178分支本地/远端已清理，旧#1176工作区按preserve记录留作失败审计。
本回执工作区docs/dify-handoff-live，基线3a6bd97a，待文档CI合并后退役。
新客户由页面一键开通，无需先写服务器清单；旧清单迁移的一次性方法见self-service手册。

只证明本次开户知识链、正常登录和订单诊断链路；30天稳定性、容量压力、公司停用账号传播、
任意文档质量及工程师确认的故障准确率未完成业务验收。
