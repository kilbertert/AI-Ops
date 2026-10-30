# 阻塞

最后更新：2026-10-30 21:1x（北京时间）。无一项是"降低标准"的结果；每项都写明所缺条件。

## 1. 真实页面开通未执行（影响 A / B / E / K 的真实部分）

- **证据**：`gh run list` 中 Dify web 镜像没有本次构建记录；`deploy/dify-36/web-patches/`
  的改动（#1225）只进了仓库，41/36 上的 `DIFY_WEB_IMAGE` 仍是上一次的镜像。
- **影响**：无法在真实页面完成"选客户 → 两端各选来源 → 开通"，也无法在编排页点
  "发布到客户端/管家"。这些用例目前只有契约级证据。
- **所缺条件**：
  1. 在开发机跑 `deploy/dify-36/web-patches/build-web-image.sh` 产出 `.next` 构建产物
     （webpack 构建约 4 分钟）；
  2. 在 36 上用上游 `langgenius/dify-web:1.17.1` 做 base，把产物 COPY 进去，产出
     `aiops/dify-web-patched:1.17.1`；
  3. `deploy/.env` 写 `DIFY_WEB_IMAGE=`，`docker compose up -d --force-recreate web`；
  4. 平台账号（库外资产 `aiops-41-platform-dify`）登录 Dify 控制台。
- **为什么没有自行执行**：第 2、3 步是 Dify 控制台的生产镜像切换，影响**所有**控制台用户，
  且第 4 步要用人持有的平台凭据登录。这两件事都不在本次"不改公网暴露、不提权"的授权形状内，
  需要用户明确同意后再做（回滚方式：清空 `DIFY_WEB_IMAGE` 再 force-recreate）。
- **已继续的独立部分**：后端 API、受管目录、逐端发布、凭据对账、草稿预览全部完成并有
  契约级证据；Gateway 已部署到 41 并回读。

## 2. 真实消费者与员工会话未执行（影响 G1 / G2 / H / I / J1）

- **证据**：本轮没有在原 H5/App 上发起任何真实会话；没有 QA/run 关联记录。
- **影响**：无法证明"消费者只读本人数据""客服只检索本客户对客资料""管家遵守站点范围"。
- **所缺条件**：
  1. 第 1 项完成（新客户端应用要能在页面上开通并发布）；
  2. 用库外资产 `aiops-ulink-phone-20261030` / `aiops-youlianchong-phone-20261030`
     在原 H5 正常登录，按 `docs/agents/env-41-runbook.md` 走完整链路；
  3. 每个请求关联到确切的 QA/run/版本，并核对授权业务来源的数字。
- **已继续的独立部分**：契约级用例覆盖"跨租户/跨用户拒绝""源客户库 ID 不得带过来"；
  现役入口在部署后仍然只对已认证会话开放（匿名 401），没有被本次改动放宽。

## 3. 渠道状态未在 41 上声明（影响 J2 的真实取值）

- **证据**：`/etc/aiops-41/production.env` 未配置 `AIOPS_GATEWAY_ENVIRONMENT_MANIFEST_FILE`，
  `ops/environments/env-41.toml` 没有 `[[channels]]` 段。
- **影响**：页面会把每一端都显示成"渠道待接入"（fail closed 的正确行为），但无法证明
  "已接入渠道显示完整结果"。
- **所缺条件**：确认哪些租户的哪些端已经接入原 H5/App（需要真实登录证据），
  再在清单里声明；声明完重启 Gateway 生效。
- **为什么没有自行声明**：把"某租户的客户端渠道已接入"写进生产配置，等于替用户断言一件
  需要用真实登录证明的事。宁可显示"待接入"，不写一个自己没验过的"已接入"。

## 4. #1227 的 CD 排队（不影响生产正确性）

- **证据**：CD run `41059747401` 状态 `queued`；自托管 runner `server-dev-runner-AI-Ops`
  被另一个会话的长任务 `Agent Update Branch (feat/dify-prompt-lifecycle)` 占用
  （12:45:51Z 起，该 workflow `timeout-minutes: 60`）。这正是 41 手册 §5.6 记录过的情形。
- **影响**：`fix/dual-entry-adoption-env` 还没上到 41；生产仍在 `1.0.0+ffa9ed645e12`，
  `/health` ok、服务 active。该修复只影响"纳管一条客户端应用"这一条尚未启用的路径，
  所以留在旧版本不产生错误行为。
- **所缺条件**：runner 空出来后 CD 自动执行，无需人工动作。
- **已继续的独立部分**：其余交付与验收不依赖这次部署。
