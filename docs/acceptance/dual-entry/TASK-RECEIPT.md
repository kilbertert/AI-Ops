# 任务回执：双端开通（2026-10-31，Codex → Claude 接力）

本文件是**一个任务的完整回执**，取代散落在 `RESUME-RECEIPT.md` / `EXECUTION-RECEIPT.md` /
`2026-10-31-*.md` 里的过程记录。那些文件保留作为证据，但**读这一份就能知道做完了什么、
没做完什么、为什么**。

## 0. 任务是什么

原需求：**「平台为客户开通一套 AI 服务，客户自己管理，消费者和员工分别使用」**。

拆开来是四件事：平台侧开通（双端：客户端客服 + 管家）、客户在自己工作区里管理（提示词、
知识库使用范围、发布/撤回）、消费者端可用、员工端可用。

起始点：Codex 会话 `01a180d5` 做到第三批推送时中断（并发工作区锁定），留下六片修复的
交付计划与本地提交。本会话从那里接手。

## 1. 交付总览

11 个 PR 全部合并，其中 **6 个是代码**、**5 个是回执/证据**。

### 代码（6 个，+1498 / -176 行，含测试）

| PR | 合并 | 内容 |
| --- | --- | --- |
| #1238 | `f168bf53` | 客户管理自己的知识对客授权（桥 + API + 3 项用例） |
| #1239 | `5ab4e4cf` | 启用前核对运行节点协议、认证能力报告（+ Java 侧上报） |
| #1240 | `79acaf4c` | 真实工作流页的受管发布/预览/知识使用范围（前端补丁 + 15 项浏览器用例） |
| #1249 | `186a9e43` | 客户端登记端写错导致页面 500 与开通被拒；provider 旧 schema 给出可操作错误码 |
| #1251 | `593428ec` | `default-credential` 是**翻转**不是"设为默认" |
| #1254 | `330f9453` | 发布白名单包含 profile 已批准的客户端模型 |

后三个（#1249/#1251/#1254）都是**在真实环境里跑出来的**缺陷，不是读代码发现的。

### 回执与证据（5 个，纯文档）

`#1241` 六片逐片交付 · `#1243` 节点升级与 CD 恢复 · `#1255` 真实页面验收与四个缺陷 ·
`#1258` 收尾三项 · `#1261` 孤儿库清理与原 H5 登录根因。

## 2. 真实环境里实际发生了什么

### 2.1 登录与开通（完成）

用库外资产 `aiops-41-platform-dify` 正常登录 `https://dify.ranlei.work`
（密码 + 图形验证码，ddddocr 一次识别正确），经同源桥 `/__aiops/manage`：

- 进客户工作区 `aiops-41-Ulink-598861824`；
- 双端开通作业 `44823895…` → **completed**；
- 客户端发布 200 `v1`（路由 rev 9）、管家端发布 200 `v1`（路由 rev 5）；
- 两端 `status` 回读 `managed=true, published=true`。

真实页面截图确认（本机私有目录，不入仓）：客户端 Chatflow 页有
「在 AI-Ops 试运行 / 撤回正式版本 / 恢复历史版本 / 发布到客户端客服」工具条与知识库绑定，
并显示「该端尚未接入使用渠道」；管家端显示「发布到管家 / 版本操作」。

### 2.2 过程中修掉的真缺陷

| 症状 | 根因 | 判据 |
| --- | --- | --- |
| 页面读客户端状态 500 | 启动纳管把客户端行写成 `entry=operator` + `profile_id=''`，`config.profiles()['']` 抛 KeyError | journal 里 `self_service.py:274` 的 `KeyError: ''` |
| 双端开通 403 | 同一行的端不对，`source["entry"] != "consumer"` | 两端分别单独提交对照 |
| 客户端发布 `model is not allowlisted` | 白名单只读平台 provider 注册表（只有管家模型），不读 profile 的 `consumer_models` | 真实发布 500 + 栈 |
| 开通反复 `SETUP_CONSUMER_PLUGIN_FAILED` | `default-credential` 在 Dify 1.17.1 是**翻转**；旧代码当"设为默认"用，已是默认时恰好关掉 | 同一 id 连调三次，默认项来回切换 |
| 开通作业 `SETUP_STEP_FAILED`（无原因） | 现役 `cpms-node` schema 比声明旧，`ValueError` 被归成通用码 | 两个客户工作区实读都是 2 个工具 |

每一条都先有真实环境证据，再有测试；后三条的测试替身原本**掩盖**了缺陷（例如替身把翻转
写成了"设为默认"），修的时候一并把替身改成与上游同语义。

### 2.3 部署侧（非代码）

- 41 补 `AIOPS_GATEWAY_CONSUMER_KNOWLEDGE_PLUGIN`；profile 增加
  `consumer_models: [deepseek-v4-flash]`；
- 两个客户工作区的 `cpms-node` schema 迁到 3 个工具（运营动作，逐工作区执行并回读）；
- 节点从 `ae290a0d` 升到 `5ab4e4cf`，**回调 nginx 同版重渲染**（旧配置没有 capabilities 路由，
  节点上报一律 404）；
