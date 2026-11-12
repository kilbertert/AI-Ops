# 管家运行日志明细上线：41 Gateway + 36 控制台 web 镜像

执行时间：2026-11-12。代码：PR #1486 合并 `520c32a7`。

## 41（Gateway）

| 项 | 值 |
| --- | --- |
| CD run | `43183203581` deploy-41 success（约 1m43s） |
| `/health` | `1.9.0+520c32a710dd`，`business_mutations: disabled`，服务 `active` |
| 生效面 | `language` 现按本轮请求落 run 记录（此前恒 NULL）；投影扩入问题/订单号/执行明细字段 |

## 36（Dify 控制台 web）

| 项 | 值 |
| --- | --- |
| 开发机构建 | `aiops/dify-web-patched:run-log-detail-f0967941`；`.next` 产物含 `终态原因`/`结果指纹` |
| 传输 | tar 59 MB，sha256 `5a3a67aaa03049a582f48a0c9dc0b946779190855cb282700ec4fc2afe8c2ffe`，36 上逐字一致 |
| 36 组装 | 上游 `docker.1panel.live/langgenius/dify-web:1.17.1` 做 base；与 README 三条 COPY 产物逐字一致的成品树单 COPY 还原到 `/app/targets/next`（tar 内是从已构建镜像原样抽出的整树，已含 web/.next/static 与 web/public） |
| 上线 | `.env` 备份 `.env.bak-web-runlog-20261112162929`；`DIFY_WEB_IMAGE=aiops/dify-web-patched:run-log-detail-520c32a7`；`docker compose up -d --force-recreate web` |
| 运行中的容器 | `deploy-web-1` 内 grep 命中 `终态原因`（镜像内容非宿主缓存）；web 日志 0 error，Next Ready |
| 页面 | 36 本机 `--resolve` 访问 `/signin` 200 |
| 回滚 | `.env` 改回 `operator-evidence-bb51c98c` → `up -d --force-recreate web` |

## 验收结论

**本文只证明部署通过**：制品在位、运行中的容器内 bundle 含新字段文案、页面可达、
网关版本与合并提交一致。**未完成业务验收**：真实管家问答在新日志页呈现问题/
订单号/节点明细/逐事件拒绝码，需一次真实运行后另行验收（开发机到
`dify.ranlei.work:443` 的 TLS 握手仍间歇被重置，页面验收要走取证通道）。
