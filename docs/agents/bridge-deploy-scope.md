# 36 上的控制台桥：哪些改动 CD 不会带过去

> 2026-10-24 实做时发现的缺口。写下来是因为它**不报错**：改完代码、CI 绿、CD 绿，
> 桥却还在跑旧逻辑。

## 事实

`deploy-41.sh` 用 `git archive … src/aiops_diagnostics` 推 41 的网关。
**桥不在那条链上**：它跑在 36 的 `/opt/aiops-console-bridge`，从 `ops/dify-console-bridge/`
的 unit 启动，代码是**手工安装的副本**。

所以下面这些改动，合并进 main **不等于**到了生产：

| 改了什么 | 要去哪里装 |
|---|---|
| `src/aiops_diagnostics/dify/dify_console_publish.py` | 36 `/opt/aiops-console-bridge/` |
| `ops/dify-console-bridge/*.js`（注入脚本） | 36 `/srv/aiops-console-assets/` |
| `ops/dify-console-bridge/dify-console-publish.conf` | 36 `snippets/dify-console-publish.conf`（`nginx -T` 里看到的是这个路径，不是 vhost 主文件） |
| `deploy/dify-36/web-patches/` | 重建 web 镜像（见下） |

## 怎么确认装没装

**别信"我改完了"。** 每一步都有可观察的判据：

```bash
# 桥代码：比对 md5，不看时间戳
md5sum /opt/aiops-console-bridge/aiops_diagnostics/dify/dify_console_publish.py
md5sum ~/Projects/AI-Ops/src/aiops_diagnostics/dify/dify_console_publish.py

# nginx：改完必须 nginx -T 里能看到，而且 reload 过
sudo nginx -T | grep -c "你的新路由"

# 桥进程：重启后看 journal 里有没有新路由的行为
sudo journalctl -u aiops-dify-console-bridge -f
```

## 一个具体的坑（2026-10-24）

新加 `/__aiops/knowledge-sync` 时，部署看起来"都做了"：

1. nginx 配置写进了仓库 ✅
2. 但 snippet 没装到 36 → 路由 404
3. 装上 snippet、reload → **还是 404**
4. 查桥日志：`POST /__aiops/knowledge-sync 404` —— **nginx 已经转发了，桥不认识这条路由**
5. 桥的代码目录里根本没有那次改动

第 4 步是关键：**nginx 的 404 与桥的 404 长得一样**，只有桥日志能区分。
"我部署了 nginx" 与 "桥会处理它" 是两件事。

## 回滚

桥：`/opt/aiops-console-bridge/aiops_diagnostics.bak-<时间戳>/`，整目录换回后重启。
nginx：`snippets/dify-console-publish.conf.bak-<时间戳>`，`nginx -t` 后 reload。
web 镜像：`DIFY_WEB_IMAGE=aiops/dify-web-patched:rollback-<日期>` + `--force-recreate`。
