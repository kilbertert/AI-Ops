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
| Python 全量 | `uv run pytest` | **3868 passed, 9 skipped** |
| 真实 Java→Gateway→合成 CPMS | `verify_site_proxy.py` | **12/12 场景通过**（两端、关键词、不匹配、空白、全租户、空范围、错租户） |
| Java 工具验收 | `CpmsToolAcceptance` | **PASS assertions=214（基线 213）**，新增站点证据页字段整链一条 |
| 真 nginx 回调白名单 | `tests/test_node_runtime_deploy.py` | 新增 `/cpms/chSite/pageBOGUS`、`/cpms/chSite/page/ORDER-1` 两条反例；被拒后进程仍在 |
| Ruff | `ruff check` / `format --check` | 通过 |
| 网关镜像锁 | `context_digest.py --check` | `c7d4a8a5…`，match: true（见下条） |

## 镜像锁的一次假记录（已修根因）

`0e912758` 里写进锁的 `8240bbed…` 与这棵树对不上：CI 在干净树上算 `68b229e2…`。
根因不是那次内容改动，而是**本机 `tools/experiments/m2_close/m3node/build/`**（`run.sh`
的编译产物，gitignored、每次运行都变）被算进了构造上下文摘要。于是摘要只代表
「某台机器跑过 run.sh 之后的状态」，CI 永远对不上——与 `LC_ALL=C sort` 那条同一类错误。

修的是排除规则本身，不是那个数字：`context_digest.py` 的 `CONTEXT_SPEC` 与
`build-image.sh` 的 `tar --exclude` 同时加上 `m3node/build`。修完后本机（有 build/）
与 `git archive` 干净导出算出同一个值，这才说明摘要与工作树状态无关。

## 说明片段与正文头一句必须是同一个数（复审新增）

`site_facts` 的 `site_count` 原先是「展示条数」，`render` 头一句用 `total`，于是
「共 10 个站点」紧跟着「共 500 个站点」。现在两者共用 `_count_is_total` /
`_stated_count` 一个判断：

- 搜索问法：`total` 是上游**过滤前**计数，只能说本地握有的匹配行；差额用
  「另有 N 个匹配站点未在此列出」。
- 列举问法且读全了：`total` 才是「你有多少个」，差额用「另有 N 个站点」补齐。
- 列举问法但只读了一页（`limited` 且未超出展示上限）：只说展示条数，加上既有的
  「结果可能不完整」。

「另有 N 个」不再在 `limited` 时补数字：那是「可能更多」，不是「已知还有 N 个」。


`m3node/run.sh`（NodeGuardAcceptance 与 ToolSchemaParity）在本机锁定依赖目录上因
`slf4j-api-1.7.36` 与 `2.0.18` 同时在 `lib/`、Logback 报「not a Logback LoggerContext」
而起不来；同一份 HEAD 的干净树（6ef58dc2）逐字复现，属既有环境问题，与本片无关。
Java 侧改跑 `CpmsToolAcceptance` 直接编译执行，结果如上。

**未完成业务验收**：部署及原入口（H5 站点正例、空结果、受限范围）仍在后续步骤；
线上热修 jar 不等于本次源码，须以容器实际挂载文件核验。
