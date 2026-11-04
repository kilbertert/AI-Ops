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

## 验收结论

**本文写下的当时**只证明部署通过：制品在位、字段在响应形状里、镜像内容含新文案、页面可达。
业务验收（真实失败下页面到底显示什么）由后续的**受控失败**补上并通过 —— 见
[受控失败的业务验收](2026-11-04-setup-failure-controlled-acceptance.md)。

下面保留当时的缺口原文作为历史，并列当前状态：

| 当时的缺口 | 当前状态 |
| --- | --- |
| 页面渲染未用人眼确认 | **已确认**：截图与页面文字见[受控失败记录](2026-11-04-setup-failure-controlled-acceptance.md#页面截图) |
| 没有任何一次真实失败跑过新文案 | **已补**：operator-only 作业的受控失败 |
| 同源桥的字段透传未实测 | **已实测**：受控失败经同源桥读到 `failed_step`/`failure_kind` |

## 当时仍未验 / 已知问题

1. **页面渲染未用人眼确认**：开发机到 `dify.ranlei.work:443` 的 TLS 握手被重置
   （5 次重试全 000，而 36 本机 `--resolve` 稳定 200）。该间歇性重置在本会话更早时
   也出现过，不是本次部署引入。因此只验到"镜像里有新文案 + 页面可达"，没有截图。
2. **没有任何一次真实失败跑过新文案**：要看到「「某步」出错（Kind）」必须有一次真实失败；
   当时没有制造 —— 这就是那句「未完成业务验收」的由来（后来用受控失败补上了）。
3. **同源桥的字段透传未实测**：从开发机调 `/__aiops/manage` 同受第 1 条影响；改用 41 上
   以服务身份调 `public_job` 回读，那只证明**公开形状**含新字段，**不覆盖**桥转发与
   登录后页面读取。网络恢复后需补一条真实桥调用。
