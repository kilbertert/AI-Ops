# M3 运行制品验收

基线 `bbcd3c76`，分支 `chore/m3-runtime-delivery`。

- 新增部署测试 2 项：真实文件安装、注入 nginx reload 失败后的完整恢复。
  systemd/nginx/curl 为命令替身，未接触服务宿主。
- 合计定向 284 passed（部署、原清单预检、桥权限/生命周期）。shellcheck 和 ruff 通过。
- 首轮有 1 项失败：节点锁已更新而 HOST-CHANGE 仍留旧摘要；同步变更单后通过，未改断言。
- 节点 JAR 来自合并修订 bbcd3c76，SHA-256 见部署锁；没有改 Java 实现。
- 打包后真实 HTTP 与 nginx 联验 6/6：三条新路由的授权转发与无会话拒绝。Dify/Gateway 为合成。完整 CI：待验证。

未完成业务验收：未调用真实供应商、未读取真实订单、未启用生产路线。部署脚本的本地恢复
不证明服务宿主恢复已演练；安装后的无会话拒绝也不替代登录运营的发布生命周期验收。

完整本地回归 3870 passed / 9 skipped（110.65 秒）；静态、格式、compileall、依赖与 policy 检查通过。
Compose 实际渲染验证默认 false / 显式 true，静态清单两侧仍保留。Gateway 镜像已构建并导出，
内容摘要与锁一致；构建身份已更新，未上传服务宿主。
