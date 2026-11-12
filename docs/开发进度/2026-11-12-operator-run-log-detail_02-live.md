# 2026-11-12：管家运行日志明细已上线（41 + 36）

分支 `docs/operator-run-log-detail-live2`，基线 `520c32a7`，PR #1487。
本记录是 #1486（实现）之后的部署里程碑，承接
[实现进度](2026-11-12-operator-run-log-detail.md)。

- **41 Gateway**：合并 `520c32a7` 经标准 CD `43183203581` 部署，
  `/health` = `1.9.0+520c32a710dd`，服务 active。
- **36 控制台 web**：开发机构建 patched 镜像 → `docker cp` 抽 `/app/targets/next`
  成品树 → 59MB tar（sha256 逐字一致）→ 36 上游 base 单 COPY 组装
  `run-log-detail-520c32a7` → `.env` 备份后 `DIFY_WEB_IMAGE` 切换 +
  `--force-recreate web`。运行中容器 bundle 命中 `终态原因`，`/signin` 200，
  web 日志 0 error。回滚：`.env` 改回 `operator-evidence-bb51c98c` → force-recreate。
- **Devin Review 闭环**：#1486 四轮——1 个真缺陷（language 未落 run 记录）已修
  并带真实入口回归测试，3 个文档项（备选方案节/进度标识/验收结论措辞）已补；
  #1487 三条（缺进度记录、交付索引旧版本、成品树手顺未入册）已修。
- **未完成业务验收**：真实管家问答在新日志页呈现问题/订单号/节点明细，
  待一次真实运行后另行验收；开发机到 `dify.ranlei.work:443` TLS 间歇被重置，
  页面验收仍走取证通道。
- 证据见[上线回执](../validation/2026-11-12-operator-run-log-detail-live.md)。
