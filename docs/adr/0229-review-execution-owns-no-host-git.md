# 229. 独立审查只写容器副本，候选安装不在宿主执行

Status: Accepted

日期：2026-11-05。承接 ADR 0220 与 #1403 复审 H1/H3/H4。

## 决定

本机 ralph 与 Actions 的两个独立审查轴复用 `review-sandbox.ts`：
宿主以 `git clone --no-local` 建立独立对象库，仅只读挂载该副本；容器再复制自己的可写仓库。
无共享对象、alternates 或指向宿主的 origin，审查内 `update-ref`、Git 配置与安装脚本均不能改写源仓库。
审查结束核对候选 HEAD/已跟踪文件及源分支 SHA；候选有变化则要求重新验收。
副本位于 gitignored `.sandcastle/reviews/`，清理只删除本次 `mkdtemp` 产生的目录。

`npm ci` 与 `uv sync` 通过既有容器准备脚本执行，删除 planner 的宿主安装 hook。
容器创建的最后一步按允许列表传模型连接变量，其他 `.env` 键不继承；仍保留模型认证和现有
profile 网络模式，因此不是“无任何秘密”或网络隔离的声明。供应商凭据不应作为审查材料读取。

裁决模块依照 CommonMark 围栏规则识别同字符、足够长度的闭合行，围栏内标记不计数；
未闭合围栏、冲突标记及缺失末行裁决均拒绝。规范范围仅为文本裁决，不声称解析全部 Markdown。
标准依据：[CommonMark 0.31.2](https://spec.commonmark.org/0.31.2/#fenced-code-blocks)、
Git `clone --no-local` 官方语义，以及已锁定 Sandcastle 0.12.0 的 provider/create 契约。

## 被否掉的替代形态

- **只增加禁读提示与清空几个 token**：改动少，但无法限制 `npm` 生命周期脚本、其他认证变量和可写 Git 挂载。
- **直接只读挂载正式 Git 目录**：能阻止 ref 写入，但 SDK 需要修改 Git 状态，候选安装也需要可写空间。
  自有副本保留这些能力，且容器修改的 Git 配置不会被宿主 Git 消费。
- **新增自托管审查平台**：隔离能力更完整，但超出本次已知反例；复用现有 Docker 镜像和 SDK。

## 代价与边界

每个独立审查增加复制和容器安装成本，换取源仓库写权限的隔离；不改 Devin 或确定性 CI 闸门。
模型令牌、镜像信任与既有 host-network profile 仍有各自边界，不能用本次文件隔离证明网络隔离。
凭据轮换需独立完成；新机制不撤销历史暴露。
