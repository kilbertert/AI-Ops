# 工具授权装请求目标（#1189）

分支 `feat/grant-request-target`，基线 `018efbcd`。

## 已完成

授权载荷从「订单号 + 基址」换成「**这一次要发的请求** + 输入」：

```json
{"inputs": {"order_no": "..."},
 "request": {"method": "GET", "path": "/chOrderInfo/getOrderInfoDetail/ORDER-1", "query": ""},
 "contract": "request-target/1"}
```

- **网关侧**：`tool_request(tool, order)` 是路径物化的**唯一**位置；代理按
  `(method, path, query)` 三元组比对放行（原来是「前缀 + 订单号」）；证据字段集合从
  `{tool, order_no, source, observed_at, data}` 换成 `{tool, inputs, source, observed_at, data}`。
- **节点侧**：`CpmsReadTool` 不再按工具名拼 CPMS 路径，只执行 `grant.request()`；
  `Grant` 装 `inputs` + `Request(method, path, query)`；`Request` 有自己的形状校验
  （只允许 GET、绝对路径、长度上限、拒 `..` 与换行）。
- **混跑显式拒绝**：节点在领授权与上报证据时带 `X-AI-Ops-Tool-Contract: request-target/1`，
  载荷回带同一个值。旧节点 → 网关在**领授权**那步拒（`OPERATOR_TOOL_CONTRACT_OUTDATED`）；
  旧网关 → 节点拒（`authorization_contract_mismatch`）。

## 验证

### Python 侧

| 检查 | 结果 |
| --- | --- |
| `tests/operations/` | 257 passed, 2 skipped |
| 全量 `uv run pytest` | 见 PR |
| `ruff check` / `format --check` / `compileall` | 通过 |
| 镜像上下文摘要 | 已同步 |

新增判据：代理拒绝「路径对但查询串不同」与「完全另一条路径」；证据缺 `inputs`、多带
`order_no`、`inputs` 不一致、`source` 不一致四种分别拒绝；不声明契约的节点在领授权被拒
**且没有任何 CPMS 读取**（断言出站调用数为 0）。

### Java 侧（这一片唯一需要真编译的部分）

本机用锁定 JDK 镜像编译并运行节点侧验收：

```bash
M3NODE_JDK_IMAGE=eclipse-temurin@sha256:5d90b26c... \
  docker run --rm --network=none --cap-drop=ALL -v "$PWD:/repo:ro" … \
  java -cp '/out:/deps/lib/*' com.trendpower.aiops.node.CpmsToolAcceptance
```

结果：**`CPMS_TOOL_ACCEPTANCE PASS assertions=126 http_calls=12`**（改造前基线 114）。
新增七条断言：请求目标来自授权而不是本地拼装、带查询串的目标拼接、五种非法请求目标形状
分别拒绝、旧网关载荷被拒且错误码稳定、节点确实声明了契约版本。

**这条很重要：仓库 CI 不构建 Java**（`ci.yml` 只跑 ruff / pytest / compileall），
所以节点侧的正确性在本片之前完全依赖人工验收。本片把这一格的证据补上了。

## 缺口与边界

- **没有灰度窗口，这是设计选择。** 环境里任何一台没同步的节点在这段窗口内不可用。
  部署顺序与回滚点写进了 ADR-0175（不能沿用「先网关后节点」；回滚必须两端一起回）。
- **`NodeLinkAcceptance`（真平台派发那条）本次没有跑。** 它需要平台 jar 与更重的替身，
  本片用 `CpmsToolAcceptance` 覆盖了工具契约与两个混跑方向；整轮派发链路的回归留给
  部署验收窗口。
- **未完成业务验收**：没有真实 CPMS、没有真实账号、没有真实页面。
- 本片不动清单身份规则、不动工具参数放行判据、不动回答协议字段（票面「不做什么」）。

## 下一步

#1190（工具定义单一声明）：把工具名 → 描述 / 参数 / 请求模板 / 证据字段从五处收到一处，
让新增工具连 `tool_request()` 都不用改。
