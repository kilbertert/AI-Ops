# 本地 Gateway 镜像与构建记录验证

适用：修改 `src/` 或镜像构建上下文后验证可部署制品。
本页只在开发机/runner 构建验证镜像；不启动生产、不更新生产配置、不向服务宿主复制文件。

从任务 worktree 根执行，使用锁文件已声明的基础镜像摘要：

```bash
umask 027
build_base=$(python -c 'import json; print(json.load(open("deploy/m2-live-03/images.lock.json"))["gateway_image"]["base_digest"])')
docker pull "$build_base"
M2LIVE_BASE_IMAGE="$build_base" \
M2LIVE_GATEWAY_IMAGE=aiops-m2-live-03-gateway:task-verification \
M2LIVE_DIST="$PWD/dist/verification" \
deploy/m2-live-03/build-image.sh
python deploy/m2-live-03/context_digest.py --check dist/verification/gateway-image/identity.json
docker run --rm --network none --read-only --entrypoint python \
  aiops-m2-live-03-gateway:task-verification \
  -c 'from aiops_diagnostics.gateway.gateway_api import create_gateway_app; print("Gateway import: passed")'
uv run pytest tests/tooling/test_m2_live03_deploy.py -n 0
```

多任务并行时给 `M2LIVE_GATEWAY_IMAGE` 换一个任务专属 tag，不能覆盖其他任务镜像。
`dist/verification/gateway-image/identity.json` 是这次真实构建的记录，含源码/依赖摘要、
基础镜像摘要、镜像ID与时间；属于制品，保存在任务自己的输出目录，不能提交到Git。
构建脚本自动对当前源码、`uv.lock`和已批准基础镜像核验这份记录。
`images.lock.json`的Gateway段只保留输入，普通源码PR不改它；`--update-lock`明确拒绝执行。
Java节点、平台和基础镜像的既有输入锁与检查继续生效。不要删除节点校验以解决Gateway并行冲突。
构建失败时保留失败记录并修原因；未完成构建不能填新的 `image_id` 或 `built_at`。

交付时从已合并修订构建并保留其提交身份。`--save <路径>`导出镜像后，将镜像和同次构建的
`identity.json`作为一组审核摘要、经既有`dev-host cp --artifact-sha256`传送；记录放到
`$M2LIVE_DIST/gateway-image/identity.json`。`checks.sh preflight`核对实载镜像ID、三个标签和
已批准基础镜像；缺失、非法或错配均拒绝。旧记录不能配新镜像，不能现场按标签重造记录。
这份JSON不是签名，可信性依赖既有合并制品和受控传送流程；不宣称防止有权限者同时篡改两者。

这里验证的是构建内容与可导入性，不证明完整 Gateway 启动、真实模型、业务访问或生产部署。
需验证 HTTP 契约时另用仓内 Gateway 验证技能，在一次性数据根执行。
