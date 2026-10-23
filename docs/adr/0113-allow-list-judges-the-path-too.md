# 113. 外链白名单要判到 path：频道主页不是视频

日期：2026-10-23
状态：Accepted
影响版本：`src/aiops_diagnostics/knowledge_retrieval.py`（`_addresses_one_video` /
`_external_video_link`）、`tests/test_knowledge_retrieval.py`、
`deploy/m2-live-03/images.lock.json`

## 背景

ADR-0111 给 `media` 加了 `external_url`，白名单只判 **host**。合规性上那是对的
（"这个域名我们信得过"），但**功能性上不够**，而这一点是 CD 部署后在 41 上跑真机
才发现的 —— 单测与代码评审都没抓到。

真机现象（2026-10-23，41，生产代码）：

```
chunk: TRENDPOWER_EV_Charging_Platform_Video_Modules.md
   external_url: https://www.youtube.com/shorts/Ld7_TVdVFpk      ← 对，是视频
   external_url: https://www.youtube.com/@trendpowercharge        ← 错，是频道主页
```

查询里只要有 "Platform Tutorials" 这类词，命中的 chunk 里就同时有这两个形状，因为
那份素材本来就同时列了「视频链接」和「官方频道主页 / 检索源」。

**后果**：前端拿到一个 `kind="video"` 的块，里面指向一个没有单个视频可播的频道页。
用户看到的是一个坏掉的播放框 —— 而且看起来像我们的 bug，不是数据的问题。

## 决策

**白名单是 host + path 两级。**

| host | 允许的 path |
|---|---|
| `youtu.be` | 任意（裸路径就是视频 id） |
| `youtube.com` / `www.` / `m.` | `/watch`、`/shorts/`、`/embed/`、`/live/` |
| `www.youtube-nocookie.com` | `/embed/` |

**认不出的形态一律当作"不是视频"。** 这个不对称是刻意的：漏判的代价是少一个链接，
误判的代价是一个看起来像我们 bug 的坏播放器。

还有一条同源的规则：**跳过不匹配的链接继续找，不能"取第一个白名单域链接"就收工**。
真机那次 chunk 里频道链接排在前面，如果能匹配到就返回，真视频会被挡掉。

## 被否掉的替代形态

- **只看 host，把频道页也签出去**。等于把"这页能不能播"的判断推给前端，而前端拿到
  的是 `kind="video"` + `embed="youtube"`，没有理由怀疑它。
- **在 `embed` 上加一个 `"youtube-channel"` 值**。这会让前端为数据里的噪音长出一条
  渲染分支；频道主页在"回答里给一段视频"这个语境下根本不是要交付的东西。
- **按内容猜（标题里有"频道"就不签）**。文本分类做不准，而且素材里频道页并不总是
  带"频道"字样。
- **只在提示词里告诉模型别引用频道链接**。模型引不引用不影响签发；问题是服务端先
  把它签成了 video 资源。

## 后果

更容易：同一个人上传的素材里混着视频链接与频道链接时，只有视频能到前端。

更难：YouTube 新增一种视频 URL 形态（比如新的路径前缀）时要在这里加一行 —— 而那正是
想要的：新增一种"我们替用户担保的地址"应该是一次显式的决定。

**代价写明**：`/playlist?list=…` 这类"一组视频"的链接现在会被判成不是视频。那是刻意的
—— 契约里一个 video 块只对应一个播放源。
