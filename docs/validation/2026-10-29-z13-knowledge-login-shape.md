# 知识库登录形状修复与优链充知识空间补齐验收

日期：2026-10-29（北京时间）。本轮接在 PR #1155 合并（`1be3f067`）之后：
先用合并制品部署 Gateway/桥/前端，再跑原开通任务重试，重试仍失败；定位到一个
**独立缺陷**——恢复工具写进 RAGFlow 的密码散列形状不对。修好后原任务完成，
知识空间与知识库入口在真实链路上验通。

## 缺陷与根因

| 项 | 事实 |
|---|---|
| 症状 | 原任务 `3cd45ef6` 停在 `step=knowledge`、`error_code=SETUP_KNOWLEDGE_FAILED`；`/kb/tenants/verify-password` 回 401「Email and password do not match」。 |
| 直接原因 | 恢复工具 `deploy/kb-service/recover_tenant_login.py` 调 `UserService.update_user_password(user.id, 明文)`，落库的是 `generate_password_hash(明文)`。 |
| 真因 | RAGFlow 登录路径把 `decrypt(crypt(明文))` 交给 `check_password_hash`；它自己的 `api/utils/crypt.py` 写明 `decrypt(crypt(input)) == base64(input)`，即上游比对的是 **base64(明文)**。存明文散列，之后任何一次登录都不可能匹配。 |
| 为什么没早发现 | 恢复工具当时只验证「密码写入成功」「会话标识未变」，没有回放上游登录判据。 |

证据（在 `ragflow-kb-ragflow-cpu-1` 内用上游自己的 `crypt/decrypt/check_password_hash` 实跑）：

```
{'login_password_is_b64': True,
 'correct_store_matches_login': True,          # 存 base64(明文) 的散列 → 通过
 'current_recovery_store_matches_login': False} # 存明文散列 → 不通过
```

## 修复

`recover_tenant_login.py` 改为写入 `login_form(password)`（base64 编码后再散列），
并在写完后用上游同一判据回放一次，不满足就报错并要求保留备份（`verify` 注入，
容器内由 `crypt/decrypt/check_password_hash` 实现，单测用替身驱动）。

## 部署与真实回读

| 组件 | 修订 | 证据 |
|---|---|---|
| Gateway | `1be3f067` | 服务 17:55:51 重启；`/opt/aiops-41/src/.../self_service_prepare.py:141 knowledge()` 回读一致 |
| Dify 发布桥 | `1be3f067` | 制品 `bridge.tar.gz` SHA256 `87d5243a…0479f`；36 上 `deploy.sh` 备份 `/opt/aiops-knowledge-1be3f067-bridge-backup`，`REVISION` 回读一致，含 `self_service_bridge.py` 与 `dify_console_publish.py` |
| Dify Web | `1be3f067` | 镜像 `aiops/dify-web-patched:knowledge-1be3f067`，`org.opencontainers.image.revision=1be3f067`；构建源 `aiops-management.tsx` / `.ts` 与合并修订 sha256 逐字节一致（`a29c8d05…eacf0` / `3163db0e…0fb0e5`）；`.env` 由 `self-service-f58e86d6` 切到该 tag，`--force-recreate web` 后 `Ready`，`/signin` 200 |
| 恢复工具 | 本分支 | 只在受控维护时运行一次，改一个账号的登录密码，先写 0600 备份 |

密码恢复执行（stdin 传入、不落日志、不写仓库）：

```
{"email": "90c5be3176bf0085@kb.internal", "user_id": "551c8898d36f11f1a026f1f706dc4043", "action": "password_recovered"}
```

恢复后经 Gateway 自己的客户端路径回读：

```
verify_login OK
repair: {"factory":"OpenAI","instance":"baoyun","instances":["baoyun"],"defaults":{...}}
embedding dim 3072
```

## 原任务重试

平台账号用正常公司密码＋图形验证码登录（`https://dify.ranlei.work/signin`），
在「复制并开通客户管家」面板点原任务「重试」。任务状态回读：

```
{"id":"3cd45ef6-…","state":"completed","step":"complete","error_code":"",
 "resources_json":"{… \"app_id\":\"a57602c1-…\",\"published\":false,\"knowledge_ready\":true}"}
```

`ragflow_tenants` 同一行 `model_checked_at` 由空变为 `2026-10-29T10:41:44Z`。
开通复用原工作区 `9d2ccbac…` 与原应用，**未**新建第二个应用或工作区。

## 正式路由不变

41 上 `agents` + `chat_route_bindings` 回读：优链充仍是
`ops-youlian-order-diagnosis` V2 / revision 1 / `app_id=349a637c-…` / `state=active`；
新建的 `a57602c1-…` 只是草稿，没有进入任何路由。原租户 `1942105476598861824`
的 `ops-methodology-700` V22 / revision 3 同样未动。

## 平台进入知识库的真实闭环

前端新增「已开通客户」选择器（`优链充` / `Ulink`）与「进入工作区」「进入知识库」两个按钮，
本次已上线并在真实浏览器验通：

| 步骤 | 观察 |
|---|---|
| 进入工作区 | 页面工作区头变为 `aiops-41-优链充-56323072`，列表含该租户三个应用 |
| 进入知识库 | 桥记 `knowledge open: target=https://kb.mall.qushiyun.com/__aiops/knowledge/hop?ticket=…`，303 → 该域并种 `Domain=.mall.qushiyun.com` 会话 |
| 落地身份 | 用该会话请求 `/api/v1/users/me`：`email=90c5be3176bf0085@kb.internal`、`id=551c8898…`——正是优链充租户账号 |
| 空间内容 | `/api/v1/datasets`：`{"code":0,"data":[],"total_datasets":0}`——空间存在且为空 |

**环境限制（如实记录）**：开发机到 `kb.mall.qushiyun.com`（41 公网）不可达，浏览器
无法完成最后一跳。因此本次用「浏览器经桥取票 → 在可达的 41 上用同一张票兑换」证明闭环，
没有伪造会话、没有回放他人响应。这证明的是链路与身份，不等于在用户浏览器里目视看过一次。

## 未验收项

- 知识库**上传/解析/Dify 同步**未做：该租户空间为空，ADR-0166 已把这三项列为独立记录项。
- 平台目录授权只在真实组织账号上验通一次（328 个客户、含优链充），未做日常巡检。
- 本轮的恢复是受控维护动作，不是常规路径；后续客户开通不应再需要恢复工具。

## 检查

- `tests/knowledge/test_recover_tenant_login.py` 3 项通过（含新增「写入形状过不了登录判据必须报错」）。
- 新增用例把「写 base64(明文) 而不是明文」钉进断言，旧实现会直接失败。
