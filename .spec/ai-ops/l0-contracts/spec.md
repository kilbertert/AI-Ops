---
title: l0-contracts
desc: 通用配置、有界传输、私有文件与脱敏
code:
  - src/aiops_diagnostics/config.py
related:
  - src/aiops_diagnostics/bounded_http.py
  - src/aiops_diagnostics/redaction.py
  - src/aiops_diagnostics/private_files.py
  - src/aiops_diagnostics/i18n.py
  - src/aiops_diagnostics/platform_paths.py
  - src/aiops_diagnostics/http_auth.py
  - src/aiops_diagnostics/console_encoding.py
---
# l0-contracts

## raw source

退役 Python 执行器后，共享配置和安全原语继续服务现役业务。

## 不变量

- 出站 HTTP 复用 bounded_http；超时、响应上限、拒绝重定向保持。
- 私有文件校验和凭据脱敏不随旧运行器删除。
- 模型配置只支持现役单次请求；没有 Codex 二进制、工作区或恢复参数。
