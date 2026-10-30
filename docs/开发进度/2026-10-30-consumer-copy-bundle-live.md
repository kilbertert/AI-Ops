# 文案包上线：部署核对与 CD 排队处置

分支`docs/consumer-copy-bundle-live`，基线`ae290a0d`。
验收证据见 [2026-10-30-consumer-copy-bundle-live.md](../validation/2026-10-30-consumer-copy-bundle-live.md)，
决策见 [ADR-0186](../adr/0186-consumer-copy-is-a-published-bundle.md)。

## 目标

把 #1211（按租户发布答复措辞）的上线结论写进交付状态索引，并把这一轮踩到的
"CD 一直 queued"的排查方法沉淀成可复跑步骤。

## 已完成内容

- 核对部署：CD `41028624018` success，41 回读 `1.0.0+ae290a0d6bf1`，`consumer_copy.py` 在位。
- 处置 CD 排队：定位到唯一的 self-hosted runner 被卡住的 `Agent Update Branch` job 占着 39 分钟，
  取消该 run 后 CD 立即启动并成功。
- 交付状态索引新增一节：**怎么改措辞**（文案包 + `aiops-admin consumer-copy` 三步），
  以及边界（只能 zh/en、占位符少一个就被拒、发布不会让在途答复变 503）。
- 41 手册新增 §5.4：CD 一直 queued 时的三条排查命令、本次实测数字、以及
  "不要为此改策略管理的 workflow" 的边界。

## 验证结果

| 项 | 结果 |
| --- | --- |
| 线上修订 | `1.0.0+ae290a0d6bf1` |
| 新代码 | `consumer_copy.py` 在 `/opt/aiops-41/src/...` |
| CD | `41028624018` success |
| runner 处置 | 取消卡住的 `41025764974` 后 CD 立即拿到机器 |

## 已知缺口

- **文案包没有在真实租户上发布过**：线上没有任何版本，真机观感也未验。
- 只有 `zh`／`en` 可改；九种本地化语种需先迁成组合式渲染。
- 节点路线仍用内置文案。

## 下一步

1. 由产品/运营给一版措辞，在 41 上给真实租户发布一次，用真实会话确认生效（这才是业务验收）。
2. 若要非中文语种可改，另起一片把那九种迁成组合式渲染。
