# 客户双端开通：页面入口与逐端进度

日期：2026-10-30。分支 `feat/dual-entry-frontend`。后端在
[PR #1224](https://github.com/kilbertert/AI-Ops/pull/1224)（`feat/dual-entry-onboarding`）。

## 里程碑目标

把"复制并开通客户管家"变成"复制并开通客户 AI 服务"：默认双端、每端各选来源、
进度按端分别显示，并且**不把"已发布"说成"用户能用"**。

## 已完成内容

| 位置 | 改动 |
| --- | --- |
| `aiops-management.tsx` | 入口改名"复制并开通客户 AI 服务"；开通范围两个勾选（默认双端）；每端一个来源下拉；逐端进度行；`partial` 显示"部分完成" |
| `aiops-management.tsx` | 没有已发布来源的一端**默认不勾**并置灰（勾了也提交不了，只会让人以为坏了） |
| `aiops-management.tsx` | `ManagedPublish` 显示所属端、对客知识库数量、渠道待接入提示；按钮文案随端变化 |
| `aiops-management.ts` | `Status` 增加 `entry`/`knowledge_base_ids`/`channel`/`channel_note`/`readiness`；新增错误码文案（凭据对账、知识绑定、跨租户、动态知识回答缺失等）；新增逐端步骤名 |
| `aiops-management.browser.spec.tsx` | 5 项既有用例改名适配 + 新增 5 项：双端默认与单端、无来源不预勾、逐端状态与"渠道待接入"、旧后端兼容、客户端端标签 |

## 关键决定

1. **渠道状态不当成"已启用"。** 后端已发布但渠道未接入时，页面显示"已发布 · AI 已准备，
   渠道待接入"，不写"已启用"。四件事（资源/发布/渠道/真实用户）在页面上分开。
2. **旧后端兼容。** `entries` 不存在时退回"只有管家"的形状，不凭空造一个客户端选项 ——
   页面提交后端不认识的形状比少一个选项更糟。
3. **一键请求键的指纹包含范围与两端来源。** 改任何一项都会生成新键，不会把"改了来源"
   当成"重复提交"而静默复用旧任务。

## 验证结果

```bash
# 固定 1.17.1 源码 + 全部 web-patches + 组件/service 复制后
npx vitest --project browser --run app/components/aiops-management.browser.spec.tsx
# Test Files 1 passed (1) / Tests 10 passed (10)

npx tsc --noEmit
# 仅 1 条与本次无关的既有错误：dev-proxy.config.ts 缺 @langgenius/dev-proxy 类型
```

## 已知缺口

- 未在 41 上构建并部署这个前端镜像；真实页面验收未执行（见
  `docs/acceptance/dual-entry/PROGRESS.md`）。
- 客户在自己工作区里的"发布到客户端/管家"按钮复用既有 `ManagedPublish`，本片只加了端标签
  与渠道提示，没有新增编辑能力。

## 下一步

构建并部署 Dify web 补丁镜像 → 41 上真实页面开通与逐端发布验收。
