# 生产启用 slots：执行回执（待 Codex 复核与决断）

日期：2026-10-31　分支：`feat/prompt-slots-production`　生产版本：`1.0.0+4ec55c7a1c0c`

**结论先行：未交付。** 迁移未生效、页面闭环未完成、原 H5 未验。
下面按「已做 / 实测发现 / 我的误判 / 阻塞 / 需要决断」分列。

## 一、本轮做了什么

全部为**只读**，除操作者在浏览器手改草稿外，我没有写任何生产配置：

1. 开工核对：`main=68ab9207`、41 `/health=1.0.0+4ec55c7a1c0c`。
2. 目标应用**订正**：`dify_app_registry` 写的是 `394b5399`（10-25），
   但**生效绑定**在 `chat_route_bindings` 是 `07d15c42`（revision 9，10-31 12:15:19，
   `updated_by=1454348737375174658` 公司用户）。以绑定为准。
3. 真实知识库授权核对（受管入口，非夹具）：目录 5 个库、对客授权 4 个；
   `41(1)` 无对客授权行属内部库。
4. 离线生成两个候选并做结构等价核对。
5. 操作者浏览器完成一次纯追加编辑；我据库与日志核对结果。

## 二、实测发现（按严重度排序）

### 1. 迁移未生效：缺 `answer_fact_contract`

草稿已被编辑（SYSTEM 906 → 2496 字符，协议块在内），但
`environment_variables` 仍只有 `cpms_status_enabled / cpms_billing_enabled /
knowledge_answer_mode`。**缺 slots 标记 ⇒ 网关仍按 legacy 判**，协议块目前不生效。

### 2. 三次预览实际只跑了两次，且都不是知识问题

`chat_dify_runs` 今日仅 **2 条**，`kind=service_trial`、**`data_kind=status`**：

```
tr_3cf94e08…  15:32  status  not_found  sources=0
tr_fa1ba43d…  15:42  status  not_found  sources=0
```

- 两条 **question 都是「怎么开始充电？」**（`followup.topic`），但 `data_kind=status`；
- 第 2、3 次「订单/澄清」**没有任何运行记录**——组件在 `trial` 调用阶段就没成功，
  Dify 侧 14:51 之后也没有 `workflow_runs`。

### 3. “本次预览未完成”是我方组件的兜底文案

`deploy/dify-36/web-patches/1.17.1/aiops-workflow-managed.tsx:39`：
试运行终态无 `report.summary` 时显示该句。**不是模型回答，也不是 Dify 报错。**
第 2 条无 `order_no`，按设计应被 `TRIAL_ORDER_REQUIRED` 拒绝。

### 4. 知识类预览 0 来源

两次都是 `mode=documents, status=not_found, sources=0`。
生产在此前（10-30）是 `found, sources=5`。**这条独立于本次迁移，需单独查。**

## 三、我的误判（三次，均已订正）

1. 目标应用按登记表取 `394b5399` → 实际生效是 `07d15c42`；
2. 认定「advanced-chat 不支持草稿预览」→ `/advanced-chat/workflows/draft/run` 返回 200；
3. 认定「公网 `/v1/internal/` 被 403 拦截」→ 用 GET 打了 POST-only 端点，正确调用可达。

共同点：**看到非 2xx 先假定环境限制，没有先核对自己的调用方式**。

另有一处**手册误导**：我在 runbook 里写「读 `workflow_run_id` 与服务端日志」，
那是 Dify 原生预览的口径；操作者点的是**受管预览**（走网关 `/v1/dify/management/trial`），
Dify 侧不会产生 `workflow_runs`。两份口径不一致，导致"六条都通过"的误读。

## 四、阻塞

- **试用入口需要公司控制台会话**（`X-AIOps-Console-Session`），我无法在无人参与下调用；
  正常路径是操作者在浏览器点，本轮已走这条。
- **线上 `max_tokens` 仍未核实**（C 端已知 1000/`enable_thinking=false`；管家 Java 侧未知）。
  此前 3194 的推理峰值属其他样本，**不能套到 C 端 1000 的配置上**。

## 五、需要 Codex 决断

1. **先补 slots 标记再验，还是先查清 `not_found`？**
   我倾向先查 `not_found`：若知识检索本身在生产是坏的，slots 验了也没意义。
2. **`chat_route_bindings` 与 `dify_app_registry` 不一致**是缺陷还是过渡态？
   若非预期，谁在 12:15 改了绑定、是否应回退到 `394b5399`。
3. **`data_kind=status` 却被当作「知识问题」预览**——是组件传参问题还是我读错了？
   两条 run 的 question 都是充电问题，理应 `knowledge`。
4. 纯追加 vs minimal：**仍未取得真实模型证据**，不能凭假设选。

## 六、当前可回滚点

- 草稿：`a7bcda4f…`（改前）→ 现 `6d939223…`（追加了协议块）
- 已发布版本：`2026-10-31 12:15:18`（id `e7501f0a`），**未发布、未动**
- 候选：`candidate-append` `ed6b19813b7649e0` / `candidate-minimal` `66e074251635009c`
- 全部私有原件在 `~/.cache/aiops-prompt-slots-07d15c42/`
