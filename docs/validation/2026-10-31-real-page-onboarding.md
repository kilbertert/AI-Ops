# 真实页面双端开通验收（2026-10-31）

> 证据文件。凭据按 runbook 从库外 age 清单读入内存，未打印、未入库；会话 cookie 只在 0600 私有目录。

环境：41 生产网关（本次验收结束时 `1.0.0+330f945379e8`，修复前 `1.0.0+05e82e8ec54e`）；入口 `https://dify.ranlei.work`（36 控制台，公司账号登录代理）。
凭据：库外 age 资产 `aiops-41-platform-dify`，只在内存解密，未打印、未落盘明文。
会话：`__Host-access_token` 已存入 0600 私有目录（`~/.cache/.../real-acceptance/dify-session.json`），不进仓库。

## 已完成（真实、可复核）

| 项 | 结果 | 证据 |
| --- | --- | --- |
| 平台账号真实登录 Dify 控制台 | 通过 | `GET /console/api/account/profile` → 200，`is_password_set=false`、邮箱 `41-1454…@aiops-login.invalid`（登录代理派生）；验证码一次识别正确 |
| 真实会话调同源桥 `/__aiops/manage` | 通过 | `catalogue` 200，`platform=true`，**328 个租户**；`sources/entries/workspaces/operations` 齐全 |
| 进入客户工作区 | 通过 | `workspace` 200 → `aiops-41-Ulink-598861824`，`role=editor` |
| 管家端状态回读 | 通过 | `status` 200：`managed=true, published=true, version_no=23`，已发布提示词与工具 `cpms_order_detail/cpms_charging_samples` |
| 客户端状态回读 | **失败** | `status` 500 `SETUP_UNAVAILABLE` |
| 双端开通提交 | **失败** | `create` 403 `SETUP_SOURCE_NOT_PUBLISHED` |
| 只开管家端开通 | **失败** | 作业 `11d23ca1…` `state=failed, step=operator:failed, error_code=SETUP_STEP_FAILED` |

## 根因（两条，均为代码缺陷，不是账号或授权问题）

### 1. 客户端登记行 `profile_id=''` → `status` 必崩

41 的 `operator_apps` 里客户端那一行是启动纳管写的：

```
394b5399-… | consumer | operator_order_diagnosis | profile_id='' | environment='' | targets_json='{}'
```

三处都错：端是 consumer、场景却是 operator 的、`profile_id` 为空。
`SelfService.status()` 不区分端先取 profile：`config.profiles(settings)[row["profile_id"]]`
→ **`KeyError: ''`**（journal 实证，`self_service.py:274`）。
后果：客户在页面上看**客户端**任何状态都是 500，且双端开通因为这行"不是 consumer 端"被拒。

### 2. 现役 `cpms-node` provider schema 与当前声明不一致 → 开通作业失败

Ulink 工作区里已登记的 `cpms-node` 只有 `cpms_order_detail`/`cpms_charging_samples`；
`operator_tools.json` 现在有三个（#1205 加了 `cpms_site_search`）。
`prepare.dependencies` → `ensure_node_provider(..., migrate=False)` 抛 `ValueError`，
被 `_error_code` 归成 `SETUP_STEP_FAILED`（页面只显示"开通失败"，看不到原因）。

## 未执行（被上面两条挡住）

- 页面真实双端开通（E/K 契约里"页面上点开通并回读 complete"）
- 消费者端与原 H5、员工端与原管家 H5 的真实会话（H/I/J1）
- web 镜像切换：已按合并修订 `86d63d4a` 构建出 `aiops/dify-web-patched:dual-entry-merged-86d63d4a`，**未部署**
  （部署前要先修上面两条，否则页面开通仍然失败）

## 追加：在第二个客户（优链充）上复现，排除"单个客户数据脏"的解释

工作区切到 `aiops-41-优链充-56323072`（`role` 仍未打印，切换接口 200）：

