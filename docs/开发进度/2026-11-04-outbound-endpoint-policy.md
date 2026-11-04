# 2026-11-04：出站端点 URL 安全规则收成一处（#754）

## 目标

「https 哪里都行，http 只给回环」被逐字抄了五份、三类带凭据端点整条缺失、「回环」有
两种矛盾渲染。抽成 `common/endpoint_policy.py` 一处定义，五份副本与缺失项各就位。

## 已完成内容

- `is_loopback_host` / `require_secure_endpoint`（ADR-0213）。
- 五份副本改调；UPMS / Dis / kb-service / 发布校验 / Dify console 补齐明文检查；
  `tdengine_proxy` 的 upstream 判定复用。
- 新增 `tests/common/test_endpoint_policy.py`（39 条），含跨端点一致性。

## 当前分支或提交

基线 `9b24aad7`。

## 验证结果

- `tests/common/test_endpoint_policy.py`：39 passed
- 反向验证：删掉 `localhost` 分支 → 3 条红
- `ruff check` / `ruff format --check`：通过

## 已知缺口

- **未部署**；未在 41 上做远程端点配置验收。
- `tdengine_proxy.listen_host` 仍按 IP 判回环（要求必须是 IP，语义不同）。
