# 视频在回答里怎么才能真的播出来

> 2026-10-23。写这份是因为存在一个常见的默认假设 ——「知识库里存了视频链接，回答里
> 就会带上视频」—— 它在**两条链路里都不成立**，而不成立的方式还不一样。

## 一句话

**回答里能不能出现播放器，取决于视频字节由谁托管，不取决于知识库里有没有链接。**

## 两条链路，两种能力

### 1. 我们自己的 blocks-v1（`/v1/agents` + `/v1/media`）—— 能播

播放器要的是 `blocks[]` 里一个 `{"kind": "video", "resource_id": "media_…"}` 块，
前端把它当 `<video src={media.url}>`。这条链满足三个条件才出块：

| 条件 | 判据 | 不满足时 |
|---|---|---|
| 库里那份文档是**视频文件**（`.mp4`/`.webm`，或 `doc_type_kwd=video`） | `knowledge_retrieval.py` 的 `_is_video_name` / `doc_type_kwd` | 不签发 video 授权 |
| 检索命中它，且签发时落在当轮 `media_by_id` 里 | `qa_rag.py` | 模型就算引用了也**被静默丢掉** |
| 模型在 `answer` 回合真的引用了那个 `resource_id` | 模型输出 | 只有文字 |

**已经实测跑通**（2026-10-23，公网入口）：

```
检索「新加坡无人电动巴士」→ record 带 media[0]，kind=video / video/mp4
  整段 GET      → 200，82,348,365 字节，ISO Media MP4
  Range 0-1023  → 206，1024 字节
  伪造签名      → 403
```

### 2. Dify 的外部知识库 API —— 不能播

这一条今天在 Dify 1.17.1 的源码里核实过，**不是我们的实现缺什么**：

```python
# api/core/rag/retrieval/dataset_retrieval.py:518
for item in external_documents:
    document_context_list.append(DocumentContext(content=item.page_content, score=item.metadata.get("score")))
```

我们的 `/v1/dify/retrieval` 返回的记录里带着 `metadata.media[]`（含签名 URL、kind、
mime）。Dify **只取 `content` 和 `score`**，`metadata` 的其余部分只用来做来源标注
（`document_name` 等），**不进模型上下文，也不进对话界面**。

所以走 Dify 时：回答里最多出现一段**文字形式的链接**（模型从 `content` 里读到的），
不会出现播放器，而且用户能不能点取决于前端是否把 URL 渲染成链接。

## 那个 xlsx 里的 19 个 YouTube 链接

`TRENDPOWER_EV_Charging_Platform_Video_Modules.xlsx` 里的链接是 `youtube.com/watch?v=…`，
**不是媒体文件**。它们能落到的最高形态是：模型在回答正文里念出这条 URL。

要变成真播放器，只有两条路：

1. **把视频文件本身放进 RAGFlow**（`chunk_method` 用 `picture`，走我们自己的 blocks-v1）。
   YouTube 的托管内容这样做有版权与来源问题，需要业务决定。
2. **改 Dify 侧的前端**（像 #66 改"输入框 → 选择器"那样打 web patch），让它渲染我们
   `metadata.media`。代价是每次升级 Dify 都要跟一次补丁，且这条能力上游没有。

## `用户端(1).docx` 里那 28 张图：今天是拿不到的

同一个原因的另一面：docx 里嵌了 28 张图（4.4 MB），但 `naive` 解析**不抽图**，
20 个分段全部 `image_id` 为空 ⇒ 不会签发任何 image 授权。

要让这些图能被引用，得把 docx 按 `picture` 方法解析（或先把图抽出来单独上传）——
那会把 28 个 Q/A 打散成图片描述，**问答正文反而检索不到**。两者不可兼得，除非分成
两个库：文字一份（`naive`）、图一份（`picture`）。

## 一次实证：截图里的 `youtu.be` 不是我们发的（2026-10-23 追查）

现象：一张 `qa` 响应截图里 `media.url` 是
`https://youtu.be/1Lk9eNUuMIV?si=301_61EH…` —— 而我们代码只会拼 `/v1/media/media_….sig`。

**先说结论，免得下次再按"后端返回的"去查**：那是**展示用的示意图**，不是真实
响应，也不是前端在替换。真实响应里这一格是 `/v1/media/…`。

追查过程仍然值得留档，因为它证明了一件事：**这条链里没有任何环节会把视频换成外部
链接** —— 想要外部链接，只能显式加一个字段（见文末）。

| 查的地方 | 结果 |
|---|---|
| 41 生产库**全部 24 张表**扫 `youtu` | 只命中 1 行（2026-09-12，正文里提到 "YouTube channels"，**不是 URL**） |
| 同一条 `qa_id` 的 `result_json` | `media.url = /v1/media/media_tgHN3r9….ba0f0d59…` —— **与现行代码逐字一致** |
| 41 / 36 / 本机 / 全部 worktree / git 全历史 | 没有 `1Lk9eNUuMIV`，也没有任何 `youtu` |
| RAGFlow 库内容、MinIO、`/tmp`、`/var/backups`、shell history | 没有 |

**教训**：截图只能证明"有这么个想法"，不能证明"线上是这个形状"。这条链的判据是
数据库里的 `result_json`，不是界面上的那一格。

## 要让外部链接真的能播，只能新增一个字段

`media.url` 的语义是"我们托管的字节"，签名、Range、403 都是按这个语义设计的。
把外部链接塞进 `url` 会同时破坏三件事：签名失去意义、前端 `<video src>` 拿到一个
HTML 页面、以及**任何人都能让服务端替它签发一条外部跳转**。

所以如果需要外部视频（YouTube 等），加**独立字段**，来源受控：

```jsonc
"media": {
  "url": "/v1/media/media_xxx.sig",      // 不变，我们托管的字节
  "external_url": "https://youtu.be/…",  // 新增：只来自命中的 chunk 正文里的白名单域链接
  "embed": "youtube"                     // 新增：前端据此走 iframe，而不是 <video src>
}
```

判据：`external_url` 的产生条件与 `resource_id` **完全一致** —— 必须来自**本轮检索
命中的那个 chunk**。chunk 里没有就不出这个字段，前端回落到 `url`。模型仍然不能
凭空造 URL，`qa_rag.py` 里"引用不在 `media_by_id` 里就丢弃"那条判据原样保留。

## 判据（下次改这条链时照这个验）

```bash
# 1. 检索确实返回了 media，且 kind 是 video
curl -s -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  --data '{"query":"<视频标题里的词>","knowledge_id":"kb-canary-2","retrieval_setting":{"top_k":5}}' \
  https://api.mall.qushiyun.com/v1/dify/retrieval | jq '.records[].metadata.media'

# 2. 整段能取回、Range 是 206、伪造签名是 403
```

**别只看第 1 步。** 签发成功 ≠ 取得回来 —— ADR-0077 记过一次：检索 200，媒体 100% 403，
原因是签发时写了一个不存在的 `agent_version`。