| 动作 | 结果 |
| --- | --- |
| 管家端 `status` | 200，`managed=true, published=true`（源应用 v1） |
| `create` scope=operator | 202 → 作业 `33d291dd…` → `failed / operator:failed / SETUP_STEP_FAILED` |
| 该工作区 `cpms-node` 实读 | **只有 2 个工具**（无 `cpms_site_search`），与 Ulink 完全相同 |
| `create` scope=both | 403 `SETUP_SOURCE_NOT_PUBLISHED`（同一根因：该租户没有 consumer 应用） |

结论不变且更强：两条缺陷是**实例级**的，不是某个客户的数据问题。

- 缺陷 1（`status` 对 consumer 行取 `profile_id=''` → `KeyError`）让任何客户页面的客户端状态 500；
  双端开通的 consumer 分支也会因为该行 `entry` 不是 consumer 而被 `SETUP_SOURCE_NOT_PUBLISHED` 拒绝。
- 缺陷 2（现役 `cpms-node` schema 少 `cpms_site_search`，`migrate=False`）让**任何**管家端开通作业
  在 `operator:dependencies` 步直接失败，页面只显示"开通失败"。

两者都不需要改生产数据即可修：修代码后重跑开通作业即可；但对第二条要**明确决定**是否让开通自动
迁移 provider schema（那是运营动作，`ops_provider.ensure_node_provider(migrate=True)` 就是为它留的口子）。

---

# 结论更新：双端开通已在真实页面完成并发布（2026-10-31 19:5x UTC）

## 通过的部分（真实会话、真实环境）

| 步骤 | 结果 |
| --- | --- |
| 平台账号真实登录（`aiops-41-platform-dify`，密码＋图形验证码） | 200，会话 cookie 落 0600 私有目录 |
| 同源桥 `/__aiops/manage` catalogue | 200，`platform=true`，328 客户 |
| 进入客户工作区 `aiops-41-Ulink-598861824` | 200 |
| 双端开通作业 `44823895…` → 重试后 | **completed**：两端应用各自创建、各自准备依赖 |
| 客户端发布 | 200，`version_no=1`，`chat_route_bindings` active rev 9 |
| 管家端发布 | 200，`version_no=1`，active rev 5 |
| 两端 `status` 回读 | `managed=true, published=true`，版本 1 |
| 现役入口未受影响 | faq/shortcuts 匿名 401，openapi 200，`/v1/runs` 404；三个 unit active |

## 过程中发现并修复的缺陷（4 个 PR）

1. **#1249** 客户端登记行端写错（`entry=operator` + `profile_id=''`）→ 页面读客户端状态
   `KeyError` 500、双端开通被拒。修：端按注册表裁定 + 显式 `repair_entry`；
   `status()` 不再依赖 profile。附带把"这一端没有来源"与"拿错端当来源"分成两个错误码。
2. **#1251** `default-credential` 是**翻转**语义（41 连调三次默认项来回切换）。
   旧代码当"设为默认"用，凭据已默认时恰好关掉 → `SETUP_CONSUMER_PLUGIN_FAILED`。
3. **#1254** 发布白名单只读平台 provider 注册表，忽略 profile 的 `consumer_models`
   → 客户端发布必然 `model is not allowlisted`。
4. 部署侧补齐（非代码）：41 缺 `AIOPS_GATEWAY_CONSUMER_KNOWLEDGE_PLUGIN`；
   两个客户工作区的 `cpms-node` schema 比声明旧（迁移到 3 个工具）；
   profile 增加 `consumer_models: [deepseek-v4-flash]`。

## 仍未完成

- **原 H5 的消费者/员工真实会话**（A–L 的 H/I/J1）：本次只做到"页面开通＋发布"，没有用租户
  手机号在原 H5 发起真实问答。
- **知识对客授权面板**：新客户端应用的 `knowledge` 动作回读 0 个库（开通时新建的
  `对客知识库-…` 未被 `owned_libraries` 列出），需要单独定位。
- **36 的 web 镜像**仍未切换（本机构建 `dual-entry-merged-86d63d4a` 未部署）。
- **渠道声明**（J2）仍为空。
