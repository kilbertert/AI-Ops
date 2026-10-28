# 知识库图文链路验证

日期：2026-10-28（北京时间）。代码基线 `3fc5301c`，分支 `fix/ragflow-ingestion-root-cause`。
**未完成业务验收**；本文证明工程故障，不替代业务内容准确性审核。

## 直接证据与更正

- 真实默认模型解析：34 个目标租户中 1 成功、33 失败。目标租户先被模型归属校验拒绝，
  按名字回退后仍因缺少本租户的视觉模型失败；不是 UUID 版本或进程缓存问题。
- 原 Word 任务最终 `progress=1/run=3`，144 切片；完整分页含 20 个 image_id。
  0.88 之后还有 92 秒索引写入，未中断。全部显示 text 不代表图片不存在。
- `image_context_size` 只加图片邻近文字；视觉解析函数仍会尝试模型绑定，失败被静默吞掉。
- 副本五处 rId4 指向不存在的 `../NULL`。现存 20 图与较早原件逐字节一致，
  五处缺失按相邻匹配图顺序唯一恢复；另存副本 ZIP 校验通过、25 图片引用有效、正文不变。
- kb-service 当前开户使用 Tongyi-Qianwen/maas 注册 baoyun 的 embedding 与旧视觉模型；
  create_kb 及 HTTP 请求模型仍默认旧 embedding。修复覆盖这三个调用点。
- Gateway 对带 image_id、无 MIME 的 Word 片段输出空 media；先写回归，观察
  `assert len(chunks[0].media) == 1` 失败，修复后签名、回源及实际 MIME 断言通过。

## 验证记录

| 项目 | 结果 |
|---|---|
| 真实 RAGFlow 默认模型检查（只读） | 33/34 失败，符合现场故障 |
| 真实 baoyun gpt-4o-mini / RAGFlow LLMBundle | 红色测试图被识别为红色 |
| knowledge_retrieval + consumer_live_context | 25 passed |
| 默认开户调用契约 | 原部署代码失败；补丁后通过，错误实例被排除、缺模型拒绝 |
| ruff check / format | 951 文件通过 |
| wheel / sdist | 构建成功，制品私有缓存，不发布 |
| Gateway 真进程 | 健康 200、无身份 401、注册 201、授权运行 200、设备访问管理面 401；完成并回收进程 |
| 真实 144 条切片离线回放 | 原版图片被过滤；修复后 20 个 image_id 全部投影为媒体 |
| CI、部署、现有租户 API 修复、重解析 | 待验证 |

私有证据位于操作人的 `~/.cache/aiops-kb-root-cause/`，含下载原文、分页切片、源码摘要与
恢复对照，不进仓库。没有记录密钥。复跑方法见 [部署手册](../../deploy/kb-service/README.md)，
场景与对应步骤见 [验收计划](../agents/kb-ingestion-recovery/qa-plan.md)。

全量首跑发现 3 项检查失败：2 项要求使用本工作树 `.venv`（已按要求重建环境），
1 项要求同步 Gateway 构造上下文摘要（已用仓库 context_digest 生成，不改已构建镜像身份）。
使用工作树 .venv 重跑：4190 passed / 9 skipped / 149.72 秒；跳过项为既有外部依赖条件。

风险测试采用真实故障回放和权限/媒体拒绝回归；本次不改变授权、金额或事务规则，未运行变异测试。
Gateway 本地技能只证明启动与鉴权，不能替代远端知识库产出和生产问答。
