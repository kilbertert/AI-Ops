# 111. 外部播放链接是一个独立字段，不是 `url` 的另一种值

日期：2026-10-23
状态：Accepted
影响版本：`src/aiops_diagnostics/knowledge_retrieval.py`（`MediaResource` / `issue` /
`normalize_search_response`）、`src/aiops_diagnostics/qa_rag.py`（检索提示词）、
`docs/agents/frontend-api-brief.md`（渲染规则）、`deploy/m2-live-03/images.lock.json`

## 背景

业务素材里的"视频"有两种，而契约只表达得了一种：

| 类型 | 例子 | 我们手里有什么 |
|---|---|---|
| 我们托管的 mp4 | `新加坡无人电动巴士.mp4`（RAGFlow 文档） | 字节，走 `/v1/media` |
| 别人平台上的视频 | TRENDPOWER 表里 19 条 YouTube 链接 | 只有一段链接，**没有字节** |

第二种在 #1035 之前**完全到不了前端**，原因不是没取到，而是**取到了也没地方放**：
链接在 chunk 正文里躺着，而 `blocks-v1` 的 `video` 块只认 `media.url`，那个字段的语义是
"我们的媒体代理能取回这段字节"。

顺带一个反直觉的事实：**含链接的那个 chunk 是纯文本分段**（`doc_type_kwd=[]`），
不是视频文档。所以它连"视频块"都不会被创建 —— 旧的判据是
`doc_type == "video" or _is_video_name(title)`。

## 决策

**新增两个字段，`url` 的语义一个字不改。**

```jsonc
"media": {
  "url": "/v1/media/media_xxx.sig",      // 有我们自己的字节时才有
  "external_url": "https://youtu.be/…",   // 本轮命中的 chunk 正文里的链接
  "embed": "youtube"                      // 前端据此选播放器
}
```

三条约束：

1. **`external_url` 的生产条件与 `resource_id` 完全一致** —— 必须来自**本轮检索命中的
   那个 chunk**。模型不能提供、不能改写、不能从一个块搬到另一个块。
2. **域名白名单**（`EXTERNAL_VIDEO_HOSTS`），且比对的是**解析后的 hostname**，不是
   URL 前缀。`https://youtube.com.evil.example/…` 必须被拒 —— 前缀匹配会放行它，
   而"我们替用户担保了这个地址"正是这个字段的全部含义。
3. **`embed` 由服务端从 host 推出**，是枚举不是自由文本。前端不该解析 URL 决定用哪种
   播放器：那会把这套判断复制到每个客户端。

## 两个必须解释的细节

### 外部链接的块**没有 `url` 字段**

纯文本 chunk 引用了外部视频时，我们手上没有字节。给它签一个 `url` 会让前端拿到
"Markdown 文件的字节、标着 `video/mp4`" —— 一个 200 响应的空播放器，比没有更糟。
所以 `url` 在"没有我们自己的字节"时**缺席**，而不是空串。

这也意味着**前端必须按 `external_url` 优先分支**；文档里已写明，并点名
`media.url ?? ''` 这种写法会得到 `undefined` 源。

### 文本 chunk 也能产出 `video` 块

这是本决策里唯一"放宽"的地方：一个 `doc_type` 为空的文本分段，只要正文里有白名单域的
链接，就会签发一个 `video` 资源。理由是**实际素材就是这个形状**（一张"标题 + 链接"的表），
不放宽等于这个功能对真实数据永远不生效。

代价写明：这个块的 `mime_type` 是 `video/mp4`，但它描述的不是我们的字节。

## 被否掉的替代形态

- **把外部链接塞进 `media.url`**。最省事，代价是三件事一起坏：签名失去意义（
  `/v1/media/{id}.{sig}` 的前提是"这个 id 指向我们的字节"）、`<video src>` 拿到 HTML
  页面、以及**任何人都能让服务端替它签发一条外部跳转**。
- **让前端解析 chunk 正文自己找链接**。等于把白名单逻辑复制到每个客户端；一个客户端
  放宽了就绕过全部约束，而服务端仍然"看起来是安全的"。
- **抓取 YouTube 转存成我们托管的 mp4**。版权、存储、转码都不在这一片；而且它把
  "引用"变成"复制"，事实来源会分叉。
- **只放行域名、不看是不是本轮命中的 chunk**。那样模型只要在正文里写一句
  "参见 https://youtu.be/xxx" 就能让我们替它签发。判据必须锚在检索结果上。

## 后果

更容易：运营把视频链接写进知识库文档（表格、FAQ 都行），回答里就能带可播放的入口，
不需要为每条视频单独建一个 mp4 文档。

更难：`external_url` 只在我们自己的 `blocks-v1` 上生效。**Dify 那条路永远拿不到** ——
Dify 的外部知识库接口只取 `page_content` 与 `metadata.score`
（`dataset_retrieval.py:518`），`metadata.media` 根本不进它的上下文。这一点写进了
`docs/agents/knowledge-media-playback.md`，免得下次又按"后端没返回"去查。
