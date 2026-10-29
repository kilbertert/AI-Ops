# 本地 Gateway 镜像与内容锁验证

适用：修改 `src/` 或镜像构建上下文后，
`test_gateway_context_digest_is_reproducible_and_matches_the_lock` 报摘要不同。
本页只在开发机/runner 构建验证镜像；不启动生产、不更新生产配置、不向服务宿主复制文件。

从任务 worktree 根执行，使用锁文件已声明的基础镜像摘要：

```bash
umask 027
build_base=$(python -c 'import json; print(json.load(open("deploy/m2-live-03/images.lock.json"))["gateway_image"]["base_digest"])')
docker pull "$build_base"
M2LIVE_BASE_IMAGE="$build_base" \
M2LIVE_GATEWAY_IMAGE=aiops-m2-live-03-gateway:task-verification \
M2LIVE_DIST="$PWD/dist/verification" \
deploy/m2-live-03/build-image.sh --update-lock
python deploy/m2-live-03/context_digest.py --check
docker run --rm --network none --read-only --entrypoint python \
  aiops-m2-live-03-gateway:task-verification \
  -c 'from aiops_diagnostics.gateway_api import create_gateway_app; print("Gateway import: passed")'
uv run pytest tests/test_m2_live03_deploy.py -n 0
```

多任务并行时给 `M2LIVE_GATEWAY_IMAGE` 换一个任务专属 tag，不能覆盖其他任务镜像。
`dist/verification/gateway-image/identity.json` 是这次真实构建的身份；`--update-lock`
由同一个构建脚本写回仓内 `images.lock.json`，一起纳入 diff 复核。不能只改摘要让测试通过。
构建失败时保留失败记录并修原因；未完成构建不能填新的 `image_id` 或 `built_at`。

这里验证的是构建内容与可导入性，不证明完整 Gateway 启动、真实模型、业务访问或生产部署。
需验证 HTTP 契约时另用仓内 Gateway 验证技能，在一次性数据根执行。
