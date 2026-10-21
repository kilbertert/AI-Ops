# `aiops-inner-api-forward`（#889）

宿主回环上的一个转发器：`127.0.0.1:15001` → Dify `api` 容器的 `5001`。
它存在的唯一理由是让 **41（以及将来的 95）能在自己的机器上**跑
`aiops admin ensure-workspace`，并写**自己的** `gateway.db`
（见 `docs/adr/0068-claim-before-create-and-two-kinds-of-failure.md`）。

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

## 部署（36）

```bash
# 1. 脚本
sudo install -m 0755 aiops-inner-api-forward.py /usr/local/bin/aiops-inner-api-forward

# 2. 单元（只绑回环；容器名以 `docker ps` 为准）
sudo install -m 0644 aiops-inner-api-forward.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aiops-inner-api-forward

# 3. 自检：只监听回环 + 5001 仍未 publish
ss -ltn 'sport = :15001'                  # 必须是 127.0.0.1:15001
sudo ss -ltn | grep 5001 || echo "5001 not published (correct)"
sudo docker ps --format '{{.Names}} {{.Ports}}' | grep deploy-api
```

## 隧道（41 / 95 → 36）

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

停掉两个单元（41 的隧道、36 的转发器）即可；api 容器与其 publish 状态不受影响，
`ensure-workspace` 回到"只能在 36 本机跑"的旧状态。
