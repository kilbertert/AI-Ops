# 运行日志复核行渲染修正：实现验收

2026-11-13。候选 `fix/run-log-review-rendering`，纯 Dify web 补丁层改动。
**未完成业务验收**：页面真实渲染验收在部署后进行（合并后换镜像 + 真实 run
截图取证），本文先记实现侧证据。

## 变更面

- `describe()` `review` 分支：running → 已进行；succeeded+非 allow →
  「事实复核观察：」；refused 保持失败措辞。投影与后端零改动。

## 自动化

- `npx esbuild` 对两文件做 TSX 语法解析：干净。
- 新增 3 个 vitest 浏览器用例（running 行、观察标注意、refused 措辞），
  沿用既有 stub/render/getByText 约定。
- vitest 浏览器用例需 web 构建环境，本机未执行——构建期 webpack 编译校验，
  同 ADR-0251 口径。

## 未验范围

- 真实 run 上的页面渲染（部署后截图取证补上）。
- 「复核开始+结束两行」是否应合并为一行，属展示取舍，本轮不动。
