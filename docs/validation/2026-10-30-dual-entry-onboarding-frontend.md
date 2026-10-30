# 双端开通页面：浏览器验收记录

日期：2026-10-30。分支 `feat/dual-entry-frontend`。

## 环境与命令

固定 Dify 1.17.1 源码（`.scratch/dify-1.17.1`），应用 `deploy/dify-36/web-patches/1.17.1/`
下的补丁，复制 `aiops-management.ts(x)` 到 `web/app/components/`、`aiops-management.ts` 到
`web/service/`，然后把浏览器用例复制到 `web/app/components/` 后运行：

```bash
npx vitest --project browser --run app/components/aiops-management.browser.spec.tsx
# Test Files  1 passed (1)
#      Tests  10 passed (10)
```

## 用例

| 用例 | 断言 |
| --- | --- |
| 响应丢失后重试同一操作 | 两次提交的请求体逐字相同，`scope=both`、两端来源、自动名称都在 |
| 默认双端与只开一端 | 两个勾选默认选中；取消管家后提交 `scope=consumer` 且只带客户端来源 |
| 无已发布来源的一端不预勾 | 该端勾选未选中且置灰，不会提交 |
| 逐端状态 | 客户端"已发布 · AI 已准备，渠道待接入"、管家失败文案、整体"部分完成" |
| 旧后端兼容 | `entries` 缺失时只渲染管家，不造客户端选项 |
| 保存失败保留正式版号 | 既有用例，仍通过 |
| 客户端端标签与渠道提示 | 端标签、对客知识库数量、渠道待接入、按钮文案随端变化 |
| 发布带 revision 与并发冲突 | 既有用例，仍通过 |
| 模型默认输出上限 | 既有用例，仍通过 |
| 进入工作区需要授权 | 既有用例，仍通过 |

## 类型检查

```bash
npx tsc --noEmit
# dev-proxy.config.ts(1,59): error TS2307: Cannot find module '@langgenius/dev-proxy'
```

只有这一条既有错误（开发代理的可选依赖未安装），与本次改动无关；
`aiops-management.tsx` / `aiops-management.ts` 没有类型错误。

## 未覆盖

- 没有在 41 上构建并部署前端镜像，所以**没有真实页面证据**。真实页面验收见
  `docs/acceptance/dual-entry/PROGRESS.md`。
