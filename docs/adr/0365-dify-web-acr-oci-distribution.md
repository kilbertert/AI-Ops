# 365. Dify Web 前端镜像公司阿里云 ACR 标准 OCI 分发与 36 一键滚动升级

状态：已采纳；与 ADR-0363（客户机 Gateway OCI 容器分发）保持一致，仅规范 `deploy/dify-36/web-patches/` 构建产物向 `yidong-36` 的发布与回滚链路，不改变 Dify API 补丁机制、`console-bridge` 权限模型及各客户机运行时边界。

## 上下文

`36` 上的 `dify-web` 镜像包含 AI-Ops 自研控制台界面（`/aiops/environments`、`/aiops/management`、工作流受控发布条及 Liquid Glass 主题）。在此之前，每当 `deploy/dify-36/web-patches/` 发生变更时，升级流程为手工链路：

1. 在美国开发机执行 `build-web-image.sh` 耗时约 8 分钟完成 Next.js webpack 编译；
2. 通过 `docker create` + `docker cp /app/targets/next/.` 抽出 ~218 MB 成品树；
3. 手写临时 `Dockerfile`（基于 `docker.1panel.live/langgenius/dify-web:1.17.1`）压成 ~59 MB `.tar.gz`；
4. 经 `dev-host cp` 传至 `36` 解包后再本地 `docker build`，最后手工编辑 `/opt/dify/deploy/.env` 并执行 `docker compose up -d --force-recreate web`。

该流程存在三个明显问题：交付链路与 Gateway 的 ACR OCI 标准割裂、构建与组装强依赖第三方 `1panel.live` 代理、缺少 `@sha256` 不可变版本留痕与失败自动回滚。此外，`build-web-image.sh` 本身已经产出了可直接运行（`["/bin/sh","./entrypoint.sh"]`、非 root `dify`）的完整 OCI 镜像，并不需要在 `36` 二次拆包重组。

## 决定

1. **统一采用公司企业版 ACR 分发完整 `dify-web` OCI 镜像**
   - 在公司 ACR 启用仓库 `qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-dify-web`（GitHub 仓库变量 `AIOPS_DIFY_WEB_IMAGE_REPO`，复用既有受控 `AIOPS_ACR_USERNAME` / `AIOPS_ACR_PASSWORD` 推送凭据，密码仅通过 `docker login --password-stdin` 传入）。
   - 新增发布脚本 [`deploy/dify-36/web-patches/publish-web-image.sh`](../../deploy/dify-36/web-patches/publish-web-image.sh)：校验源提交归属 `origin/main` → 调用 `build-web-image.sh` 构建 → 非联网容器冒烟检查（验证 `/app/targets/next/web/server.js`、`/app/entrypoint.sh` 及关键自研标记）→ 推送 ACR → 解析并输出不可变 `@sha256` 元数据 `dify-web-image.json`。

2. **按路径变更触发独立 GitHub Actions 工作流（不拖慢日常 Gateway 发版）**
   - 新增 [`.github/workflows/release-dify-web-image.yml`](../../.github/workflows/release-dify-web-image.yml)：仅在 `main` 上 `deploy/dify-36/web-patches/**` 变更或人工 `workflow_dispatch` 时在 `ubuntu-latest` 上执行 `publish-web-image.sh`，并将 `dify-web-image.json` 保留为 Workflow Artifact。
   - 不将 8 分钟前端编译塞进每次后端 `CD`（`cd.yml`），避免无关 PR/发版等待前端全量构建。

3. **36 宿主强制走不可变 `@sha256` 拉取与原子回滚**
   - 新增部署脚本 [`deploy/dify-36/web-patches/deploy-web-image.sh`](../../deploy/dify-36/web-patches/deploy-web-image.sh)：
     - **强拒绝可变标签**：`--image` 仅接受 `<repo>@sha256:<64位小写hex>`；
     - **就近拉取 + 零波及重建**：在 `36` 直接从阿里云 ACR `docker pull`，非联网冒烟通过后备份 `.env`、原子改写 `DIFY_WEB_IMAGE` 并仅 `--force-recreate web`；
     - **探活失败自动回退 + 状态账本**：自动探活 `127.0.0.1:10008/signin`（`200`）与 `/aiops/environments`（`307`），不通过即自动切回原镜像；通过后更新 `.dify-web-release.json` 记录 `previous_image`，支持 `deploy-web-image.sh --rollback` 一键回退。

4. **更新文档并将原 `docker cp / tar` 组装路线标为废弃**
   - 收敛 [`deploy/dify-36/README.md`](../../deploy/dify-36/README.md)、[`deploy/dify-36/web-patches/build-web-image.sh`](../../deploy/dify-36/web-patches/build-web-image.sh) 注释并补齐 [`deploy/dify-36/web-patches/README.md`](../../deploy/dify-36/web-patches/README.md)，让新接手工程师一眼掌握单路径操作。

## 替代方案

- **继续开发机编译并 `docker cp` + `tar` 复制到 `36`**：每次发版需人工 5 步操作、缺乏 `@sha256` 不可变记录与自动回滚机制，且与 `deploy/dify-36` 的容器体系割裂，拒绝。
- **合并进 `cd.yml` 每次发版必编 `dify-web`**：`dify-web` 仅在前端文件改动时才需要更新，若每次后端修复（`VERSION` bump）都重跑 ~8 分钟 webpack 编译会严重拖慢 `41`/客户机升级节奏，拒绝。
- **本次同步把 `/aiops/*` 控制台彻底拆出 Dify Next.js 仓库**：拆分独立前端工作台属于中长期结构改造（需要独立路由代理与认证测试重构），应作为后续单独步骤（简化 4）实施，不应阻塞当前先消除手工复制镜像。

## 后果

- **正向收益**：前端补丁合入 `main` 即自动构建、校验并推送阿里云 ACR；`36` 更新 `web` 收敛为一条 `@sha256` 命令且具备失败自动回滚与 `--rollback` 保障。
- **需注意**：因 `deploy-web-image.sh` 依赖 `36` 已配置对 `qushi-registry.cn-hangzhou.cr.aliyuncs.com` 的免交互拉取登录态（本次实施前已实测就绪），后续若更换 ACR 仓库或凭据，需同步更新 `36` 的 Docker 凭据与 GitHub 仓库变量。
