# M3 运行制品交付准备

分支 `chore/m3-runtime-delivery`，基线 `bbcd3c76`。

目标：补齐 Gateway CD 之外的运营桥、页面入口和节点制品，使 M3 可按合并修订部署。
发布桥打包代码及全部标准库依赖、JS、nginx snippet；安装失败恢复原文件。
节点锁更新至 M3-02 制品，Compose 增加默认关闭的 managed-publication 启动参数，保留原清单预检。

定向验证 284 passed；shellcheck、ruff 通过。服务宿主部署、登录运营/公司 App、真实供应商与
业务库整链未执行；不能把代码部署准备写成已启用。下一步从合并修订构建制品，按私有变更单
同步运行环境后执行受控业务验收。
