# 站点代理复审修复（2026-11-02）

#1302 的复审确认：权限内站点仍可能不匹配关键词。双端代理共用既有关键词过滤，
证据与节点响应同时过滤，保留上游总数与截断提示。第一页/50条来自工具声明，
不将它描述成完整扫描，也不扩展本轮分页协议。

反例先得到6处失败，修复后36项代理检查通过；真实Java→Gateway→合成CPMS
共12场景通过，包含两端不匹配行。Ruff通过。首次全量3860通过/9跳过/1失败：旧合成站点地址“广州市天河区”
不包含测试请求的连续过滤词“广州天河”。改用确实匹配的合成地址保留整链正例；
不匹配反例单独覆盖且保留旧失败日志。

另一会话最后提交的ADR判据已核对并纳入既有ADR-0192，不新增编号冲突的重复记录。

## 第二条反例：11 条突破展示上限（已修）

复审方在干净树（`git archive origin/fix/site-tool-no-order-identity`）复现：关键词过滤已生效，
但 `operator_conversation.render` 直接 `enumerate(sites["sites"])`，权限内 11 条全命中时
正文列出 11 行，而 `_roster_count` 说「共 1 个」——两个数字自相矛盾。工具证据路径
（`_sites_from_evidence`）返回整页，同样不截断。

修法是**一个截断点**：`operator_conversation.shown_sites`（`SITE_MAX_SHOWN` 的唯一定义也
搬到这里，`operator_cpms` 改为别名，避免包内导入环被判为新增循环边）。`render` 与
`site_facts`（`{{fact:site_roster}}`、`site_count`）同源取它；超出的行数以「另有 N 个
匹配站点未在此列出」给出。

**没有把展示截断并进 `truncated`**：Java 节点按 `total > rows.size()` 逐字重算该字段与
Gateway 比对，改口径会让每次真实运行以 `OPERATOR_EVIDENCE_MISMATCH` 失败。一条 11 行的
完整页仍 `truncated=false`——「查全了、只列 10 条」与「没查全」是两件事。

## 本轮实测

| 项 | 命令 | 结果 |
|---|---|---|
| Python 全量 | `uv run pytest` | **3863 passed, 9 skipped** |
| 真实 Java→Gateway→合成 CPMS | `verify_site_proxy.py` | **12/12 场景通过**（两端、关键词、不匹配、空白、全租户、空范围、错租户） |
| Java 工具验收 | `CpmsToolAcceptance` | **PASS assertions=214（基线 213）**，新增站点证据页字段整链一条 |
| 真 nginx 回调白名单 | `tests/test_node_runtime_deploy.py` | 新增 `/cpms/chSite/pageBOGUS`、`/cpms/chSite/page/ORDER-1` 两条反例；被拒后进程仍在 |
| Ruff | `ruff check` / `format --check` | 通过 |
| 网关镜像锁 | `context_digest.digest()` | `8240bbed…`，已按 `build-image.sh --update-lock` 的算法写入 `images.lock.json` |

`m3node/run.sh`（NodeGuardAcceptance 与 ToolSchemaParity）在本机锁定依赖目录上因
`slf4j-api-1.7.36` 与 `2.0.18` 同时在 `lib/`、Logback 报「not a Logback LoggerContext」
而起不来；同一份 HEAD 的干净树（6ef58dc2）逐字复现，属既有环境问题，与本片无关。
Java 侧改跑 `CpmsToolAcceptance` 直接编译执行，结果如上。

**未完成业务验收**：部署及原入口（H5 站点正例、空结果、受限范围）仍在后续步骤；
线上热修 jar 不等于本次源码，须以容器实际挂载文件核验。
