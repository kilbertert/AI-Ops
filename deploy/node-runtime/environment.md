# 共享平台中的独立环境节点

适用已批准的共享平台＋每环境接入模块档案。此项目只启动`environment-node`和`gateway-egress`，
复用现役平台与模型服务；不在目标业务服务器装Java/平台，不新开宿主端口或业务SSH隧道。
工具白名单继续来自现役声明；设计见[ADR0275](../../docs/adr/0275-shared-environment-node-egress.md)。

## 构建与预置

```bash
bash ops/environments/gateway/build-environment-node.sh <合并SHA> <锁定节点jar> /private/environment-node.tar.gz
PYTHONPATH=<解包根>/src python3 <解包根>/tools/content/render_node_callback.py \
  --environment-egress --gateway https://<目标正式域名> --out <独立配置根>/callback.conf
```

包不含凭据、平台jar或数据库；固定REVISION和SHA256SUMS，节点jar必须匹配该修订的部署锁。
固定Nginx镜像在Compose中按摘要声明，提前拉取/导入并核身份；正式启动用`--pull never`。
内部上游DNS在Nginx启动时解析；目标DNS更换后重新预检并定点重建出口，不能假设自动热更新。

先核共享宿主角色/访问路径、资源及现役组件ID；两个external网络必须已存在且`Internal=true`。
项目名、节点注册别名及Snail app ID全局唯一，不收养未知同名项目、不改现役node配置。
已有项目服务身份运行两容器，文件权限沿现役纪律；配置/状态分别置于项目已登记根下的环境子目录。
运行env（0600）提供Compose中所有`AIOPS_*`部署参数；不使用默认环境或默认目标。

- `node.env`只含本组Snail应用ID/token与节点token（0600），不得写公司会话、数据库或模型供应商密钥。
- `tls/callback/server.crt`及`server.key`由现有内部CA签发，SAN为`gateway-egress`，私钥仅出口可读。
- `tls/public/truststore.p12`信任现役平台/模型CA及回调证书，节点只挂公开信任库。
- `exec-manifest.json`为现役受控模型/工具模板；与目标Gateway发布绑定一致。
- Gateway节点登记固定新应用/agent/openId、节点凭据摘要与逻辑环境；`snail_base_url`使用已授权共享HTTPS入口。
- 管家目标`proxy_base_url`为节点内部`https://gateway-egress:9443`；`cpms_base_url`仍是该环境公司API，二者不能混填。

资源登记使用平台原管理API并读回，不能直接写平台库或运行会修改管理员/demo应用的全量bootstrap。
新组与旧组独立；平台现有同名资源需逐字段核对归属，不能静默改绑未知资源。

## 启动、验收与恢复

只通过受管主机入口传合并制品和配置，记录具体授权/摘要/恢复点；启动前冻结目标配置及原共享容器ID。
使用独立项目，external网络保持原样：

```bash
docker compose -p <独立项目名> --env-file <私有运行env> \
  -f <固定制品>/deploy/node-runtime/environment.compose.yaml up -d --pull never
```

检查容器服务身份、OOM/重启、网络无宿主ports；节点不能接egress，出口不接平台/模型网络。
等待实际能力回调、工具声明摘要及平台注册一致；再用正常公司登录的候选业务请求核唯一run、节点、
清单matched及授权数据，核原节点/平台/存储/模型容器ID保持。未完成这些前不启用租户目录。
内部能力上报最多等待240秒，节点启动可能受CPU限额影响；健康/心跳不是业务验收。

失败先停止新任务接入，保留诊断回执与新状态；只对本项目`stop`，不停止共享平台/旧节点。
配置或模板已下发时按Gateway受管配置事务恢复，保留新run账本；不要用旧业务库覆盖新状态。
首次试点不自动删除已创建应用或知识资源，按归属核对后另做退休。升级同样保存原制品/env及回退回读。

```bash
uv run pytest tests/integrations/test_environment_node_egress.py tests/test_node_runtime_deploy.py
```

本地双段TLS证明声明路径、凭据/合同头、证书拒绝与独立部署形状；实际隔离Docker运行和现场业务分别留回执。
