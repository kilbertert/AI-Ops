# 运维入口与适用边界

更新：2026-11-04。运行版本、启用与验收见[交付索引](../docs/agents/current-delivery-state.md)，
本页只说明方法入口。部署与读取主机沿用项目声明及库外受控清单，不从历史记录猜地址或凭据。

| 任务 | 现役方法 |
|---|---|
| Gateway部署、依赖升级、恢复、只读现场核对 | [环境手册](../docs/agents/env-41-runbook.md)、[依赖升级](../docs/agents/env-41-dependency-update.md) |
| Dify控制台连接预检与预览恢复 | [连接手册](../docs/agents/operator-cpms/dify-console-preflight.md)：HTTPS或已登记隧道回环地址；部署与启动均校验 |
| Dify/节点配套交付 | [Dify资产](../deploy/dify-36/README.md)、[节点资产](../deploy/node-runtime/README.md) |
| 平台按客户开通两端与知识空间 | [自助开通](../docs/agents/operator-cpms/self-service.md)、[业务环境清单](../docs/agents/operator-cpms/business-environments-catalogue.md) |
| 消费者编辑、试用、发布与恢复 | [正式发布手册](../docs/agents/consumer-wf01/live-release.md) |
| 管家协议/路由迁移和恢复 | [迁移手册](../docs/agents/operator-route-migration.md) |
| 目录、PR/issue和分支工作树收尾 | [收尾方法](../docs/agents/stage-closeout.md) |

管理命令是独立 `aiops-admin`，例如 `aiops-admin --config <私有配置> reconcile --help`；
旧 `aiops ... admin`、Python诊断、七工具、共享服务主体provider和旧 `/diag/*` 取证路线已经退役。
现役订单证据使用授权的CPMS接口；不能因缺API恢复旧直连工具。数据库与历史证据保持保留。

## 环境清单与发布归属

`ops/environments/` 保留部署声明与遗留登记；它不替代受管Dify页面的正式选择。
`[[agents]]` 描述清单管理的内容，`[[dify_apps]]` 描述登记映射。
页面管理的应用、工作区、草稿和正式路由须通过当前受管流程读取与修改，
不能用旧清单覆盖；执行reconcile前先dry-run，遇到页面归属冲突就停止并核对差异。

Dify工作区对应业务租户，Gateway仍独立执行请求授权。workspace可见不等于订单可见，
服务key不能代替个人权限。当前开通路径和资料维护见[两端手册](../docs/README.md)。

## Dify受控端点

Dify可达的端点及鉴权登记在[暴露清单](dify-exposure-registry.md)，
`tests/dify/test_dify_exposure_registry.py`检查登记与路由一致；运行时探针不等于业务验收。
内部回调只接受本轮授权，不能经消费者H5代理替换凭据。

# WF01知识回调部署

与[WF01配置](../docs/agents/consumer-wf01/README.md)一起部署的片段为
`ops/nginx/dify-consumer-callback.conf`，目标位置：
`/www/server/panel/vhost/nginx/extension/api.mall.qushiyun.com/dify-consumer-callback.conf`。
它精确匹配`POST /v1/internal/consumer-knowledge`，保留Dify的一次性Authorization，清空业务身份头；
不能放进消费者H5的服务令牌include，也不能经通用App `/v1/`代理替换凭据。

从已合并提交取制品，先备份已有同名片段（不存在也记录），再用`dev-host cp 41 <制品> --dest <目标路径>
--artifact-sha256 <摘要>`部署；保持0644。经`dev-host exec 41 --allow-service-exec --`执行
`/www/server/nginx/sbin/nginx -t`，通过后同一二进制`-s reload`。
失败时恢复原片段；原来不存在则只删除本次新增文件，重新检查并reload。
回执分别记录制品摘要、配置校验、reload、回调结果，不能把上传成功当部署完成。
用已撤销验收凭据只读复核`GRANT_REVOKED`而非`GRANT_UNKNOWN`；新有效凭据不得代发或重放。

# 消费者 H5 的助手面转发（`ops/nginx/`）

`ulink.h5.mall.qushiyun.com` 是个只有静态回退的站点：`/v1/` 下的请求不到网关，
`GET` 拿到的是 SPA 回退的 `index.html`（gzip 780 B），`POST` 直接 405。
于是聊天页唯一的症状是 "Failed to ask"，而访问日志里那几条 `200` 看着像成功。

`ops/nginx/ulink-h5-assistant.conf` 是装到该 vhost `extension/<host>/` 的 server 级
include，**只转发 H5 聊天页实际调用的 12 条路径**。清单的唯一声明处是
`ops/nginx/h5-assistant-routes.txt`；`tests/test_h5_assistant_routes.py` 用**真的 nginx**
断言「声明的都转发」+「网关其余 76 条一条不转发」。

**按方法拆 location，不写一条路径正则**：`location ~` 只匹配 URI、不看方法，
一条正则会顺带放开 `POST /v1/shortcuts`（创建快捷键，管理面）。
`limit_except` 也不够 —— 它在静态回退之后才生效，实测 `POST` 照样进代理。

宿主安装属于**产物部署**（CD 只管 `src/` 到 `/opt/aiops-41`），
按 `deploy/manual-deploys.md` 留痕。回滚 = 删两个文件 + `nginx -t` + reload。

新版WF01同一片段增加精确范围`/v1/internal/consumer-data/(knowledge|status|billing)`的POST转发，
保留旧consumer-knowledge供旧发布版与显式恢复使用。新凭据在Gateway会拒绝旧回调，不能靠URL切换能力。
Nginx仍清空消费者/运营身份头，只透传本次grant。未匹配的后缀不能落入此内部转发规则。
