# 失败定位上线：41 后端 + 36 控制台 web 镜像

执行时间：2026-11-04 02:40–03:05（北京时间）。
代码：PR #1331 合并 `5bbf7e0d`。

## 41（Gateway）

| 项 | 值 |
| --- | --- |
| CD run | `5bbf7e0d` success（前一轮因自托管 runner 被无关 `Agent Update Branch` 占用而排队约 40 分钟） |
| `/health` | `1.0.17+5bbf7e0d54e8`，`business_mutations: disabled` |
| 部署源码 | `self_service_store.py` 的 `_ENTRY_FIELDS` 含 `failed_step`/`failure_kind`，`public_job` 输出两者 |
| 以服务身份回读现役作业 | `public_job` 顶层含 `failed_step`/`failure_kind`；优链充 consumer `ready/published v1`，operator `draft` |

**未造新的真实失败**：新字段的归因链由确定性用例（逐端失败、作业级失败、重试不继承旧位置、
客户端来源步骤）和一次性数据根的公开形状检查覆盖；生产上只确认字段真的在响应形状里。

## 36（Dify 控制台 web）

| 项 | 值 |
| --- | --- |
| 开发机构建 | `aiops/dify-web-patched:setup-failure-visibility`；`.next` 产物含 `核对客户端来源` 与 `出错（` |
| 传输 | tar 288 MB，sha256 `c3b70e4e333b5fedb9b08d81dae6977271961abfe2982624cc03a1c26a039821`，36 上逐字一致 |
| 36 组装 | 上游 `docker.1panel.live/langgenius/dify-web:1.17.1` 做 base，三条 COPY 与 README 一致；镜像内 grep 命中新文案 |
| 上线 | `.env` 备份 `.env.bak-web-sfv-20261103-*`；`DIFY_WEB_IMAGE=aiops/dify-web-patched:setup-failure-visibility`；`docker compose up -d --force-recreate web` |
| 运行中的容器 | `grep` 命中 `核对客户端来源` 与 `出错（`（说明镜像而非宿主缓存） |
| 页面 | 36 本机 `--resolve` 访问 `/signin` 200、`/apps` 307（未登录跳转）、web 日志 0 error |
| 回滚 | `.env` 改回 `trial-errors-07a113ec` → `up -d --force-recreate web` |

## 仍未验 / 已知问题

1. **页面渲染未用人眼确认**：开发机到 `dify.ranlei.work:443` 的 TLS 握手被重置
   （5 次重试全 000，而 36 本机 `--resolve` 稳定 200）。该间歇性重置在本会话更早时
   也出现过，不是本次部署引入。因此只验到"镜像里有新文案 + 页面可达"，没有截图。
2. **没有任何一次真实失败跑过新文案**：要看到「「某步」出错（Kind）」必须有一次真实失败；
   本轮没有制造。
3. 从开发机调同源桥 `/__aiops/manage` 同受第 1 条影响，改用 41 上以服务身份调
   `public_job` 回读，等价于接口形状。
