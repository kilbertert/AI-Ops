# `aiops-inner-api-forward`（#889）

> **36 宿主总览**：Dify 36 全体组件拓扑与分发规范见 [`deploy/dify-36/ARCHITECTURE.md`](../../deploy/dify-36/ARCHITECTURE.md)；本转发器因需访问宿主机 `/var/run/docker.sock` 执行 `docker inspect`，按 ADR-0367 保留为宿主机 systemd 服务。

宿主回环上的一个转发器：`127.0.0.1:15001` → Dify `api` 容器的 `5001`。
它让共享控制Gateway在自己的机器上调用Dify工作区、Console及Service API，
并跑
`aiops-admin ensure-workspace`，并写**自己的** `gateway.db`
（见 `docs/adr/0068-claim-before-create-and-two-kinds-of-failure.md`）。
第二环境使用公司的既有HTTPS业务入口，不给租户服务器新增专属业务隧道。

## 为什么需要它

`POST /inner/api/enterprise/workspace` 是建 workspace 的唯一 HTTP 入口，而 api 容器的
`5001` **没有 publish 到宿主**（这是刻意的：它是一个管理接口）。宿主只能经 docker 内网
访问它；41 隔着公网。

## 为什么不是 nginx，也不是 `ports: 5001:5001`

- **nginx**：宿主 nginx 解析不到 compose 的容器名，而静态 `proxy_pass` 的主机名只在
  加载配置时解析一次 —— 容器重建换 IP 后，写 `/etc/hosts` 也救不了正在跑的 nginx。
- **publish 5001**：那会把一个管理接口暴露到宿主网络，正是要避免的事。

所以转发器**每次 accept 现查**容器地址（`docker inspect`），没有缓存，也没有要 reload 的
配置。查不到就**断开**（fail-closed），不转发到别处、不挂起。

最多32条连接独立搬运，超出额度的新连接立即关闭，正常结束及异常都释放额度。
流式回答连接保持时，工具回调仍能经同一入口读取Console；串行搬运会让两者互相等待。
这不改变Dify的应用/管理鉴权，也不把一次可达当作业务验收。决定见[ADR0294](https://github.com/kilbertert/AI-Ops-history-archive/blob/4bed22cbabbcbe80f242c83275a46d3f550f996d/docs/adr/0294-inactive-consumer-trial.md)。

## 部署（36）

```bash
# 从已合并修订构建固定包；不携带凭据或改动现役单位。
bash ops/dify-inner-api/build-bundle.sh <已合并修订> /private/forwarder.tar.gz
# dev-host传包并核外层摘要，解包核SHA256SUMS；更新前确认旧连接已结束。
bash <固定包>/deploy.sh install <固定包> <全新私有备份目录>

# 自检：只监听回环 + 5001 仍未 publish
ss -ltn 'sport = :15001'                  # 必须是 127.0.0.1:15001
sudo ss -ltn | grep 5001 || echo "5001 not published (correct)"
sudo docker ps --format '{{.Names}} {{.Ports}}' | grep deploy-api
```

安装入口只更新已存在的转发脚本并重启其自有单位；不改Dify容器、公司服务、隧道、配置或暴露。
备份旧脚本及单位快照，部署失败恢复旧脚本并重启；取回当前匿名鉴权端点仍为401，随后执行
“保留连接→并发请求→关闭”的真实TCP验收。重启前核在飞连接，不能为验收中断原流式请求。

## 既有共享管理连接（41 → 36）

形状与既有的 kb 隧道（`aiops-36-kb-tunnel.service`）相同：
`restrict,port-forwarding,permitopen="127.0.0.1:15001",command="/bin/false"` 的受限账号 +
只绑回环的 `ssh -N -L`。

```bash
# 36：受限账号的 authorized_keys 一行（key 只授权到 15001）
restrict,port-forwarding,permitopen="127.0.0.1:15001",command="/bin/false" ssh-ed25519 AAAA… aiops-inner-api

# 41：单元（见 aiops-inner-api-tunnel.service），然后
sudo systemctl enable --now aiops-inner-api-tunnel
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:15001/inner/api/enterprise/workspace
# 401 = 隧道通了、`X-Inner-Api-Key` 还没带（正确：两层认证都在）
```

## 访问边界（与设计 §「那条隧道怎么接」一致）

| 边界 | 值 |
|---|---|
| 5001 | **永不 publish 到宿主**；不得因为这条需求去改 compose 的 ports |
| 36 上的监听 | 只 `127.0.0.1:15001`；转发器**拒绝**非回环的 `--listen` |
| 谁能连 | 只有 `permitopen` 白名单里的那两把 key |
| 隧道建立后 | 41 本机**任何进程**都能连 15001 —— `permitopen` 只限制 SSH 的远端目标，不限制本地。**这一层不是授权** |
| 认证 | 隧道之外仍有 `X-Inner-Api-Key`，两层都要过 |

## 回滚

脚本升级回退使用同一个固定包及原备份，不停隧道、不改容器：

```bash
bash <固定包>/deploy.sh restore <固定包> <原私有备份目录>
```

退休入口另需实际流量和独立授权；不能把版本回退做成停用共享管理连接。
