---
title: tdengine-proxy
desc: loopback-only 的 TDengine 严格只读 SQL 白名单代理
hue: 0
code:
  - src/aiops_diagnostics/tdengine_proxy.py
---
# tdengine-proxy

SQL 进入 TDengine 的唯一门。它只监听回环，只放行白名单形态的只读查询。

## raw source

这是 ADR-0001「只读诊断边界」在数据面最直接的兑现。它的存在使"只读"不是一句承诺，
而是一道**进程边界**：绕过它需要拿到主机上的执行权，而不是构造一条聪明的 SQL。

## 不变量

- 只监听回环地址——它不对网络暴露，暴露它等于暴露数据库。
- 白名单是**白名单**：未列出的形态拒绝，不是"过滤掉明显危险的"。
- 只读：任何写形态的语句在白名单之前就被拒。
