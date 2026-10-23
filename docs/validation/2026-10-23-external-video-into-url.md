# 外部视频地址改回 `media.url`：验收记录（#1035 / ADR-0120）

**范围**：`src/aiops_diagnostics/{knowledge_retrieval,qa_rag,dify_knowledge_api}.py`、
`docs/agents/frontend-api-brief.md`、`deploy/m2-live-03/images.lock.json`。
**性质**：契约反转 + 一处真实缺陷修复。**未做 41 真机验收**，见文末。

## 一、为什么反转

用户问：「前端不是已经预留好了视频 url 的字段吗，为什么还要前端调整」。

去读前端**产物**（APK `assets/apps/__UNI__EE327C0/www/app-service.js`）：

```js
var c = t.url || "";                                  // 读的就是 media.url
var l = "video" === a ? parseYoutubeUrl(c) : null;    // 自己去认 YouTube
if (l) { /* 内嵌播放器：youtubeId、start、封面 i.ytimg.com/vi/<id>/hqdefault.jpg */ }
else   { /* 普通 <video src> */ }
```

`parseYoutubeUrl` 自己处理 `youtu.be/<id>`、`watch?v=`、`embed/`、`shorts/`、`live/`、
`v/`、`e/`，解析 `start`、生成封面、跳专门播放页；mock 数据里本来就有一条
`url: "https://youtu.be/…"` 的样本。

**结论**：前端从来没把 `media.url` 当成"只能是我们托管的字节"。ADR-0111 要求一个
已经做完并且做对了的客户端改代码，换来的只有"字段名更精确"。

## 二、改了什么

```jsonc
// 之前（ADR-0111）
"media": {"external_url": "https://youtu.be/…", "embed": "youtube", /* 没有 url */ }

// 现在（ADR-0120）
"media": {"url": "https://www.youtube.com/shorts/…", "kind": "video", …}
```

服务端的 host + path 白名单（ADR-0113）**保留**。

## 三、判据与结果

| # | 判据 | 结果 |
|---|---|---|
| 1 | 文本 chunk 带视频链接 ⇒ `media.url` 就是该链接 | ✅ |
| 2 | **签发出来的 `url` 能被前端 `parseYoutubeUrl` 认出来**（判据照抄 APK 实现） | ✅ 三种形态 |
| 3 | 我们托管的 mp4 ⇒ `url` 仍是 `/v1/media/…` | ✅ |
| 4 | 频道页 / 播放列表 / 首页 / 形似域 ⇒ 不签发 | ✅（ADR-0113 不回归） |
| 5 | **配了 public base 时，外部地址不被拼 base** | ✅（见下） |

**变异复验**：

| 变异 | 转红 |
|---|---|
| 外链仍签本地路径 | 4 条 |
| 去掉 `url.startswith("/")` 判据（Codex 指出的缺陷） | 1 条，实际拼出 `https://api.mall…https://youtube…` |

## 四、Codex review 抓到的真实缺陷

PR #1047 上指出：`dify_records()` 无条件 `f"{prefix}{item.resource.url}"`。配上
`AIOPS_GATEWAY_PUBLIC_BASE_URL` 时，外部视频地址会被拼成

```
https://api.mall.qushiyun.comhttps://www.youtube.com/watch?v=…
```

—— 一个对日志看起来"完整"、在浏览器里永远打不开的字符串。**是我引入的**：改契约时
没有回头查这个消费方。

修法：只给**相对路径**拼 base。新增回归用例，去掉判据即转红。

## 五、测试

```
./tools/test.sh                    3705 passed, 9 skipped, 0 failed
tests/test_dify_knowledge_api.py   45 passed
ruff check / format / adr check    通过
```

## 六、未完成的部分（明说）

- **未做 41 真机验收。** 本片只到单元与契约层；部署由 CD 负责。真机要验的是
  「命中含 YouTube 链接的 chunk ⇒ 回答里 `media.url` 是那个外链，且前端能播」。
- **前端渲染未验。** 前端零改动这个结论来自**读它的代码**（`parseYoutubeUrl` 的判据
  被我复刻成用例 2），不是在真机上点开一个播放器看它播了。
  这比"单测全绿"强，但**仍不是端到端证据**。
- **Dify 那条路仍然拿不到外链**：它只取 `page_content` 与 `metadata.score`，
  `metadata.media` 不进它的上下文。这是 Dify 侧的限制，不是本片能修的。
- **`用户端(1).docx` 里 28 张图**仍拿不到：`naive` 解析不抽图，是独立问题。
