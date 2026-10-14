# Dify 控制台里的「发布到 AI-Ops」按钮（#577 的最后一跳）

**这是什么**：在 Dify 控制台的编排页注入一颗按钮，运营点它就把**当前改好的内容**
冻结成一个已发布的线上版本。**不用 SSH、不用敲命令。**

决定的论证在 **ADR-0021**；后端桥在 `src/aiops_diagnostics/dify_console_publish.py`。
本目录只是**部署资产**。

## 四件东西，装在哪

| 文件 | 装到 | 作用 |
|---|---|---|
| `dify-console-10008.conf` | `/www/server/panel/vhost/nginx/` (36) | 宿主 nginx 的 10008 server 块：转发 Dify + 注入 |
| `dify-console-publish.conf` | `/www/server/panel/vhost/nginx/snippets/` | 桥的同源转发（含 cookie 与 CSRF 头） |
| `dify-publish-button.js` | `/srv/aiops-console-assets/` | 注入进页面的脚本。**不含任何凭据** |
| `aiops-dify-console-bridge.service` | `/etc/systemd/system/` | 桥的服务单元（身份 `aiopsbridge`） |

## 拓扑

```
浏览器 ──► 36:10008（宿主 nginx，本目录的第一个 conf）
             ├─ /            ──► 127.0.0.1:10018（Dify 自己的 nginx）
             ├─ /__aiops/publish-button.js  ← 注入脚本从这里出
             └─ /__aiops/publish            ──► 127.0.0.1:10118（桥）
```

**Dify 的发布端口必须收进回环**：`deploy/docker-compose.yaml` 里
`127.0.0.1:${DIFY_EXPOSE_PORT:-10018}:80`。绑 `0.0.0.0` 会让 10018 成为绕过注入层的
第二条路。

## 三处**容易踩空**的地方（都是实测踩过的）

1. **CSRF 头必须转下去。** Dify 的 `/console/api/*` 除了 cookie 还要
   `X-CSRF-Token`，少了它一律 401 `CSRF token is missing or invalid` ——
   桥会把它误报成"会话失效"。桥和 nginx snippet 两处都要带。
2. **注入脚本不能放在 `vhost/` 目录下。** 那两级目录是 `drw-------`（宝塔私有区），
   nginx 的 worker **走不进去**，取脚本时 403。放 `/srv/aiops-console-assets/`。
3. **`sub_filter` 要求上游不压缩。** 所以 `proxy_set_header Accept-Encoding ""` +
   `gzip off`；少了这两行，替换**静默失败**（看着配了其实没注入）。
   另外脚本里的符号全部带 `aiops` 前缀 —— 第一版直接叫 `publish`/`toast`，
   与 Dify 自己的 bundle 撞名，报 `publish is not a function`。

## 桥读什么

`/etc/aiops-console-bridge/bridge.env`（**0600 root**）：

```
AIOPS_GATEWAY_DIFY_PUBLISH_API_KEY=…    # 服务端持有；**不进浏览器**
AIOPS_CONSOLE_BRIDGE_DIFY_URL=http://127.0.0.1:10018
AIOPS_CONSOLE_BRIDGE_GATEWAY_URL=https://api.mall.qushiyun.com
AIOPS_CONSOLE_BRIDGE_HOST=127.0.0.1
AIOPS_CONSOLE_BRIDGE_PORT=10118
```

**为什么桥走 `/v1/dify/publish`（共享密钥）而不是 `/v1/operator/publish`**：
后者认的是「运营的**网关**会话」，而桥手上只有运营的**Dify** 会话 —— 两者换不通。
桥的形态是 BFF：**先把"人"验明白（拿 Dify 会话问 Dify），再以"机器"的身份把事做完**。
两条门各司其职，最终都走同一个 `_publish_bound_app`。

## 失败的方向

**注入不上 = 没有那颗按钮，控制台照常。** 挂靠靠 DOM 选择器，Dify 改版可能失效 ——
那时按钮消失，但 Dify 本身不受影响，发布仍可用 `/v1/dify/publish` 或 CLI。
