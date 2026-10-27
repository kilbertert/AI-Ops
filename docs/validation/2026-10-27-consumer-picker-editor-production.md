# 知识节点editor 403线上修复

日期：2026-10-27（北京时间）；PR #1108合并69409638，v0.6.1。

用户反馈的英文权限提示来自Dify的dynamic-options：admin/owner限定与公司映射editor不一致。
使用现役数据库中同一个system/editor Account，调用完整端点装饰链及实际凭据目录：部署前403同文案，部署后200且返回3个本租户库。
复核在容器内使用已有维护授权满足CSRF前提，没有修改账号角色、伪造个人登录或旁路角色守卫；用户浏览器刷新后仍待其确认。

## 证据

- 最终头8ed4d08：verify/Workflow policy/spec-tree通过，CI4121 passed/10 skipped；16个合并文件逐字核验。
- 原Dify镜像角色矩阵13组通过；普通成员、错误工具与不可见凭据拒绝。真实editor浏览器选库/保存，
  然后既有管理身份试用/发布/恢复，隔离95/95通过。三组变异由断言检出；最终定向25 passed。
- 现役从合并树生成完整6文件补丁树，原5文件逐字不变；compose新增api/worker的plugin.py只读挂载。
- 第一次冷启动探活8秒超时触发自动回滚；原compose/五文件补丁逐字恢复，旧API健康。修正单次超时处理并在180秒总截止内轮询后重跑成功。
- 最终api/worker运行，容器内plugin.py摘要8e0ee3188e12与合并制品一致；.env未改，正式WF01仍V5/revision4/active。
  本轮未修改Gateway业务源码、未发布新WF01版本、未调用模型或真实业务查询；Gateway自动CD未触发。
- 第一版CI缺少Dify镜像多跳过1项而失败；改为按固定digest拉取并实际执行，不增加跳过阈值。

## 恢复方法

按[部署手顺](../agents/consumer-wf01/knowledge-picker-deploy.md)，恢复部署前compose及完整patched-api目录，
重建api/worker、等待健康；不回退web/plugin_daemon，不改账号角色或业务数据库。
备份在项目专用consumer-editor-69409638及-r2目录，私有部署回执保存精确路径与摘要。
探活使用curl的连接/总超时，并把单次TimeoutExpired作为未就绪继续轮询；总截止到期才回滚。

**未完成业务验收**：本轮不是故障诊断准确率验收；未拿用户个人浏览器会话重放，线上依据是同账户真实角色与完整端点，
页面点击依据是隔离真实editor浏览器。用户后续确认前，不声称其浏览器已经看到列表。
