# 阻塞

最后更新：第二轮（2026-10-30 晚）。每项写清**证据、影响、所缺条件、已继续的部分**。
本轮明确：原任务书已授权在既有入口部署合并制品并使用库外账号（R12 的"重新授权"要求不成立），
所以下面没有一项是"等授权"。

## 1. 前端制品未构建部署（影响 R3 的真实页面证据、A/B/E/K 的真实动作）

- **已尝试**：在固定 1.17.1 源码上应用全部 web-patches（含新增的
  `06-workflow-and-home-entry.patch`），跑了浏览器用例：
  `aiops-management.browser.spec.tsx` 10 passed、`aiops-workflow-managed.browser.spec.tsx`
  2 passed。**没有**执行 `deploy/dify-36/web-patches/build-web-image.sh`（约 4 分钟的
  `next build --webpack`）与 36 上的镜像组装/`force-recreate`。
- **所缺条件**：开发机完成构建 → 36 上用上游 `langgenius/dify-web:1.17.1` 做 base 组装
  `aiops/dify-web-patched:<新tag>` → `deploy/.env` 写 `DIFY_WEB_IMAGE=` → `up -d --force-recreate web`
  → 用 `aiops-41-platform-dify` 正常登录核验。回滚回到**切换前那个补丁镜像 tag**（不是清空回裸 Dify）。
- **具体缺失事实**：本轮剩余时间内没有执行这次构建与切换；因此 36 上控制台仍是
  `knowledge-1be3f067` 那一版，页面上点不到新入口。
- **已继续的部分**：组件、页面挂载点、浏览器用例、桥/API 契约全部完成并合并且已部署到 41。

## 2. 真实消费者/员工会话未执行（影响 G1/G2/H/I/J1 的真实部分）

- **已尝试**：核对了库外资产字段存在（`aiops-41-platform-dify`、
  `aiops-ulink-phone-20261030`、`aiops-youlianchong-phone-20261030`），未输出凭据、未发起登录。
  网关侧只做了只读回读（匿名 401/403、openapi 200、退役路由 404）。
- **所缺条件**：第 1 项完成（新入口可用）→ 平台账号在页面对一个租户完成双端开通 →
  用租户手机号在原 H5 正常登录跑知识/订单问题 → 用员工账号在原管家 H5 跑站点范围问题 →
  把每个请求关联到确切 QA/run/版本。
- **具体缺失事实**：没有可点击的新入口，所以"页面开通"这一步无法进行；真实会话因此没有起点。
- **已继续的部分**：契约级用例覆盖跨租户/跨用户拒绝、内部库拒绝、撤销后拒绝；现役入口边界
  在部署后复验未放宽。

## 3. 渠道状态未声明（影响 J2 的真实取值）

- **证据**：`/etc/aiops-41/production.env` 未配置 `AIOPS_GATEWAY_ENVIRONMENT_MANIFEST_FILE`；
  `ops/environments/env-41.toml` 没有 `[[channels]]` 段。
- **所缺条件**：先有真实登录证据确认哪些租户的哪些端已接入原 H5/App，再写声明并重启网关。
- **没有自行声明的原因**：把"某租户客户端渠道已接入"写进生产配置，等于替用户断言一件需要
  真实登录证明的事；宁可显示"渠道待接入"。

## 4. 桥 / API / 节点部署未做（R12 的其余组件）

- **证据**：本轮只触发了 Gateway 的 CD（`1.0.0+b0d0bd3f392c`）。控制台同源桥
  （`ops/dify-console-bridge` 包）、Dify API 补丁（`deploy/dify-36/patches`）与节点 jar 都没有
  在本轮重新安装。
- **影响**：R1 的桥修复在**代码**上完成，但 36 上运行的桥仍是旧包 —— 页面即使部署了也会在
  桥上被 400 拒绝（正是 R1 描述的故障）。
- **所缺条件**：按 `docs/agents/operator-cpms/self-service.md` 的部署节安装桥包与 API 补丁，
  逐组件核对 sha，并保留旧制品。

## 5. R13 原 CI 间歇回读失败：未解决，风险在册

- **已尝试**：`gh run view 41061932673`、`gh api .../actions/jobs/124086732484/logs` ——
  **原日志已不可取**（该 job 已被 rerun 覆盖/清理），因此无法从日志判定当时返回 200 的是
  被篡改的正文还是观察时序。
- **本轮做的判定**：同一份代码本机 `-n 0` 连跑 3 次、`tests/consumer` 整体 `-n 4` 连跑 2 次
  全通过；#1233 的 CI 也通过。⇒ **未复现**。
- **风险与影响面**：受影响的是**回读路径的篡改检测**
  （`tests/consumer/test_consumer_model_reply.py::test_readback_refuses_a_reply_whose_numbers_do_not_match`
  所保护的"被篡改的正文不得交付"）。若它真的会偶发返回 200，含义是消费者可能看到一条与
  本轮取证不符的正文 —— 属于安全相关缺陷。**不宣称已修复。**
- **所缺条件**：一次可复现的失败（CI 日志或构造出那个时序）。已知的一个相关窗口
  （终态先可读、取数凭据后撤销）已由 #1229 修掉并加确定性回归；本条与它不是同一件事。
