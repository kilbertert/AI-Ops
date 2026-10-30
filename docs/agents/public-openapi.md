# 用户 OpenAPI 部署与核验

规范由 `src/aiops_diagnostics/gateway/public_openapi.py` 从允许公开的路由和请求模型生成。
`GET /v1/openapi.json` 是 OpenAPI 3.1 JSON，`GET /v1/docs` 是不加载外部脚本的中文查阅页。
导入测试工具后按规范选择个人身份。媒体签名URL不附个人令牌；管理和回调不在公开清单中。

## 交付

代码通过同一PR的确定性CI后合并，现有CD调用 `deploy/deploy-41.sh` 交付Gateway。
本轮已只读确认API域名的现有`/v1/`代理可承接，无需新增Nginx配置或开放端口。
不把H5同源代理的路径白名单扩大成整个API面；测试工具使用API基址。
部署失败沿用CD和[41手册](env-41-runbook.md)的恢复流程，不直接修改运行代码。

## 可重复核验

本地运行 `uv run pytest tests/gateway/test_public_openapi.py`，并按
`verify-aiops-gateway` 技能启动一次性数据根的真实Gateway。
通过回环HTTP读取下列两个新地址，另验无凭据业务请求仍401；结束后关闭该临时进程。

部署后只发下列GET，不提交问题、不读业务数据、不调用模型：

```bash
curl --fail --max-time 20 https://api.mall.qushiyun.com/v1/openapi.json -o /tmp/aiops-public-openapi.json
python -c 'import json; d=json.load(open("/tmp/aiops-public-openapi.json")); assert d["openapi"]=="3.1.0"; print(sum(len(v) for v in d["paths"].values()))'
curl --fail --max-time 20 https://api.mall.qushiyun.com/v1/docs -o /tmp/aiops-public-api-docs.html
curl --max-time 20 -o /dev/null -w '%{http_code}\n' https://api.mall.qushiyun.com/v1/faq/catalog
```

预期：规范23个操作；HTML链接指向同源规范且无外部脚本；最后请求401。
规范有效性用独立 `openapi-spec-validator` 校验；无需将它加入运行时依赖。
保留构建/合并修订、时间、HTTP状态与规范摘要。JSON有效不等于全部业务能力已验收。
