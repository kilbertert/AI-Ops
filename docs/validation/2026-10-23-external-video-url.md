# 外部视频链接进 `blocks[]`：验收记录（#1035）

**范围**：`src/aiops_diagnostics/knowledge_retrieval.py`、`zh` 提示词、前端契约文档。
**不涉及生产部署**（本片只改代码与契约；41 上线需另走 CD）。

## 一、要解决的问题

业务素材里的视频有两种，契约只表达得了一种：

| 类型 | 例子 | 我们手里有什么 | 之前能不能播 |
|---|---|---|---|
| 我们托管的 mp4 | `新加坡无人电动巴士.mp4` | 字节，走 `/v1/media` | ✅ |
| 别人平台上的视频 | TRENDPOWER 表 19 条 YouTube 链接 | 只有链接，**没有字节** | ❌ 到不了前端 |

## 二、开票前先核实的事实

含链接的那个 chunk 长这样（36 上实测）：

```
docnm_kwd  = 'TRENDPOWER_EV_Charging_Platform_Video_Modules.md'
doc_type_kwd = []            ← 纯文本，不是视频文档
content: "- YouTube 视频链接：https://www.youtube.com/shorts/Ld7_TVdVFpk"
```

**这条事实决定了实现**：旧判据 `doc_type == "video" or _is_video_name(title)` 对这种
chunk 不成立，所以它连"视频块"都不会被创建。不额外加一条分支，这个功能对真实数据
永远不生效。

## 三、实现

`media` 新增两个字段，`url` 的语义不变：

```jsonc
"media": {"url": "/v1/media/media_xxx.sig",   // 有我们自己的字节时才有
          "external_url": "https://youtu.be/…",
          "embed": "youtube"}
```

三条约束：

1. `external_url` 必须来自**本轮命中的 chunk**（与 `resource_id` 同一条件）；
2. **域名白名单**，比对**解析后的 hostname**，不是 URL 前缀；
3. `embed` 由服务端从 host 推出，是枚举。

**外部链接的块没有 `url` 字段。** 我们手上没有字节；给它签一个 `url` 会让前端拿到
"Markdown 文件的字节、标着 `video/mp4`" —— 一个 200 响应的空播放器。

## 四、判据与变异复验

5 条新增用例全部通过，3 处变异分别让对应用例转红：

| 变异 | 转红的用例 |
|---|---|
| host 精确匹配 → 前缀匹配 | `test_a_lookalike_host_does_not_pass_the_allow_list` |
| 外链字段改成总是输出 | `test_a_video_document_without_an_external_link_keeps_its_own_url` |
| `url` 永不为空 | `test_a_text_chunk_that_links_a_published_video_becomes_a_video_block` |

第一条是要害：`https://youtube.com.evil.example/…` 在前缀匹配下会被放行，而这个字段
的全部含义就是"服务端替用户担保了这个地址"。

## 五、同步更新的内容锁

改 `src/` 之后 `deploy/m2-live-03/images.lock.json` 的 `source_sha256` 必须跟着变
（全量测试里有一条专门比对它）。值是 `context_digest.py` 算出来的，**不自造**：

```
e8e37d7b114d1d6e… → afad662dca13beb0…   （165 个文件，数量未变）
```

`image_id` 与 `built_at` **未改**：它们由一次真实构建写入，这次没有构建。

## 五之二、真机验收抓到一个缺陷（2026-10-23，41）

CD 部署 `702bc5e` 之后在 41 上用生产代码跑 `normalize_search_response`，正面判据成立：

```
chunk: TRENDPOWER_EV_Charging_Platform_Video_Modules.md
   media: {"kind": "video", "external_url": "https://www.youtube.com/shorts/Ld7_TVdVFpk",
           "embed": "youtube"}
```

**但同一次运行里 `https://www.youtube.com/@trendpowercharge` 也被签成了 video 块。**
那是**频道主页**，不是单个视频 —— 前端会把播放器挂在一个没有视频可播的页面上，用户
看到一个坏掉的框，而且看起来像我们的 bug。

根因：判据只有 **host**，没有 **path**。同一个文档里既有 `watch?v=…` 也有
`@trendpowercharge`，两者 host 相同。

**修法**：白名单是 host + path 两级（`_addresses_one_video`）。`youtu.be` 的裸路径
本身就是视频 id 所以不受影响；其余 host 必须匹配 `/watch`、`/shorts/`、`/embed/`、
`/live/` 之一。**认不出的形态一律当作不是视频** —— 漏判的代价是少一个链接，误判的
代价是一个看起来像我们 bug 的坏播放器。

同一个扫描里还确认了一条：频道链接出现在**前面**、真视频在后面时，要跳过频道页继续
找，不能"取第一个白名单域链接"就完事。

新增 3 条用例，去掉 path 判据的变异让其中 2 条转红。

## 六、环境问题（与本次改动无关）

在 worktree 里用主 checkout 的解释器跑全量时，`test_parallel_setup.py` 两条转红 ——
它们检查的正是"测试解释器必须是**本项目**的 venv"。`uv sync --locked --dev` 之后用
`./tools/test.sh` 跑，全绿。

## 七、缺口

- **没在 41 上做真机验收**：本片只改代码，部署由 CD 负责。真机要验的是
  "命中 TRENDPOWER 表 → 回答里有 `external_url` 且 `embed=youtube`"。
- **前端渲染未验**：契约写在 `docs/agents/frontend-api-brief.md`，iframe 那一侧不在本仓。
- Dify 那条路**永远不会**有这个字段（它只取 `page_content` 与 `metadata.score`）。
