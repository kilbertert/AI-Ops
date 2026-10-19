# 36 上部署登录代理（可复跑的手顺）

> **读者**：要在 36 上更新 `aiops-dify-login-proxy` 或它那两个 nginx 配置的人。
> **状态**：2026-10-19 首次成文（#849 上线时实操并踩坑）。
> 决定的论证在 ADR-0023 / 0052 / 0053 / 0055；本页只讲**怎么做、怎么判、怎么回滚**。

## 0. 三句话

1. **代码是手工拷贝**，不是 CD。`/opt/aiops-login-proxy/` 不在仓库部署管线里。
2. **解包后必须核对属主** —— `tar -xzf` 会把归档里的属主/权限一起带过去，而服务身份是
   `aiopslogin`。这一步漏掉的表现是 `No module named aiops_diagnostics.dify_login_server`
   （**报错说模块不存在，实际是读不到文件**）。
3. **判断入口是否生效看端口与 `nginx -T`，不看 `systemctl is-active nginx`** ——
   那个单元从 2026-09-11 起就是 failed（既有债，报 "already running"）。

## 1. 取制品

从一个**已合并的 main 提交**打包，不要从工作区打：

```bash
cd <repo>
git rev-parse origin/main                       # 记下这个 sha，验收要用
git archive origin/main src/aiops_diagnostics | tar -x -C /tmp/stage
tar -czf /tmp/aiops-login-sync.tgz -C /tmp/stage/src aiops_diagnostics
sha256sum /tmp/aiops-login-sync.tgz             # 传完在远端再核一次
```

## 2. 备份（必须先做）

```bash
ssh yidong-36 'cp -a /opt/aiops-login-proxy /opt/aiops-login-proxy.bak-$(date +%Y%m%d%H%M%S)'
```

nginx 两个文件也备份，**文件名带时间戳**：

```bash
ssh yidong-36 'ts=$(date +%Y%m%d%H%M%S);
  cp /www/server/panel/vhost/nginx/dify-console-10008.conf \
     /www/server/panel/vhost/nginx/dify-console-10008.conf.bak-$ts;
  cp /www/server/panel/vhost/nginx/snippets/dify-login-proxy.conf \
     /www/server/panel/vhost/nginx/snippets/dify-login-proxy.conf.bak-$ts'
```

## 3. 传 + 解包 + **修属主**（这一条最容易漏）

```bash
scp /tmp/aiops-login-sync.tgz yidong-36:/tmp/
ssh yidong-36 'sha256sum /tmp/aiops-login-sync.tgz'      # 与本地比
ssh yidong-36 'cd /opt/aiops-login-proxy && tar -xzf /tmp/aiops-login-sync.tgz
  chown -R root:root aiops_diagnostics
  chmod -R a+rX aiops_diagnostics
  chmod -R go-w aiops_diagnostics
  find . -name __pycache__ -type d -exec rm -rf {} +'
```

**判据**：解包后 `ls -ld /opt/aiops-login-proxy/aiops_diagnostics` 必须是
`root root` 且 `a+rX`（`aiopslogin` 要能读）。不是就先修再重启。

## 4. nginx 两处配置（缺一不可）

`ops/dify-console-bridge/dify-console-10008.conf` 与 `ops/dify-login-proxy/dify-login-proxy.conf`
是**同一条链路的两半**：

- 10008 那一层用 `set_real_ip_from` + `real_ip_header` 修正 `$remote_addr`，
  并用 `map $realip_remote_addr $aiops_upstream_proto` 算出该上报的协议；
- snippet 里转发 `$aiops_upstream_proto`。

**只装 snippet 不装 10008** ⇒ `$aiops_upstream_proto` 未定义 ⇒ 登录代理拿不到
`X-Forwarded-Proto` ⇒ **所有登录被自己的闸门拒掉**。

```bash
nginx -t                                              # 必须 ok 才继续
nginx -s reload
```

## 5. 重启与版本核对

```bash
ssh yidong-36 'systemctl restart aiops-dify-login-proxy && sleep 3
  systemctl is-active aiops-dify-login-proxy'
ssh yidong-36 'sha256sum /opt/aiops-login-proxy/aiops_diagnostics/dify_login_server.py'
git show origin/main:src/aiops_diagnostics/dify_login_server.py | sha256sum   # 两个必须相等
```

## 6. 验收（四条，都是真入口）

| # | 动作 | 期望 |
|---|---|---|
| A | `POST http://36.156.159.175:10008/__aiops/login` 带明文口令 | 400「此入口是明文 HTTP…请使用 https://dify.ranlei.work 登录」 |
| B | `GET` 同入口的验证码 | 200 `image/jpeg` |
| C | `https://dify.ranlei.work` 取图 + **故意填错验证码** | 403「验证码不正确或已失效」 |
| D | 同一来源连续失败到阈值 | 第 N 次起 429 |

**C 必须用错码**。正确验证码 + 错误密码得到的是「密码不正确」——那条路修复前后都是 403，
**证明不了任何分类修复**。错码才是走 428 分支的那条。

D 的日志应出现 `source=<真实地址>`，**不是 `127.0.0.1`**（那是 realip 修正生效的判据）。

## 7. 回滚

```bash
ssh yidong-36 'systemctl stop aiops-dify-login-proxy
  cp -a /opt/aiops-login-proxy.bak-<ts>/. /opt/aiops-login-proxy/
  cp <conf>.bak-<ts> <conf>; nginx -t && nginx -s reload
  systemctl start aiops-dify-login-proxy'
```

nginx 两处要**一起**回滚（snippet 引用了 10008 那一层定义的变量）。

## 8. 已知的坑

| 现象 | 真因 |
|---|---|
| `No module named aiops_diagnostics.dify_login_server` | 属主/权限不对（见 §3），不是文件缺失 |
| `systemctl is-active nginx` = failed | 既有失效单元，端口才是判据 |
| 所有登录都 400「明文 HTTP」 | 10008 那一层的 realip/map 没装或没 reload |
| 登录后立刻弹回 `/signin` | 走了明文入口；会话 cookie 是 `Secure` + `__Host-`，http 源上被丢弃 |
