# 120. 外部视频地址就写在 `media.url` 里：前端已经这么做了

日期：2026-10-23
状态：Accepted
影响版本：`src/aiops_diagnostics/knowledge_retrieval.py`（`MediaResource` / `issue` /
两个签发分支）、`src/aiops_diagnostics/qa_rag.py`（检索提示词）、
`docs/agents/frontend-api-brief.md`、ADR-0111（状态改为已反转）

## 背景

ADR-0111 决定给外部视频单独开 `external_url` + `embed` 两个字段，理由写得很清楚：
`media.url` 的语义是"我们的媒体代理能取回这段字节"，塞外部链接会让签名失去意义、
让 `<video src>` 拿到 HTML 页、并让服务端看起来在替任意地址背书。

**那个论证本身没错，但它漏查了一件事：前端到底怎么用这个字段。**

2026-10-23 解开 APK（`assets/apps/__UNI__EE327C0/www/app-service.js`）读到的实际渲染代码：

```js
var c = t.url || "";                                  // 读的就是 media.url
var l = "video" === a ? parseYoutubeUrl(c) : null;    // 自己去认 YouTube
if (l) { /* 内嵌播放器：youtubeId、start、封面 i.ytimg.com/vi/<id>/hqdefault.jpg */ }
else   { /* 普通 <video src> */ }
```

`parseYoutubeUrl` 自己就处理了 `youtu.be/<id>`、`watch?v=`、`embed/`、`shorts/`、
`live/`、`v/`、`e/`，还解析 `start` 参数、生成封面、跳专门的播放页。它的 mock 数据里
本来就有一条 `url: "https://youtu.be/…"` 的样本。

**换句话说：前端从来没有把 `media.url` 当成"只能是我们托管的字节"。** 它把它当成
"视频地址"，并已经为两种形态分别实现了渲染。

## 决策

**外部链接直接写进 `media.url`，删掉 `external_url` 与 `embed` 两个字段。**

```jsonc
"media": {"url": "https://www.youtube.com/shorts/…", "kind": "video", "mime_type": "video/mp4", …}
```

服务端仍然保留全部把关（ADR-0113 的 host + path 白名单），但那是**给字段填值**的
规则，不是新契约。前端零改动。

## 被否掉的替代形态

- **保留两个字段（A 或 B 并存）**。前端只读 `url`，`external_url` 会变成一个没人读的
  死字段；两个字段表达同一件事，下一个改这块的人必须先判断"该信哪个"。
- **让前端改成读 `external_url`**。这是 ADR-0111 实际导向的结果。代价是一个**已经做完
  并且做对了**的客户端为没有用户可见收益的改动改代码；而我们从中得到的只有"字段名更
  精确"。
- **只在服务端拼一个重定向地址**（`/v1/media/redirect/…` 302 到 YouTube）。会多一跳、
  多一份签名状态，而且 `<video src>` 跟随重定向到 HTML 同样播不了 —— 前端仍要识别。

## 代价（写在明处）

`media.url` 从此有两种形态：同域相对路径，或外部绝对地址。

任何**以后**想按 `url` 做转发、缓存、鉴权、日志脱敏的代码，都必须先判它是不是外部
地址。今天没有这样的代码（媒体代理只处理 `/v1/media/` 前缀，见 `MediaProxy`），但这是
一个真实存在的约束，不是"以后再说"。

换来的是：前端零改动，以及一个已经存在的、被真实客户端验证过的分派逻辑。

## 后果

更容易：知识库文档里写视频链接，回答里就能带可播放入口；前端一行不改。

更难：见上面的代价。另外 **Dify 那条路仍然拿不到** —— 它只取 `page_content` 与
`metadata.score`（`dataset_retrieval.py:518`），`metadata.media` 根本不进它的上下文。

**一条方法论**：ADR-0111 的错误不是推理错误，是**没有核对客户端**。设计一个跨系统
字段时，"我觉得它会怎么用"不能替代"去看它实际怎么用" —— 尤其当客户端已经是既成事实
的时候。
