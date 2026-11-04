# 2026-11-04：出站端点 URL 安全规则收成一处（#754）

**里程碑目标**：「https 哪里都行，http 只给回环」此前五份副本 + 三类带凭据端点缺失；
「回环」有两种互相矛盾的渲染。收敛成 `common/endpoint_policy.py`。

## 已完成内容

- `is_loopback_host`（两种旧渲染的并集）、`require_secure_endpoint`（ADR-0213）。
- 五份副本改调：`config.canonical_provider_base_url`、`caller_auth.IntrospectionSettings`、
  `company_token_auth.CompanyTokenSettings`、`gateway_config.canonical_gateway_url`、
  `consumer_flow_contract`（这一份原本还漏了 `::1`）。
- 补齐明文半条：`UpmsSettings`、`DisSettings`、`KbServiceClient`、
  `KbServiceKnowledgeClient`（此前**完全不做 URL 校验**）、`DifyDslClient`。
- `tdengine_proxy` 的两处回环判定改用同一函数。

## 验证结果

- 新增 `tests/common/test_endpoint_policy.py`（39 条）：回环定义逐项、`127.0.0.2` /
  `localhost` 两个旧渲染分歧输入、九个端点对同一远端明文 URL **一律拒绝**、
  对同一回环明文 URL **一律接受**、全仓不再有第二份字面量集合。
- 反向验证：删掉 `localhost` 分支 → 3 条红；删掉整个 `is_loopback_host` 的并集 → 同上。
- 因补齐明文半条而改动的既有测试：`tests/gateway/test_media_api.py` 的
  `KbServiceClient("http://kb.local", …)` 改为回环地址（它原本钉的是**缺失**）；
  `tests/identity/test_company_token_auth.py` 的 `UpmsSettings(base_url="http://upms.invalid")`
  改为回环地址（原本不需要 URL 合法）。

**真实故障业务验收：未完成** —— 未在 41 上做真实远程端点配置验收；本轮证据是
「九个端点对同一输入给出同一个答案」，不是「线上曾发生明文泄露」。

## 已知缺口

- **未部署**。
- `docs/gateway.md` 的 `validation:` 段落描述的仍是旧行为（本 PRD 不宣称线上已明文，
  也未改任何端点当前 URL）。
- `tdengine_proxy` 的 `listen_host` 仍用 `ipaddress` 判回环（它要求**必须是 IP**，
  语义不同：监听地址不接受主机名），本轮只统一了 upstream 那一处。
