# 失败定位已上线：41 后端 + 36 控制台

日期：2026-11-04。分支 PR #1332（只含记录，代码是 #1331 `5bbf7e0d`）。

## 目标

把 #1331 的"通用失败带步骤与异常种类"真正送到生产两端：Gateway 的响应字段，与控制台
页面的新文案。

## 已完成

- **41**：CD `5bbf7e0d` 成功，`/health` = `1.0.17+5bbf7e0d54e8`；`public_job` 顶层含
  `failed_step` / `failure_kind`。
- **36**：开发机构建 `aiops/dify-web-patched:setup-failure-visibility`，产物传输后
  sha256 逐字核对，36 上用上游镜像做 base 组装；`.env` 备份后切 `DIFY_WEB_IMAGE` 并
  `--force-recreate web`；运行中的容器内 grep 命中「核对客户端来源」「出错（」。
  回滚：`.env` 改回 `trial-errors-07a113ec` → `up -d --force-recreate web`。

## 验收结论

上线当时只证明部署通过（制品在位、字段在形状里、镜像内容对、页面可达）。
**业务验收随后用一次受控失败补上并通过**：页面显示「准备管家依赖出错（PrivatePathError）」，
作业行与 journal 给出对应的步骤与栈，经同源桥与真实登录会话读到。
详见[受控失败的业务验收](../validation/2026-11-04-setup-failure-controlled-acceptance.md)。

## 当前缺口（受控失败验收之后）

1. **只验了一种失败形状**（依赖准备的本地校验失败）；发布阶段、Dify 上游失败、
   `SetupConflict` 类仍只有确定性用例覆盖。
2. 开发机到 `dify.ranlei.work:443` 的 TLS 握手仍被重置；页面验收走的是取证通道，
   不是网络恢复。
3. 运营能否直接从这句话定位问题，仍需要真实运营反馈。

## 上线当时的已知缺口（保留为历史）

以下三条是**上线当时**的待办，逐条标出当前状态（"已补"指受控失败验收；
其中第 3 条只是取证路径补上了，网络问题本身没有修复）：

1. ✅ 已补：受控失败确认页面显示「准备管家依赖出错（PrivatePathError）」，journal 里有栈。
2. ✅ 已补：受控失败经同源桥读到 `failed_step`/`failure_kind`，不再只是公开形状。
3. ✅ 已补：真实登录会话打开页面并截图（见受控失败记录）。
   但**网络本身仍未恢复**：开发机到 `dify.ranlei.work:443` 的 TLS 握手仍被重置。

## 落地经验

从 36 组装 web 镜像时，产物只能从**已构建镜像**的 `/app/targets/next` 取
（`docker cp`）；单独构 builder 阶段会因缺 `sdks/nodejs-client` submodule 失败，
工作树里也没有 `.next`。

证据见
[上线记录](../validation/2026-11-04-setup-failure-visibility-live.md)。
