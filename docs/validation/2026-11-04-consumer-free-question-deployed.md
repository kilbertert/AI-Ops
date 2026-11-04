# 自由提问修复部署与真机收尾

日期：2026-11-04。代码PR [#1352](https://github.com/kilbertert/AI-Ops/pull/1352)，
合并提交`ed73f12a09b8e919768d775674fccba9267beb77`；复审完成，无未解决线程。
最终[CI 41818035173](https://github.com/kilbertert/AI-Ops/actions/runs/41818035173)：
4136通过、10跳过。初版失败和修复过程保留在[调查记录](2026-11-04-consumer-free-question.md)。

## 部署证据

- 合并后未生成push触发的CD记录，使用既有`workflow_dispatch`运行当前main，未更改部署脚本或审批规则。
- [CD 41819002819](https://github.com/kilbertert/AI-Ops/actions/runs/41819002819)成功；部署逐文件摘要一致。
- 线上健康版本`1.0.20+ed73f12a09b8`，业务写操作仍禁用；部署前后问答/诊断在途任务均为0。
- 两份服务配置及正式路由行的摘要前后一致；没有为验收改密钥、清数据或发布另一套提示词。
- 注释标签`v1.0.20`指向上述合并提交，[版本说明](https://github.com/kilbertert/AI-Ops/releases/tag/v1.0.20)已发布。

## 原安卓页面

现装消费者APK1.2.15/code24，Android12；保留用户登录。使用adbx从客服页新对话开始，
通过截图确认输入框坐标后输入，未安装APK/输入法或注入会话。以下时间为UTC。

| 自由输入 | QA创建→完成 | 页面和服务端对照 |
|---|---|---|
| How to charge safely | 11:14:44→11:14:52 | completed/succeeded；343字新答案可见，UI与存储正文逐字一致 |
| How do I start charging | 11:17:28→11:17:34 | completed/succeeded；114字新答案可见，UI与存储正文逐字一致 |

两次均无订单目标，未返回原503。原始截图、UI、QA、配置摘要及日志保存在库外私有归档。
本轮设备临时XML、自己的隧道和保活会话已清理，保留登录和正常聊天记录。

## 收尾边界

代码22条变更路径已从origin/main逐项回读核对，本地main已快进，生成式CHANGELOG已更新。
#1345按“错误原因保留、外部依赖恢复、部署后自由提问成功”关闭。
两次服务端conversation_id仍不同，由[#1353](https://github.com/kilbertert/AI-Ops/issues/1353)跟进，
不能把同页两气泡记为上下文关联通过。历史上游原始错误未保留，不能补造当时具体HTTP码。
未完成业务验收：不宣称真实故障语义准确率、全部知识覆盖、多语言质量或长期模型稳定性。
