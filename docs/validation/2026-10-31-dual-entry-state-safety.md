# 双端状态与发布安全验证

基线f9865f17，分支fix/dual-entry-state-safety。验证只在临时根，未改生产数据。

五项旧反例复跑全部失败；修复后撤销、凭据、日志、摘要和建库回执回归通过，并新增旧token字典
迁移与显式重新授权测试。已跑开通/生命周期/知识授权46项通过，新增7项状态安全通过。
R13先复现原用例第10轮返回200，再固定observed_at为01:47:12稳定复现，换01:46:12拒绝。
原因是_evidence_numbers把时间中的47当成业务数字；不需要并发才能复现。
取证时间和订单标识只允许完整引用，不把拆散的数字作为金额依据；两条固定时间负例与完整时间正例验证。

原失败及构建/测试日志在本机私有cache的aiops-dify-self-service-research/direct-takeover与state-safety文件。
全量3666 passed / 9 skipped / 1 error；唯一error是H5固定测试端口18098被占，
单独重跑该文件3 passed。未将全量首次结果写成全绿。最终专项28 passed，
5个定向变异全部被测试杀死（重授、明文、草稿窗口、重复建库、时间冒充金额）。
coverage分支统计：KnowledgeGrants所在模块97%、consumer_reply 96%、service_keys 77%，
总体86%；复杂度validate_reply 19、ensure 29，仍有既有恢复分支未覆盖。
ruff与format通过，Gateway镜像构建通过；临时根真实Gateway启动及8项HTTP检查通过。
首次observe输出目录在仓库外导致末尾展示报错，改用文档规定的dist目录复跑成功。
本轮未执行远端运维命令，生产验收待完成；**未完成业务验收**，不把这批回归当成双端服务已可用。
