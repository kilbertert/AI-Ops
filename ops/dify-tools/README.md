# 七工具目录在 Dify 侧的注册（#774 / #775）

> **这是什么**：把 `aiops-tools.openapi.json` 注册成 Dify 工作区里的一个 **API 工具
> 提供方**，名字必须叫 `aiops`。注册之后，运营在智能体编排页的工具列表里就能选到
> AI-Ops 的七个真实诊断工具；选了哪些，由 `aiops` 提供方的 `operationId` 直接对齐，
> 经发布门进智能体版本的白名单（#774 / ADR-0019）。
>
> 本文件只写**怎么注册**与**为什么这么填**。端点本身的行为见
> `docs/gateway.md` 与 `ops/dify-exposure-registry.md`；暴露面的论证见
> `deploy/dify-36/README.md` §2.1。

## 一、四步

以**应急 owner 会话**在控制台里做（不需要 SSH；#778 的自签会话 API 同样可用）：

1. 打开 **集成 → Swagger API 作为工具 → 添加 Swagger API 作为工具**。
2. **Schema**：把本目录的 `aiops-tools.openapi.json` 内容粘进去。
   **粘贴前把 `servers[0].url` 的占位符 `https://<AI-Ops 网关域名>` 换成真实域名**
   （本环境是 `https://api.mall.qushiyun.com`）。仓库里这份保持占位符，是因为域名属于
   环境事实、不是版本化契约；每处部署各自填自己的。
3. **鉴权方式**：`Header`（即 `api_key_header`）。
4. **三个字段必须这样填**（第三个是本目录存在的主要理由）：

   | 字段 | 值 | 说明 |
   |---|---|---|
   | `api_key_header` | `Authorization` | 默认值就是这个，但**显式填**，别依赖默认 |
   | `api_key_header_prefix` | **`bearer`** | ⚠️ **默认是 `basic`，见下节** |
   | `api_key_value` | `<AIOPS_GATEWAY_DIFY_TOOLS_API_KEY 的值>` | **不带 `Bearer ` 前缀** |

## 二、为什么 `api_key_header_prefix` 必须是 `bearer`（踩过的坑）

Dify 的凭据组装（`core/tools/custom_tool/tool.py` 的 `assembling_request`）分两步：

```python
api_key_value = credentials["api_key_value"]
if "api_key_header_prefix" in credentials:
    prefix = credentials["api_key_header_prefix"]
    if prefix == "basic" and api_key_value:
        api_key_value = f"Basic {api_key_value}"
    elif prefix == "bearer" and api_key_value:
        api_key_value = f"Bearer {api_key_value}"
headers[api_key_header] = api_key_value
```

而这个字段的 schema 默认值是 **`basic`**（`custom_tool/provider.py` 的
`ProviderConfig(name="api_key_header_prefix", default="basic", ...)`），并且
`validate_credentials_format` 会在**调用时**把缺失的字段补成默认值。

于是：**把 `Bearer <key>` 填进 `api_key_value`、又没设 prefix，Dify 会发出
`Basic Bearer <key>`** —— 网关认的是 `Authorization: Bearer <key>`，前缀对不上，
一律 **401 `DIFY_CREDENTIAL_REQUIRED`**。而且这个错误发生在**调用时**、不在注册时，
所以注册看起来是成功的、工具列表也正常显示，只有真去调用才炸。

**判据（怎么一眼看出是不是踩了这个坑）**：网关回 `401 DIFY_CREDENTIAL_REQUIRED`
（= 没收到可识别的 `Bearer ` 头），而不是 `403 DIFY_CREDENTIAL_REJECTED`（= 收到了
但值不对）。401 指向组装，403 指向密钥本身。

## 三、注册完之后怎么验证（不点界面也能验）

`POST /console/api/workspaces/current/tool-provider/api/test/pre` 是 Dify 自己的
可用性检查端点，走的是与界面**同一条**凭据组装路径。用它会话调用一次：

```jsonc
// body（credentials 与上面三个字段一致）
{"tool_name": "order_snapshot", "provider_name": "aiops",
 "credentials": {"auth_type": "api_key_header", "api_key_header": "Authorization",
                 "api_key_header_prefix": "bearer", "api_key_value": "<key>"},
 "parameters": {}, "schema_type": "openapi", "schema": "<文档内容>"}
```

七个 `tool_name` 各调一次，`result` 里应各自出现
`{"tool": "<name>", "note": "本工具只在 AI-Ops 运行时执行，请使用试运行。…"}`。

**这一步通过 = 全链路通**（文档解析 → 凭据加密存储 → 前缀组装 → 出站经 squid →
网关鉴权 → 固定说明）。2026-10-15 实测 **7/7 通过**。

## 四、密钥的纪律

- 这把钥匙由 `AIOPS_GATEWAY_DIFY_TOOLS_API_KEY` 配置（**留空 ⇒ 整组路由 404**）。
- 它是 Dify 侧**工作区共享**的：任何能编辑该提供方的管理员都看得见。
  端点的设计前提就是"泄露了也拿不到东西" —— 除了一段固定说明文本，它不读任何数据源、
  不创建运行。这是它敢用共享密钥的原因，也是它**必须**与检索/调试/发布/控制台四把
  钥匙互不相同的原因（启动期校验，同值即拒绝启动）。
- 注册进 Dify 的是一份**加密存储**的副本；轮换时两边都要改。
