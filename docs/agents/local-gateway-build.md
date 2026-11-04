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
`$M2LIVE_DIST/gateway-image/identity.json`。构建侧输出该JSON的SHA-256；在批准的部署记录中
固定它，并通过`M2LIVE_GATEWAY_IDENTITY_SHA256`传给预检（恢复旧版时使用旧版批准值）。
该值是本次交付的选择，必须从构建侧记录取得，禁止在服务宿主临时对现存JSON取摘要作为期望值。
没有这个独立期望值，旧镜像与旧记录一起遗留也会相互匹配，无法证明部署的是本次制品。
`checks.sh preflight`先核对批准摘要，再核对实载镜像ID、三个标签和
已批准基础镜像；缺失、非法或错配均拒绝。旧记录不能配新镜像，不能现场按标签重造记录。
这份JSON不是签名，可信性依赖既有合并制品和受控传送流程；不宣称防止有权限者同时篡改两者。

这里验证的是构建内容与可导入性，不证明完整 Gateway 启动、真实模型、业务访问或生产部署。
需验证 HTTP 契约时另用仓内 Gateway 验证技能，在一次性数据根执行。

## 判断构建工具变更是否需要生产部署

先比较现役部署提交到待交付提交的`src/`、`services/`、版本/依赖声明和实际部署入口。
再按[环境手册](env-41-runbook.md)用`dev-host exec 41 --allow-service-exec`只读检查：
`systemctl show`的ExecStart/Pre/Post，现役容器的`docker inspect`挂载与镜像ID，
以及正式节点发布目录中的执行引用。记录确切提交、文件摘要和核对时刻，不能只看health版本。

若变化仅在开发机/runner构建工具，且没有服务端执行引用，交付动作是同步主线并验证合并工具；
不为“补部署”重启无变更的应用或安装未启用的隔离栈。runner由Actions checkout取得每次
任务版本，不修改它当前运行中的工作目录。若发现实际引用，再按对应项目部署规程交付
已合并制品，并保留原镜像、配置与恢复路径。#1355的实例见
[部署范围核验](../validation/2026-11-04-build-identity-deployment-scope.md)。
