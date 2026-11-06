# 0236：开通生成的应用密钥属于持久运行状态

状态：已接受

## 原因与决定

原任务在依赖修复后走到consumer:key，systemd沙箱返回EROFS。密钥目录虽归服务身份，
配置却放在/etc；宿主权限可写不能推出服务命名空间可写。

按[systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#ProtectSystem=)
的官方定义，ProtectSystem=full会把/etc挂为只读；StateDirectory的系统路径属于/var/lib。
本项目沿用已批准的/var/lib服务数据根，运行生成密钥迁到其私有子目录；保留旧文件，
让现役路由已保存的绝对引用继续有效。不取消/etc保护或给整段配置树开写权限。

在客服依赖步骤和申请新Service key前，用同一私有原子文件原语实际写入无密钥探针，
失败映射SETUP_KEY_DIRECTORY_UNWRITABLE。已有有效key仍可只读复用。探针只证明当时可写，
后续失败仍依靠已有持久化对账账本恢复，不声称原子跨系统事务。

部分完成仍可从页面重试；队列状态先变pending，成功端不重建。

## 备选方案

给旧/etc目录增加ReadWritePaths只需改unit，但会混淆配置与运行状态并永久扩大配置写面，故不用。
只改目录能解眼前问题，但下次只读挂载或满盘仍在创建外部key后才失败，故加最小实际写入预检。
在每次开通搬迁全部历史密钥会引入额外生产副作用；迁移由部署一次执行，保留原文件。

## 代价与验证

每次客服开通多一次小文件写入，失败可在创建客户库/应用前定位。原私有路径检查不减弱。
最小回归从真实SelfService入口验证先拒绝后恢复，已有对账测试继续保护响应丢失；
生产验收仍要点击原任务，检查双端草稿及正式路由。