- 36 重装同源桥（旧包缺 `knowledge`/`knowledge-access`）与 web 镜像
  `dual-entry-merged-86d63d4a`。

### 2.4 一次自己造成又恢复的事故

改 profile 时 `shutil.copy2` 让文件属主变成 root，网关被私有文件校验拦下
（`PrivatePathError`）。已改回 `aiops41:aiops41` 600 并确认恢复。回滚点都在。

## 3. 验收结果（A–L 矩阵）

| 项 | 状态 | 依据 |
| --- | --- | --- |
| A/B 目录与双端分配 | **契约级通过**，真实页面已跑通一次 | 真实会话 catalogue + 开通作业 completed |
| C/D 逐端准备与失败隔离 | **完成** | 真实作业里两端各自准备；契约用例覆盖失败隔离 |
| E 受管预览与停止 | **契约级通过**，真实页面工具条在 | #1237/#1240 + 截图 |
| F 发布/撤回/恢复 | **真实页面通过** | 客户端与管家端各发布 v1，路由 active |
| G1/G2 命中本客户对客库 / 内部库不可检索 | **契约级通过** | 真实会话未覆盖（见 §4） |
| G3 跨客户拒绝 | **完成** | 契约用例 |
| H 消费者只读本人数据 | **未执行** | 阻塞于公司商城后端 |
| I 管家站点范围 | **未执行** | 同上 |
| J1 已接入渠道完整结果 | **未执行** | 同上 |
| J2 未接入渠道不谎报 | **通过** | 两端 `channel=pending`，客户端有「尚未接入使用渠道」 |
| K1–K3 权限边界 | **契约级通过** | 篡改请求被拒用例 |
| L1 重启后可用 | **部分** | 改配置后重启验证过；未做重启前后同作业对照 |
| L2/L3 执行器兼容 | **完成** | 节点报告回读，`tools_sha256` 与声明一致 |

## 4. 没做完的，和为什么

**1. 原 H5 真实消费者/员工会话（H/I/J1）——卡在公司商城后端，不在本仓**

对照实验：用**不存在的手机号**登录，得到与我们资产**一模一样**的错误。`cloud-mall-api` 真实异常：

```
Field 'sysem_id' doesn't have a default value
ThirdSession is null → NullPointerException
UserInfo(..., sysemId=null, tenantId=null, nickName=tourist138000, ...)
```

后端为手机号走"不存在则注册"，插入时 `tenant_id`/`sysem_id` 落 null。**任何手机号都过不去。**

**2. 我们自己的一个待修**：`_run_consumer` 每轮重试都先建库再做事，失败后留下空库。
本轮清了 4 个产物（9 → 5），但没改代码，下次失败还会再留。

**3. 渠道声明**：需要真实渠道证据才能写 `[[channels]]`，没有就不写。

**4. 资产描述陈旧**：`aiops-ulink-phone-20261030` 的 `verification` 写"固定验证码 8888"，
线上 Redis 存随机 4 位码。应在商城后端修好后一并订正。

## 5. 现在的生产状态

| 位置 | 值 |
| --- | --- |
| 41 Gateway | `1.0.0+4ec55c7a1c0c`，`business_mutations=disabled`，health ok |
| 41 节点 | `5ab4e4cf6a89-6ffad7414d2b` |
| 41 units | gateway / node-runtime / node-callback 全 active |
| 36 控制台 | web `dual-entry-merged-86d63d4a`，桥含 `knowledge-access`（3 处） |
| Ulink 路由 | consumer `88fc20bd` v18（原客服）与 `07d15c42` v1（本次开通）均 active；operator `137cc417` v23 与 `2b0629cf` v1 均 active |
| Ulink 知识库 | 5 个，4 个允许对客；授权行 4 active / 4 revoked |
| `origin/main` | `1ca11669` |

**现役流量未受影响**：faq/shortcuts 匿名 401、openapi 200、`/v1/runs` 404，
`business_mutations=disabled` 全程未变。

## 6. 回滚点

- 41：`/root/gateway.env.bak-onboarding-20261031`、
  `/etc/aiops-41/operator-self-service/profiles.json.bak-consumer-model-20261031`、
  `/etc/aiops-node-runtime/runtime.env.bak-20261031-1733`、
  `/etc/aiops-node-runtime/gateway-callback.conf.bak-20261031`、
  旧节点发布目录 `releases/tools-964dbf353f73`
- 36：`/root/dify-env.bak-web-20261031`、`/root/bridge-backup-20261031`、
  原镜像 `aiops/dify-web-patched:knowledge-1be3f067`

## 7. 下一步建议（按优先级）

1. 修 `_run_consumer` 建库时机（我们这边，可立即做）。
2. 报商城后端 `sysem_id`/`tenant_id` 落库问题，修好后补做 H/I/J1。
3. 有真实渠道证据后再写 `[[channels]]`。
4. 订正资产里"固定验证码 8888"的陈旧描述。
