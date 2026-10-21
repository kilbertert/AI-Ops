# 70. inner_api 转发器：每次连接现查容器地址，查不到就断

日期：2026-10-21
状态：已采纳（#889 实施；ADR-0059 §「那条隧道怎么接」落成可执行件）
影响版本：`ops/dify-inner-api/`（新：转发器、两个 systemd 单元、README）、
`tests/test_inner_api_forwarder.py`（新）

## 背景

`POST /inner/api/enterprise/workspace` 是建 workspace 的唯一 HTTP 入口，而 api 容器的
`5001` **没有 publish 到宿主**（刻意的：管理接口）。宿主只能经 docker 内网访问它，
41/95 隔着公网。而 `dify_workspaces` 在**每个环境自己的** `gateway.db` 里 —— 36 写不到
41 的库，所以开户必须**在目标环境的机器上**跑。

## 决策

**一、一个只绑回环的转发器，每次 accept 现查容器地址。**

`aiops-inner-api-forward --listen 127.0.0.1:15001 --container deploy-api-1`。它不用 nginx
（宿主 nginx 解析不到 compose 容器名，而静态 `proxy_pass` 的主机名只在加载配置时解析一次
—— 容器重建换 IP 后写 `/etc/hosts` 也救不了正在跑的 nginx），也不 publish 5001
（那会把一个管理接口暴露到宿主网络）。它**不缓存**地址：没有缓存就没有"重建之后还往旧
IP 发、直到有人重启"的那种故障。

**二、fail-closed，且不转到别处。**

查不到容器（停了 / 改名 / docker 不可用）或容器端口没起 ⇒ **关闭这条连接**。挂起会让
调用方把"容器没了"读成"上游很慢"；转到别处更糟 —— 它把一次管理调用发给了另一个进程。
测试钉住了这一点：查不到时 `create_connection` **一次都不被调用**。

**三、绑定地址只允许回环，脚本自己拒绝。**

`--listen 0.0.0.0:15001` 会被 `argparse` 直接拒绝。这个转发器存在的全部意义就是"把一个
没 publish 的端口搬到回环上"，绑 `0.0.0.0` 等于把它 publish 出去。

**四、隧道与既有 kb 隧道同形，且这一层**不是**授权。**

41/95 一侧是 `ssh -N -L 127.0.0.1:15001:127.0.0.1:15001`，36 一侧是
`restrict,port-forwarding,permitopen="127.0.0.1:15001",command="/bin/false"` 的受限账号。
`permitopen` 只限制 SSH 的**远端**目标，不限制本地 —— 隧道建立后 41 本机任何进程都能连
15001。所以授权仍在 `X-Inner-Api-Key` 上，两层都要过。

## 被否掉的替代形态

- **`docker compose ports: "5001:5001"`**。一行就能让 41 连上，代价是把 Dify 的管理接口
  publish 到宿主网络（进而依赖防火墙去挡）。这正是#879 之后所有票都在避免的形状。
- **nginx `proxy_pass` 到容器内网 IP**。静态主机名只解析一次；容器重建后 ip 变化，
  而 nginx 会一直转发到旧 IP，且**没有任何报错**。
- **socat / 第三方 TCP 代理**。36 上没有 socat；为一条每月不到一次的管理路径引入一个
  常驻依赖，比 60 行标准库贵。
- **隧道里直接 `ssh 36 'docker exec …'`**。那要把 docker 权限给隧道的远端命令，
  而 `docker` 组等价于 root。转发器只读 `docker inspect` 的输出。
- **把 15001 的授权做成"隧道即授权"**。见决策四：`permitopen` 限制不到本地调用方。

## 后果与代价

- **36 上多一个常驻单元**（`aiops-inner-api-forward.service`），只读 `docker inspect`。
  它不含凭据；真正的钥匙是 `X-Inner-Api-Key`，在 41 的 `gateway.env` 里。
- **41 上多一个常驻隧道单元**。隧道断了 ⇒ `ensure-workspace` 立即得到连接失败
  （不是超时到天荒地老）—— 这是 fail-closed 的另一半。
- **容器名是一个配置值**（`--container deploy-api-1`）。compose 改名后要同步改这一行；
  改错的表现为"所有调用都连接失败"，而 `journalctl -u aiops-inner-api-forward` 会打印
  `{"refused": "container address unknown"}`。
- **转发器不会自证授权**：任何人只要能在 36 上绑 15001，就能把内部调用转给任意地址。
  所以它的单元以 root 跑、只绑回环，且**不提供**任何"目标可配"的运行时参数给调用方。
