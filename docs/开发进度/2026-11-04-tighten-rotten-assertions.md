# 2026-11-04：收紧四处范围过宽的测试断言

## 目标

#1350 的 Devin Review 四条全部成立：断言的范围比它声称的大 —— 删掉实现之后测试仍然全绿。
逐条收紧到「删掉实现即红」。

## 已完成内容

| 文件 | 改前 | 改后 |
|---|---|---|
| `test_diag_shared_contract.py` | 整份 controller 找一个 `validateToken(` | 按 `@GetMapping` 切端点体，逐个断言 header / 校验 / 401 |
| `test_dify_login_page.py`（redirect） | 页面上有 `".redirect"` 即可 | 解析提交请求体，必须有 `redirect` 字段 |
| `test_dify_login_page.py`（刷新） | `page.count(...)` 把函数定义算进 3 次 | 只数调用行 |
| `test_dify_registry_route_proof.py` | 任意带「不可区分」的 `.append` | 限定接收者是 `failures` |

同时订正里程碑记录的行数为实测的 +331 / -377。

## 当前分支或提交

`test/tighten-rotten-assertions`，基线 `49742191`。

## 验证结果

四条各做一次反向验证（改掉生产代码 → 红 → 恢复），全部实测。
`uv run pytest tests/integrations/test_diag_shared_contract.py tests/dify/test_dify_login_page.py tests/dify/test_dify_registry_route_proof.py`：20 passed。

## 已知缺口

- 这四条修复最初与一个后来被 revert 的提交混在一起，重建分支时丢失，经合并后核实才发现；
  本轮以独立分支补交。
- **真实故障业务验收：未完成**（只改测试）。
