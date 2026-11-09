# 2026-11-09 管理操作报错透出真实原因（PR #1445）

## 目标

测试反馈受管发布报「管理操作未完成，请重新登录或联系平台管理员」。第一性原理排查后，
根因是**前端错误翻译面**而不是登录态，且不是一处缺口而是一类缺陷：

1. `messages` 表靠手工穷举 `error.code`，后端约 160 个字面 code 只收录子集；
   未收录的一律落到固定兜底句，把版本撤回、并发冲突、凭据缺失、执行组件未就绪
   全部伪装成会话问题。
2. `node_publication.py` 的 `advance()` 把 `RouteBindingError` 用 `str(exc)` 包成
   `NodeRouteConflict` —— 信封只有 `code` 一个槽位，于是给人看的 message
   （'route binding moved'）泄漏成了机器码，任何译文表都不可能命中。
3. `manage()` 三条传输路径各自裸抛：fetch 拒绝、`response.json()` 解析异常、
   非 2xx 非 JSON，TypeError/SyntaxError 原文直接上屏。

## 已完成

### 1. `failureText(body, fallback)` 统一错误→文案解析（`aiops-management.ts`）

顺序：**本表译文 → 后端正文（`reason`/`error.message`/`detail`，附 code）→
原样带码兜底**。后端再新增 code 的最差显示是「兜底（CODE）」，一张截图即可定位，
永远不产生误导性提示。

### 2. `messages` 补齐可达拒绝面（约 +40 条）

SETUP_* 缺口（APP_NOT_MANAGED/CREDENTIAL_REQUIRED/DISABLED/VERSION_MISMATCH/
BUSY/LEASE_LOST/…）、ROUTE_BINDING_* 家族、`NodeRouteConflict` 的英文短句 code；
撤回版本给出「恢复历史版本」指引。`SETUP_APP_CONFLICT` 按 review 意见订正为
「登记身份冲突」（`self_service_store.py` 三处 raise 都是归属/端不一致，
刷新无效），不再误写成「其他任务占用」。

### 3. `manage()` 容错

- fetch 拒绝只按 `TypeError` 译成「无法连接管理服务，请检查网络后重试」；
  测试替身自抛的 Error 原样透传。
- 非 2xx 一律走 `failureText`，非 JSON 响应体带 HTTP 状态码兜底。
- 2xx 非对象响应报「管理服务返回了无法解析的响应」，不再让 SyntaxError 上屏。

### 4. 两条显示路径口径一致

`entrySummary()` 进度 chip 与行级错误同走 `failureText`：未收录 code 显示
「这一端未完成（CODE）」而非笼统的「这一端未完成」。

### 5. `node_publication.py` 包裹修正（Devin review 驱动的后端一行）

`raise NodeRouteConflict(str(exc))` → `raise NodeRouteConflict(exc.code)`：
包裹后的 `NodeRouteConflict` 透出稳定 `ROUTE_BINDING_*` code，恰好命中第 2 条
新增的译文，而不是英文 message 短句。

## 分支与提交

- 分支 `fix/manage-error-banner`，基于 `origin/main` `4a493ce5`；
  PR https://github.com/kilbertert/AI-Ops/pull/1445 。
- 测试现场：`.scratch/dify-1.17.1/web`（vitest browser/unit project），
  每次跑后恢复快照，未留改动。

## 验证结果

- `aiops-management.browser.spec.tsx` **15/15**（存量 11 + 新增 4 契约规格：
  撤回版本译文、未知 code 带文带码、裸 code 不含「重新登录」、502 非 JSON
  按状态码描述），对最终代码复跑确认。
- `aiops-workflow-managed.browser.spec.tsx` 6/6、`aiops-workflow-header.spec.tsx`
  （unit）1/1。
- `pytest tests/execution/test_node_publication.py tests/dify/test_dual_entry.py`
  **46 passed**（`exc.code` 修正后）。
- CI verify / Devin Review / spec-tree / mirror / workflow-policy 全绿（首版提交；
  本轮 review 修订推送后 CI 重跑，结果待观察）。

## 已知缺口

- `NodeRouteConflict` 自带的四个英文短句仍是 code（'node route retracted' 等），
  靠 `messages` 直译兜住；统一为 ROUTE_* 词表是后端词表收敛工作，不在本里程碑。
- 未收录未来 code 的设计性兜底就是原文上屏（带码）——这是刻意的可定位性取舍。
- 生产生效依赖 dify web 镜像重建（webpack）+ 36 换镜像；合并前操作员看到的仍是旧兜底。

## 下一步

合入后按 `deploy/dify-36/web-patches/build-web-image.sh` 从合入版构建 web 镜像并在
36 `--force-recreate`；部署后对一次真实「已撤回版本重发」复测横幅文案。
