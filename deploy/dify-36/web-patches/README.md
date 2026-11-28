# Dify Web 前端补丁与 ACR 镜像发布指南（`1.17.1`）

本目录维护 `yidong-36` Dify `1.17.1` 控制面板的前端定制、本地编译以及阿里云 ACR 标准 OCI 分发（对应 [ADR-0365](../../../docs/adr/0365-dify-web-acr-oci-distribution.md)）。

## 1. 目录结构

- `1.17.1/*.patch`：对上游 `langgenius/dify@1.17.1` 前端现有文件的增量补丁（由 `build-web-image.sh` 按 `--fuzz=0` + SHA-256 严格校验后应用）。
- `1.17.1/aiops-*.tsx` / `1.17.1/aiops-management.ts` / `1.17.1/aiops-liquid-glass.*` / `1.17.1/ExternalKnowledgeIdPicker.tsx`：AI-Ops 自研管理页与控制台内嵌组件（`/aiops/environments`、`/aiops/management`、`/datasets` 页 `AiopsKnowledgeOpenCard` 等；原 Nginx `sub_filter` 注入脚本已按 ADR-0366 退役）。
- `1.17.1/*.spec.tsx`：基于真实 Chromium（Vitest Browser）的前端组件回归测试。
- `build-web-image.sh`：核心镜像构建脚本（拉取钉死版本源码 → 校验上游 22 个文件 SHA-256 → 打补丁 → `pnpm build --webpack` 构建独立可运行 OCI 镜像）。
- `publish-web-image.sh`：CI / 发布标准入口——校验提交归属 `origin/main`、构建、非联网冒烟校验、推送至公司阿里云 ACR（`AIOPS_DIFY_WEB_IMAGE_REPO`）并解析输出不可变 `@sha256` 元数据 `dify-web-image.json`。
- `deploy-web-image.sh`：36 宿主部署入口——强制校验 `@sha256` 摘要、在 `36` 直接从 ACR `docker pull`、只读冒烟后原子切换 `.env` 的 `DIFY_WEB_IMAGE` 并仅 `--force-recreate web`；web 重建或探活失败时恢复旧 `.env` 并尝试重建原 web。若旧 web 也无法重建，脚本会明确报错，不把恢复配置当作恢复服务。仍可使用 `--rollback` 一键回退。

## 2. 日常发版流程

### 第一步：CI 自动构建并推送 ACR

只要改动合入 `main` 且涉及 `deploy/dify-36/web-patches/**`，GitHub Actions 工作流 [`.github/workflows/release-dify-web-image.yml`](../../../.github/workflows/release-dify-web-image.yml) 会自动执行 `publish-web-image.sh`，将新镜像推至公司企业版 ACR（`qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-dify-web`），并在 Workflow Summary 与制品 `dify-web-image-*` 中给出不可变 `@sha256` 引用。

也可随时手动触发补打：
```bash
gh workflow run release-dify-web-image.yml --ref main -f target_ref=main
```

### 第二步：36 一键拉取并滚动更新（零 tar 包、零跨海编译）

从 Workflow Summary 或 `dify-web-image.json` 获取 `image_ref` 后，只需一条命令即可在 `36` 安全切换：
```bash
bash deploy/dify-36/web-patches/deploy-web-image.sh \
  --image qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-dify-web@sha256:<64位摘要>
```

> 说明：不再使用开发机 `docker cp /app/targets/next` 打 tar 包再 `dev-host cp` 复制到 `36` 重建的做法；也不改动 `36` 上的 `api`、`worker`、`nginx` 与任何业务卷。

### 一键回滚

若新前端页面需回退至上一版本：
```bash
bash deploy/dify-36/web-patches/deploy-web-image.sh --rollback
```

## 3. 仅本地验证构建

```bash
DIFY_WEB_PATCHED_IMAGE=aiops/dify-web-patched:local bash deploy/dify-36/web-patches/build-web-image.sh
```
