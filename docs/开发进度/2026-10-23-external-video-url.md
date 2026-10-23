## 2026-10-23 · 外部视频链接进 `blocks[]`（#1035）

**要解决的问题**：业务素材里的视频有两种 —— 我们托管的 mp4（能播），和别人平台上的
链接（YouTube，播不了）。后者在 #1035 之前**完全到不了前端**：链接在 chunk 正文里
躺着，而 `blocks-v1` 的 `video` 块只认 `media.url`，那个字段的语义是"我们的媒体代理
能取回这段字节"。

**一个反直觉的事实**：含链接的那个 chunk 是**纯文本分段**（`doc_type_kwd=[]`），
不是视频文档 —— 所以它连"视频块"都不会被创建。旧判据是
`doc_type == "video" or _is_video_name(title)`。

**做法**：新增两个字段，`url` 的语义一个字不改。

```jsonc
"media": {"url": "/v1/media/media_xxx.sig",
          "external_url": "https://youtu.be/…",
          "embed": "youtube"}
```

三条约束：`external_url` 必须来自**本轮命中的 chunk**；**域名白名单**且比对**解析后的
hostname**；`embed` 由服务端从 host 推出、是枚举。

**两个必须解释的细节**：外部链接的块**没有 `url` 字段**（我们手上没有字节，给它签一个
`url` 等于让前端把 Markdown 当 mp4 播）；文本 chunk 也能产出 `video` 块（实际素材就是
一张"标题 + 链接"的表，不放宽这个功能对真实数据永远不生效）。

**判据**：5 条新增用例 + 3 处变异复验全部转红。变异分别是：host 精确匹配改成前缀
（`youtube.com.evil.example` 被放行）、外链字段改成总是输出、`url` 永不为空。

**同步更新的内容锁**：`deploy/m2-live-03/images.lock.json` 的 `source_sha256`
（改 `src/` 后必须跟着变，用 `context_digest.py` 算，不自造）。

**范围外**：Dify 那条路永远拿不到 `external_url` —— 它只取 `page_content` 与
`metadata.score`，`metadata.media` 不进上下文。已写进
`docs/agents/knowledge-media-playback.md`。

**交付**：本分支（源码 + 测试 + ADR-0111 + 前端契约 + 内容锁）。
