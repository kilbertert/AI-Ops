# 开通失败可归因：通用码带上步骤与异常种类

日期：2026-11-04。分支 `fix/setup-step-failure-visibility`（PR #1331）。
决策见 [ADR-0200](../adr/0200-generic-failure-must-name-step-and-kind.md)。

## 目标

`SETUP_STEP_FAILED` 以前只有一句通用码，页面上不知失败在哪一步，journal 里也没有栈。
2026-11-04 优链充开通第一轮重试因此**至今无法归因**（记录见
[业务环境清单上线](2026-11-04-business-environment-enable-and-onboarding.md)）。
本片让同类失败下次能查。

## 已完成内容

- 逐端失败：`entries.<端>` 增加 `failed_step`（`save()` 当时的步骤名）与 `failure_kind`
  （异常类名）；作业级异常记在 `resources` 层，`public_job` 一并输出。
- 失败位置逐轮作废：新一轮开始清掉 `resources` 与各端上一次的定位字段，避免"旧故障位置
  冒充这一次"。
- 客户端第一步补 `consumer:source`：来源导出/校验失败时定位落在客户端自己的步骤上，
  而不是上一个端或公共准备那一步（review 指出的两处，均已修）。
- 异常正文与栈只进 journal（`aiops.dify_self_service`），不进任务行、不进公开形状。
- 前端对 `SETUP_STEP_FAILED` 追加「「步骤」出错（`Kind`）」；受控业务码保持原文不动。

## 验证结果

| 项 | 结果 |
| --- | --- |
| `pytest tests/dify/test_self_service.py` | 36 passed |
| `pytest tests/dify/test_self_service.py tests/dify/test_dual_entry.py` | 70 passed |
| `ruff check` / `format --check` | 通过 |
| 公开形状 + 真实服务边界 | 见下 |

`tools/verification/setup-failure-visibility/observe.py` 在一次性数据根运行：

- 公开形状白名单给出 `failed_step` / `failure_kind`，`exception_message` / `traceback` /
  `key_ref` **一个都不出现**；
- 真跑 `aiops-gateway serve`，无会话读作业进度被拒（403），新字段不是新的免鉴权读入口。

## 仍未验

- **没有造一次真实开通失败**：那需要可用的 Dify 控制台与真实来源应用。归因链的端到端
  证明在确定性用例里（逐端失败、作业级失败、重试不继承旧位置、客户端来源步骤四条）。
- **前端文案未部署**：改动在 `deploy/dify-36/web-patches`，需要重建 Dify web 镜像
  （`build-web-image.sh`）。后端字段随 CD 就在接口回读里可见；旧前端忽略未知字段，
  两者不同版不会崩。
- 生产上那次失败的**真实原因仍未确定**，本片只保证下一次能查到。
