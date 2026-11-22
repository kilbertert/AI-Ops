# 正式开通实现：控制面配套与缺项回读验收

2026-11-22 12:14～12:25（北京时间）。PR[#1651](https://github.com/kilbertert/AI-Ops/pull/1651)
合并`c9d8f03cb7d0280fd9a1b2545a663522c8562e6e`，版本1.39.0。
本事件交付控制面、管理桥、Web和原worker；**没有在124发布应用、授予知识权限或启用租户**。

## 合并与配套

最终CI [44983526104](https://github.com/kilbertert/AI-Ops/actions/runs/44983526104)：5671通过/10跳过。
独立H1/H2及最后路径修复见[开发验收](2026-11-22-environment-publication-development.md)。
PR审查另发现阶段间取消后任务无法被worker领取恢复，已在`f0716128`修复；
原管理HTTP五个时点修前3失败/2通过，修后5通过，相邻117项通过，Codex复审无新发现、线程已解决。
Devin因额度不足跳过，不记为完整审查。

| 组件 | 实际部署与核对 |
|---|---|
| 41 Gateway | 标准CD [44984077408](https://github.com/kilbertert/AI-Ops/actions/runs/44984077408)成功；Console/schema/selfcheck与传输完整性通过，health实读1.39.0+c9d8f03cb7d0 |
| 36管理桥 | 同合并修订固定bundle经原deploy.sh安装；备份旧代码/JS/片段/unit，语法与匿名管理拒绝探针通过，服务active |
| 原后台worker | canonical已快进；原timer入口消费合并代码，880秒unit和900秒租约未改，没有重复安装unit或终止在飞任务 |
| 36 Dify Web | 从合并修订完整webpack构建，按原成品树上传/组装；运行镜像publication-c9d8f03c，signin200、环境页路由存在；仅Web容器替换，旧env/镜像保留 |

Gateway制品摘要`6d4b9b9c77bbe67f029dc57941ec52ff437a4e7b6dac0fd49f2955a1fdbb6757`；
桥包摘要`d2176ec3deab48856ad94571353bcf017be6ec1f67f3d5f77c3f02eb995ce868`；
目标Web镜像ID`e70169f850a52053395622d80b86ae3d5ecd281b9c362ee4206394ad5c164828`。
Web在目标只组装编译成品，不从未合并工作树运行代码。各组件有独立恢复点，不把同SHA当原子部署。

## 正常公司页面回读

12:24由人工输入新验证码正常登录，选124，实际页面观察：

- 生效目录仍显示“此环境尚未生效”；草稿V9与实际停用状态分开，保存/执行说明已更新。
- 既有验证任务的智能客服、异常订单、离线分析三项仍为`ENVIRONMENT_VALIDATION_SAMPLE_REQUIRED`。
- 该阻塞任务没有“发布并开通”按钮；本轮未提交publication、未填写对客资料授权、未发模型请求。
- 原页面创建新鲜同输入配置预检计划V9/R1，显示各项条件；刷新后读回同一历史验证任务与三项缺资料原因。
- 原生退出HTTP200；旧会话重放`SESSION_LOGIN_REQUIRED`且不返回身份。浏览器与本次临时管理连接结束。

这是**正常页面和历史缺项回读验收**，不是正式发布正向或授权API攻击矩阵。
跨环境/越权/取消恢复的本地实际入口证据仍按开发回执范围使用，不因页面没有按钮推定服务端全部安全。

## 目标保持与后继

124实读仍`1.36.1+9b5d3d0183b8`、PID1329880，无pending；环境/两租户停用、正式路由0，
agent_runs=3、chat_dify_runs=0、工作区2、应用4，与原D40终态相同。
本轮没有升级124、修改其凭据/配置引用、创建正式应用key/grant或执行准入恢复。
P3独立空实例已在[另一事件](2026-11-22-isolated-install-recovery-live.md)停用归档，不挪用其故障窗口。

下一现场步骤仍须有效的有订单消费者/有权后台身份、离线索引维护入口、正式网络和容量条件，
再明确目标升级及发布/准入/恢复窗口。控制面代码交付不等于P4现场完成；P5两环境各两租户三业务仍未完成。

原始CD、组件制品/恢复点、页面截图、正常会话与目标只读对照仅存私有
`~/.cache/aiops-onboarding-completion-20261122/publication-merged/`及`live-publication/`。
本页是该配套事件唯一仓内回执，不再新建同内容开发进度。
