# 137. 发布桥重写平台哨兵常量，不从登录服务导入

日期：2026-10-25
状态：已采纳
影响版本：`src/aiops_diagnostics/dify_console_publish.py`、
`tests/test_dify_console_publish.py`

## 背景：平台账号发布时报「你所在的租户还没有可用的 Dify 工作区」

`system`（公司体系的平台账号）在 Dify 控制台点「发布到 AI-Ops」，运营看到：

> 你所在的租户还没有可用的 Dify 工作区，不能发布。请联系管理员为该租户开户后再试

而 36 上那三个 workspace 早就建好了，`dify_workspaces` 表里 `platform` 那行
`setup_state='ready'`。**这句提示是不成立的。**

链路上真正发生的事（36 的桥日志，2026-10-25 10:36:30）：

```
workspace lookup failed: _WorkspaceLookupFailed: 400
console publish refused: the initiator's tenant has no workspace
```

平台账号在公司体系里没有 `sys_organ`，它的 `tenant_id` 是**哨兵值 `-1`**。
桥把这个 `-1` 原样发给网关的 `GET /v1/dify/workspace?tenant_id=-1`，而网关的
`_SAFE_ID = ^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$` 要求首字符是字母或数字 ——
`-1` 不匹配，抛 `ValueError` → **400 `DIFY_WORKSPACE_TENANT_INVALID`**。

桥把所有非 2xx 都译成「没有工作区」，于是「租户 id 非法」被抹成了一句不成立的话。

## 登录侧早就处理了，发布桥漏了同一层

`dify_login_service.py:447`（#882）：

```python
workspace_tenant = PLATFORM_TENANT_ID if user_type == PLATFORM_USER_TYPE else identity.tenant_id
```

平台账号落到它**自己的** `platform` workspace —— 不靠进别人的 workspace 体现「可见
全部」（ADR-0059 §4）。发布桥拿到同一个 `company_user_type` 字段（它已经在
`X-AIOps-Initiator-Type` 头里），却没有用。

## 决定

发布桥做同样的映射：`X-AIOps-Initiator-Type == "-1"` ⇒ 查 `platform` workspace。

**两个常量在本模块内重写，不从出处 import。** 这不是风格问题：

发布桥的部署制品只含五个模块（`ops/dify-console-bridge/build-bundle.sh:13`）：

```
src/aiops_diagnostics/{__init__,dify_console_publish,async_job_lifecycle,bounded_http,http_auth}.py
```

第一版就是 `from aiops_diagnostics.dify_login_service import PLATFORM_USER_TYPE`。
**本地全绿，服务宿主上 `ModuleNotFoundError`** —— `test_console_bridge_deployment`
的五条用例立刻转红。桥是一个被裁剪的运行时，它的模块边界是部署形态决定的，不是
包结构决定的。

## 备选方案与取舍（被否的路线）

- **从 `dify_login_service` / `dify_workspaces` 导入常量。** 被否：见上，制品不含那些
  模块。这是实测出来的，不是推测。
- **把这两个模块加进 bundle。** 被否：会把登录服务的依赖链（SQLite 映射表、公司
  HTTP 客户端）拖进一个只需要读 `dify_console_publish` 的宿主。桥的职责是发布，
  不该因为它要判「是不是平台账号」而背上整条登录链。
- **在桥里改判据：`tenant_id == "-1"` 时直接用 `workspace_id` 常量。** 被否：那正是
  #884 明令禁止的「回落默认 workspace」——回落等于把「这个租户没开户」悄悄变成
  「发布进别人的工作区」。
- **只改错误文案，说清是「租户 id 非法」。** 被否：文案改准确有价值（另记一条），但
  它不解决「平台账号永远发布不了」这件事。那是功能缺失，不是措辞问题。

## 后果

- `system` 现在可以在自己的 `platform` workspace 里发布，不会看到也改不到租户的 app。
- 两个常量在桥里有一份副本。代价由两条用例钉住：
  - `test_the_inlined_platform_constants_match_their_sources` —— 出处改了这里没跟上就红；
  - `test_a_tenant_account_still_resolves_its_own_tenant` —— 对照，证明映射没有把
    租户账号一起改宽。
- `shortcut_lifecycle.py` 另有同名的 `PLATFORM_TENANT_ID = "__platform__"`
  （快捷动作命名空间，与 workspace 无关）。那条也被一条断言钉住，防止有人「统一」它们。
- **本次没有修**桥把所有非 2xx 译成「没有工作区」这件事。它让 400 与 404 无法区分，
  下一次再遇到参数类错误仍会被误读。那需要单独的改动与它自己的判据。
