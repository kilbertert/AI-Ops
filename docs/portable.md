# 便携部署

第一版便携制品是命令行应用，不是桌面 GUI，优先面向 Windows 11，也支持 Linux。由于 Codex SDK 携带平台专用原生 runtime，必须在目标操作系统上构建对应制品。

## Windows 要求

- 推荐 Windows 11；完全更新的 Windows 10 1809 及以上版本属于尽力支持范围。
- 通过受限 SSH 隧道访问生产数据时，必须安装 Windows OpenSSH Client。可以通过 `AIOPS_SSH_BIN` 指定 `ssh.exe`。
- 便携 runtime 默认使用 Codex 的 `unelevated` Windows sandbox，基于当前用户的 restricted token，并受只读文件配置和模型命令禁网约束。只有在企业环境验证管理员设置和工作区 ACL 后，才可设置 `AIOPS_WINDOWS_SANDBOX=elevated`。
- 便携包不要求目标机器安装 Python、Node.js、Codex CLI 或项目源码。
- 初始 ZIP 未签名。企业广泛分发前应增加组织代码签名。

## 首次运行

在解压目录打开 PowerShell：

```powershell
.\aiops.exe init
.\aiops.exe paths
notepad "$env:LOCALAPPDATA\AI-Ops-Diagnostics\config\production.env"
```

安装 provider key 时不要把密钥放在命令行：

```powershell
.\aiops.exe key-install primary
.\aiops.exe agent-doctor --key-slot primary
```

默认私有路径位于 `%LOCALAPPDATA%\AI-Ops-Diagnostics`。设置 `AIOPS_HOME` 可以整体迁移 runtime 边界，也可以分别设置 `AIOPS_CONFIG_HOME` 和 `AIOPS_DATA_HOME`。

## 构建与验收

Windows 构建 Windows 制品，Linux 构建 Linux 制品：

```text
uv sync --locked --dev
uv run python packaging/build_portable.py
```

构建会生成 `dist/aiops/` 和版本化 ZIP，然后在源码目录之外解压 ZIP，并以最小 PATH 启动它。验收会初始化隔离私有 home，检查 key slot 和内置 Codex runtime，验证私有路径权限，拒绝制品中的秘密文件，并诊断 OCPP、YKC 金额不一致和交易数据缺失三份 fixture。

GitHub Windows runner 的通过制品会作为 CI artifact 保留 7 天；artifact 不是生产凭据存储，也不替代企业制品仓库。

## 安全边界

- provider key 保存在外部 key slot 文件中，永远不会编译进 executable 或复制进 archive。
- Windows 的配置、key、Codex home 和 run 路径使用当前用户保护 DACL；POSIX 使用 owner-only 的 `0700` 和 `0600`。
- 便携进程通过和源码运行时相同的环境清洗 launcher 启动 SDK 固定版本的原生 Codex runtime。
- 便携包只暴露有界的只读诊断工具，不增加退款、重算、重放、订单修改或服务控制能力。
