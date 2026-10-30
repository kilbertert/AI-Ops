# 授权载荷变更的回归自检（#1189）

日期：2026-10-30。分支 `feat/grant-request-target`，基线 `018efbcd`。

本片是**破坏性的双端契约变更**，所以验收不能只看「同版能跑」：必须证明
(1) 新契约成立；(2) 两个混跑方向都被**显式**拒绝；(3) 节点侧真的编译并通过。

## 已执行的检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 管家面 | `uv run pytest tests/operations/` | 257 passed, 2 skipped |
| 全量 | `uv run pytest` | 见 PR |
| 静态/格式/语法 | `ruff check`、`ruff format --check`、`compileall` | 通过 |
| 镜像上下文摘要 | `deploy/m2-live-03/context_digest.py --check` | match |
| **Java 编译** | 锁定 JDK 镜像 + 锁定依赖目录，`javac` 全量源码 + 两个验收类 | 通过 |
| **Java 节点验收** | `java com.trendpower.aiops.node.CpmsToolAcceptance` | **PASS assertions=126** |

改造前的同一命令是 `assertions=114`。差值七条就是本片新增的判据，没有一条是「把旧断言
删了换成松的」。

## 两个混跑方向分别在哪证

| 方向 | 证在哪 | 断言 |
| --- | --- | --- |
| 旧节点 + 新网关 | Python（`test_a_node_that_does_not_declare_the_contract_is_refused_before_any_read`） | 403 + `OPERATOR_TOOL_CONTRACT_OUTDATED`，**且 CPMS 出站调用数为 0** |
| 新节点 + 旧网关 | Java（`mixedVersionIsRefusedInBothDirections`） | `authorization_contract_mismatch`，且节点确实发出了 `X-AI-Ops-Tool-Contract` |

**这两条不能互相替代**：Python 侧的替身说不了 Java 的话，Java 侧的替身也证不了网关的拒绝位置。

## 为什么「拒绝发生在领授权」这件事单独算一条判据

旧节点如果能把授权领走，公司令牌就已经用过了，拒绝会拖到完成回调——那时错误形状是
「证据缺字段」，既不是稳定码，也已经产生了一次对外请求。把拒绝前移，才有可观测的边界。

## 一台机器上跑 Java 验收的方法（本片补的一格）

仓库 CI **不构建 Java**，`ci.yml` 只有 ruff / pytest / compileall。所以节点侧的正确性
在本片之前完全依赖人工验收。本机可复现的路径：

```bash
mkdir -p ~/.cache/m3node-deps/lib
find ~/.m2/repository -name '*.jar' ! -name '*-sources.jar' ! -name '*-javadoc.jar' \
  -exec cp -n {} ~/.cache/m3node-deps/lib/ \;
# 编译 + 运行见 docs/开发进度/2026-10-30-grant-request-target.md
```

镜像是 `eclipse-temurin@sha256:5d90b26c…`（本机已有）。依赖目录是**平铺 jar**，
不是 maven 布局——`verify_*` 脚本要求 `$M3NODE_DEPS/lib`。

## 未完成业务验收

- 没有真实 CPMS、没有真实账号、没有真实页面。
- `NodeLinkAcceptance`（真平台派发整轮）本次**未跑**，需要平台 jar 与更重的替身；
  整轮链路的回归留给部署验收窗口。
- 因此不能引用本记录说明「节点已升级」——节点 JAR 尚未构建入私有记录、尚未部署。
- **没有灰度窗口**：任何一台没同步的节点在部署窗口内不可用，这是设计选择。
