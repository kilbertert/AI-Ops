# 环境管理页面→真实管理 API 浏览器验收

2026-11-22。工单 [#1537](https://github.com/kilbertert/AI-Ops/issues/1537)/[#1538](https://github.com/kilbertert/AI-Ops/issues/1538)。
基线 `3b4594f4`（紧随正式发布纵切 `c9d8f03c`，版本线 1.39.0），**无源码变更**。
本回执补齐「页面→真实 API」接缝：真实 Chromium 驱动现有「环境与租户」组件，
浏览器请求逐字节穿过真实桥与真实管理 API 落一次性持久根，不是 mock fetch。

## 链路与合成边界

`deploy/dify-36/web-patches/1.17.1/aiops-environments.tsx` 与 `aiops-management.ts`
经 esbuild 原样打包挂进极简外壳；同源 `POST /__aiops/manage` 纯转发进
`dify_console_publish.make_server`（真桥：Origin/Referer/CSRF/cookie 校验 +
console summary 核对）→ uvicorn `self_service_api.register`（真
`management_session` 鉴权）→ `auth.identity` → `operator_cpms.console_identity`
+ `company_get` → `environment_drafts`/`environment_registry` → 真 SQLite +
JSON 账本。

合成面（任务允许范围）：公司 `/__aiops/internal/operator-token`、`/user/info`、
Dify `workspaces/current/summary` 为本地替身；外壳仅提供导航链接与路由 push
记录。其余全为仓库代码。

## 七场景实测（全绿，键盘输入，逐点截图）

- S1 新增两个环境草稿、同号租户 `same`：保存后页面重载，字段由服务端草稿完整
  回填，生效目录不变。
- S2 编辑生效环境名称与 `gateway_url` 草稿：草稿 V1 入库，生效目录名称/入口
  逐字保持，页面「允许准入」不变——草稿编辑不改变生效入口。
- S3 两编辑者并发：先保存方得 V2，持旧 revision 方保存得 409「草稿已被其他
  管理员更新」，页面输入保留，服务端不被覆盖。
- S6 草稿 `enabled=false` 持久化，生效目录仍 `enabled=true`——草稿停用≠生效
  停用。
- S7 脏表单切换环境项/点主导航链接均弹「放弃未保存修改？」；「继续编辑」保
  输入，「放弃并继续」才放行（导航目标 `/apps`）。
- S4 真实撤权（`registry.apply` 清空 managers）：刷新即「当前账号没有环境
  管理授权」并清空表单，重放保存 403 `ENVIRONMENT_MANAGEMENT_FORBIDDEN`。
- S5 普通租户会话：页面读取与绕过界面直接 POST 保存均 403。

驱动结束后草稿库断言 alpha V3 / beta V1 / gamma V1，生效仅 alpha enabled；
账本 revision 经撤权推进到 R2。未改源码，不加永久测试（无缺陷不为数量造例）。

## 边界

页面在极简外壳渲染（无 Tailwind/Dify 壳）；开通计划/任务/发布链未在本轮驱动，
超出 UI 草稿管理接缝。生效目录行显示生效名而非草稿名，符合「分别显示」合同。

## 正常公司页面保存与恢复（同日补验）

2026-11-22 12:55（北京时间），控制Gateway/桥/Web为`1.39.0+c9d8f03cb7d0`。
由人输入验证码正常登录，选择真实124既有草稿，只给环境显示名追加验收标记：
保存返回V10（原V9），刷新后实际读回新名；核目录R1及生效环境列表完全不变，
其他草稿/连接/租户字段逐值保持。随后以V10为CAS前驱恢复原名，返回V11；
再次刷新及API回读确认原字段全部恢复。无并发冲突，没有倒写版本号。
原生退出200，旧会话`SESSION_LOGIN_REQUIRED`且无身份；临时浏览器和管理连接结束。

没有新增假环境、修改地址/秘密、切换准入、运行模型或发安装/发布任务。
该证据补齐真实页面的保存/刷新/恢复；两环境新增、并发和撤权仍是上文隔离场景，
不能据此宣称真实双环境生效停用或P5完成。

## 证据位置

原始验收栈、驱动脚本、截图与日志仅存私有 dispatch 目录
（`~/.cache/aiops-onboarding-completion-20261122/dispatch/ui/`），不入仓。
隔离脚本及截图在`evidence/`，正常公司页面原件在`live-draft/`；
两次保存均读取实际revision，冲突时停止并核对，不强制覆盖他人更新。
