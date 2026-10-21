# 66. 知识库的「选中」发生在 Dify 自己的对话框里

日期：2026-10-21
状态：已采纳
影响版本：`operations/dify-exposure-registry.md`、`deploy/dify-36/patches/1.17.1/06–07`、
`deploy/dify-36/web-patches/`、`src/aiops_diagnostics/{dify_knowledge_api,gateway_api,gateway_config}.py`

## 背景：上一版是对的，但答错了问题

ADR 0063 把"配置知识库"从手写 env 变成我们自己的一个页面（`/v1/knowledge`）。它能用，
但它**又多造了一个运营要记住的地方**：配置在 AI-Ops 的页面里做，Dify 里还要再手打一遍
`knowledge_id`。需求原话是「**直接在 Dify 中完成知识库配置**」—— 我们的页面满足了"点选"，
没有满足"在 Dify 里"。

真正卡住的只有一处：Dify 的「连接外部知识库」对话框把 `Knowledge ID` 渲染成**裸输入框**，
运营必须已经知道那个不透明字符串。这一处补上，"在 Dify 里点完"就成立。

## 决策一：给端点加一个只读的清单路由，让对话框能变成选择器

`GET {endpoint}/knowledge` → `{"knowledge_items": [{knowledge_id, name, knowledge_base_ids}]}`。

- **列的是我们的登记表，不是 RAGFlow 的 dataset**。`knowledge_id` 是**我们**的键（Dify 回传
  的那个值），只有登记过的才检索得到。列 dataset 会给出检索一律 404 的选项 —— 一个看起来
  更丰富、实际更难用的选择器。
- **同一把凭据、同一张表**（`AIOPS_GATEWAY_DIFY_KNOWLEDGE_API_KEY`）：清单里出现的每一个，
  检索都接受。这是"选择器不能撒谎"的唯一实现方式。
- **调试身份只看到自己的子集**。预览界面不得展示预览用不了的选项 —— 判据与检索侧同一条。
- **它是我们这一侧的扩展**，不是 External Knowledge API 标准的一部分。任何**别的** provider
  没有这个路由是正常情况，Dify 侧必须退回自由输入（见决策三）。

## 决策二：改 Dify 的前端，但要按上游的方式改

Dify 的 web 镜像是**预构建产物**（`/app/targets/next/` 里只有编译结果，没有源码），所以
改控制台界面只有一条路：按固定 tag 取源码 → 打补丁 → **重新构建镜像**。

沿用仓库已有的纪律（`patches/apply.sh`）：**版本钉死 + 逐文件 sha256 核对**，tag 一换就
拒绝执行，而不是把陈旧的补丁树烧进镜像。新增两处：

- `patches/1.17.1/06–07`：api 侧两个文件（服务方法 + 控制台路由），照旧用**文件只读挂载**
  生效，不重建 api 镜像。
- `web-patches/1.17.1/` + `web-patches/build-web-image.sh`：web 侧一个组件 + 三处改动，
  必须重建镜像。

**改的只有"输入框 → 选择器"这一处**，其余全部保留上游行为：没有清单（provider 不支持、或
还没登记任何东西）时，原样渲染上游的 `Input`。用 React.memo 包住，与上游同类组件一致。

## 决策三：构建必须用 webpack，不能用 Turbopack

上游 `Dockerfile` 的构建行是 `pnpm build`（Turbopack）。实测在本机（15 GiB RAM + 20 GiB swap）
三次尝试全部失败：`137`（OOM）、`124`×2（90 分钟零产物）。同一棵树换成 `--webpack`
**3.8 分钟编译成功**，产物完整（40/40 静态页）。

所以 `build-web-image.sh` 在构建前把那一行改成 `pnpm build --webpack`，并去掉
`pnpm build:vinext`（本实例 `EXPERIMENTAL_ENABLE_VINEXT=false`，用不到，且会再跑一遍构建）。
脚本对找不到预期那行的情况**直接失败**，不去猜。

`web/next.config.ts` 也要固定 `turbopack.root`：不固定时 Turbopack 会向上找到一个无关的
`pnpm-workspace.yaml`，报"outside the current Git repository"并继续扫描 —— 这正是那 90 分钟
零产物的直接原因之一。

## 被否掉的替代形态

- **继续用我们自己的页面（ADR 0063）。** 它已经能用，但运营仍要在两个系统之间搬运一个
  不透明字符串；需求说的是"在 Dify 里"。

- **让运营自己填 `knowledge_id`，不改前端。** 这正是现状，正是它让配置成了工程师的活。

- **在 Dify 里加一个"知识库管理"页面。** 那是把我们的管理面搬进别人的产品，改动面大得多，
  而且需要在 Dify 侧再实现一遍鉴权与租户。

- **用 Dify 的 iframe / 插件机制挂我们的页面。** Dify 1.17.1 控制台没有为这种嵌入提供
  受支持的接缝；能做的只有改源码。

- **把清单塞进现有的 `/retrieval`（例如 query 为空时返回列表）。** 会把一个纯检索接口
  变成双语义接口，而 Dify 的 `check_endpoint_and_api_key` 恰恰会发一个 `query: ""` 的探测
  请求 —— 两种语义会在那一次探测里打架。

## 后果

更容易：运营在 Dify 的「连接外部知识库」里**选**一个源，而不是背一个 id。我们那个页面
（ADR 0063）保留，两个入口读同一张表，不会互相矛盾。

更难：**web 镜像是我们自己构建的**，上游每次升级都要重做补丁并重建（脚本会以 sha 不符
挡在前面）。这是"改别人产品界面"的固有代价，写在这里以免下次被当成意外。

上限：只改了这一个对话框。知识库的**内容**（文档、解析、embedding）仍在 RAGFlow，Dify 侧
不做任何托管；跨租户 / 无主库仍然要工程师（ADR 0063 的边界不变）。
