# H5 助手面：这个前端调了哪些路径，nginx 就只转哪些

`ops/nginx/ulink-h5-assistant.conf` 的转发清单是**声明**，这份文档说清它是怎么得出的，
以及为什么是这些、不是别的。`tests/test_h5_assistant_routes.py` 断言两边逐条相等 ——
只加一行正则而不动这里，CI 会红。

## 一、事实：H5 聊天页调用的是这 9 条

从 41 上已部署的前端产物里读出来的，不是从公司仓库源码推测的
（`/www/wwwroot/ulink.h5.mall.qushiyun.com/static/js/aiPackage-pages-chat-chat.*.js`）：

```bash
grep -oE 'p\("[^"]*"' aiPackage-pages-chat-chat.<hash>.js | sort -u
```

| 方法 | 路径 | 网关路由 | 方法 |
|---|---|---|---|
| GET | `/v1/faq/recommendations` | `/v1/faq/recommendations` | GET |
| GET | `/v1/faq/catalog` | `/v1/faq/catalog` | GET |
| POST | `/v1/faq/answer` | `/v1/faq/answer` | POST |
| GET | `/v1/shortcuts` | `/v1/shortcuts` | GET |
| GET | `/v1/media/{signed_id}` | 同左 | GET |
| POST | `/v1/assistant/questions` | `/v1/assistant/questions` | POST |
| GET | `/v1/assistant/questions/{qa_id}` | 同左 | GET |
| POST | `/v1/assistant/questions/{qa_id}/cancel` | 同左 | POST |
| POST | `/v1/health-report-jobs` | `/v1/health-report-jobs` | POST |
| GET | `/v1/health-report-jobs/{job_id}` | 同左 | GET |
| POST | `/v1/standard/diagnoses` | `/v1/standard/diagnoses` | POST |
| GET | `/v1/standard/diagnoses/{diagnosis_id}` | 同左 | GET |
| POST | `/v1/standard/diagnoses/{diagnosis_id}/cancel` | 同左 | POST |

ID 形状取自网关自己的生成处（`gateway_store.py`）：`qa_` + 32 位十六进制、
`hrj_` + 32 位、`dx_` + 32 位。正则按这个形状限死，不写 `[^/]+` —— 后者会把
`/v1/assistant/questions/../../admin` 这类路径也放进来。

WF01新增签名素材路径来自`MediaResourceSigner.issue`：`media_`+32个base64url字符+
`.`+24位十六进制签名。只转GET，签名及当前QA/发布版本仍由网关验证；错误签名不能获得素材。
原H5回答中的相对图片URL依赖这条同源转发，缺失会落静态首页。真实nginx同时验证
有效形状可达、截短/加长/非素材ID及POST/DELETE不转发。生产照片输入仍关闭，不增加上传路由。

同目录的 `aiPackage-*` 里还有 `/v1/signin/anonymously` 与 `/v1/user/me`：
**那不是我们的路由**（网关 `/v1/` 下没有这两条），属于公司业务面，不在本片。

## 二、为什么不整片转发 `/v1/`

网关 `/v1/` 下还有发布、注册表、Dify 运维、知识源写入等端点。整片转发等于把
消费者站点变成一个到全网关的通道，而这次要修的只是"聊天页打不通"。窄转发让
**没声明过的路径继续 404/405**，这本身是可回归的判据（见测试）。

## 三、身份怎么来的，以及它挡不住什么

| 头 | 来源 | 说明 |
|---|---|---|
| `Authorization` | `/etc/aiops-41/nginx-aiops-service-token.conf` | 服务令牌，页面拿不到也改不了 |
| `X-Third-Session` | 透传浏览器 | 用户自己的会话，网关解析成个人范围 |
| `X-Business-Entry` | **固定 `consumer`** | 页面自报的入口值被覆盖，抬不成管家端 |
| `X-AIOps-Source-Key` | **清空** | 这一跳不是公司网关，不带来源密钥 |

网关每次请求都重新解析身份与权限，nginx 这里不产生任何授权判断。
`proxy_next_upstream off`：诊断面是 `POST` 且会创建作业，失败自动重发会造出第二个作业。

## 四、这篇文档的边界

- 证明的是「H5 同源 `/v1/` 请求会到达网关」。**不证明**聊天的业务答案正确。
- 不涉及 `App`/`BFF` 的接线，也不代表 M3 整线完成。
- 装到宿主上是**产物部署**，不是 CD：走 `dev-host write` 写入后 `nginx -t` + reload，
  并按 `deploy/manual-deploys.md` 留痕。
