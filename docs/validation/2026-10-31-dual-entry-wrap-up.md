# 双端开通收尾（2026-10-31 晚）

承接 [真实页面双端开通验收](2026-10-31-real-page-onboarding.md)。本文记**收尾三项**的实际结果。

## 1. 知识对客授权面板：已修（部署落后，不是代码缺陷）

页面读客户端知识库返回 0 个库。根因不是 `owned_libraries`，而是**36 上的同源桥还是旧包**：
部署在 `/opt/aiops-console-bridge` 的 `self_service_bridge.py` 没有 `knowledge` /
`knowledge-access` 两个动作，桥直接回 `400 无效管理动作`，页面按空列表渲染。

处理：按 `ops/dify-console-bridge/README.md` 用 `build-bundle.sh origin/main` 重新打包
（`REVISION=e0864bdf…`），`dev-host cp` 上传并 `deploy.sh` 安装，桥与 nginx 同版切换。

复测（同一真实会话）：

```
knowledge: 200  libraries: 9  allowed: 8
```

仍有一项事实需要记：**每次失败的开通都留下一个孤儿知识库**（`对客知识库-<hash>`），
本次 9 个库里 8 个是历次失败留下的，只有 `188c54f4…` 被现役客户端路由引用。
清理这些库属于运营动作，未在本次执行。

## 2. 36 的 web 镜像：已切换并验证

之前只构建未部署。本次：

- 确认自 `79acaf4c`（#1240 合并）以来 `deploy/dify-36/web-patches/` **没有变化**，
  所以用该修订构建的 `aiops/dify-web-patched:dual-entry-merged-86d63d4a` 与当前 main 的
  前端补丁一致；
- `docker save` + zstd 上传（sha256 `7d7bae26…`），36 `docker load`；
- 备份 `/opt/dify/deploy/.env` 后把 `DIFY_WEB_IMAGE` 指向新镜像，`--no-deps --force-recreate web`。

回读：`/signin` 200、`/` 307、`x-version: 1.17.1`；登录会话仍有效；两端 `published=true`。
回滚：`/root/dify-env.bak-web-20261031` 与原镜像 `aiops/dify-web-patched:knowledge-1be3f067`。

**真实页面证据（截图在本机私有目录）**：

- 客户端（Chatflow）编排页出现受管工具条：`在 AI-Ops 试运行` / `撤回正式版本` /
  `恢复历史版本` / `发布到客户端客服` + `版本操作`，并显示
  「该端尚未接入使用渠道」；右侧知识库绑定为 `对客知识库-a9a6bd4c…`。
- 管家端（Agent）编排页显示 `发布到管家` / `版本操作`，预览按钮因无公司身份而禁用
  （`请使用该客户的公司账号预览订单`）。

## 3. 渠道声明（J2）：按"无声明即待接入"已满足

`/etc/aiops-41/gateway.env` 没有 `AIOPS_GATEWAY_ENVIRONMENT_MANIFEST_FILE`，
`ops/environments/env-41.toml` 也没有 `[[channels]]`。`dual_entry.channels()` 在文件缺失时
返回空表，于是每一端都是 `pending`。实测：

```
operator channel='pending' note=None
consumer channel='pending' note='该端尚未接入使用渠道' readiness='pending'
```

这正是 J2 要的形状：**AI 已准备但渠道未接入时显示待接入，不显示为可用**。
没有去写 `[[channels]]`：写"某租户某端已接入"是一条需要真实渠道证据的断言，
本次没有该证据（见下条）。

## 4. 原 H5 真实消费者会话：**未完成**，卡在公司 mall API

按 `runtime-retirement-operations.md` §21 用原 H5 手机号验证码登录，过程与结果：

1. 开发机连不上公司公网域名，经 41 建 SSH 隧道后在真 Chromium 里打开
   `ulink.h5.mall.qushiyun.com`，页面正常。
2. 手机号 + 验证码流程走到 OTP 页；`/mallapi/phone/code?type=1&phone=…&areaCode=86`
   发码成功（Redis `ver_code_default::<phone>` 有值，TTL 以分钟计）。
3. 登录调用 `/mallapi/userinfo/phone/login`（真实字段
   `{phone, code, areaCode, deviceId}`，从页面 bundle 读出）返回
   **`{"code":1,"msg":"SQL错误！"}`**，`cloud-mall-api` 日志里是
   `java.sql.SQLException`。这是**公司商城后端**的缺陷，不在本仓范围。

因此消费者/员工的真实问答（A–L 的 H/I/J1）仍未执行。没有用任何在线真实客户的会话代替，
也没有把这条写成通过。

## 仍未做

- 原 H5 的消费者/员工真实会话（阻塞于公司 mall API 的 SQL 错误）。
- 孤儿知识库清理（8 个 `对客知识库-<hash>`）。
- L1「重启后可用」只在本轮改配置后重启验证过一次，没有做「重启前后同一作业状态」的对照。
