# Python 运行时退役验证

日期：2026-10-29（北京时间）；分支 `refactor/thin-harness-layout`，基线 `55a284d6`。
本次覆盖用户扩大后的完整退役范围，替代上一轮“仅删除配置别名”的验收范围。

## 确定性与本地进程验证

| 检查 | 实际结果 |
|---|---|
| 最终全量 `uv run pytest -rs --tb=short` | **3313 passed、9 skipped、41 warnings**，123.54 秒 |
| 现役业务专项 | WF01、语义路由、管家 CPMS、Dify 草稿、配置与分析 117 passed（中间检查） |
| 管理与退役专项 | 独立运维入口/知识开户/发布/退役集合 174 passed（中间检查） |
| 数据源/代理/状态机专项 | 115 passed；保留订单、枪时序、范围限制，删除旧取证专属方法 |
| 知识同步真实命令 | 临时库、回环替身，`aiops-admin` 执行 13 项通过：dry-run、创建、幂等、孤立库报告与输入拒绝 |
| Gateway 真进程 | 临时配置/数据根、动态回环端口：health 200；问答/管理匿名 401；OpenAPI 200；旧运行/注册/标准诊断/工具 404；进程清理 |
| 静态/依赖 | ruff check、format、compileall、`uv pip check`、部署脚本语法通过 |
| 包与镜像 | wheel/sdist 构建通过；旧模块和 `_bundle` 不在 wheel，保留 aiops-admin；真实镜像禁网只读导入通过，无 Codex SDK |
| 内容锁 | 最终上下文 173 个文件，`context_digest.py --check` 一致 |
| 治理检查 | policy、repo-map 通过；spex 0 error / 49 warning，既有覆盖/漂移未宣称清零 |

9 项跳过与基线同类：8 项需要公司 GitLab 身份锚点，1 项 operator 目录已无可测试缺口。
旧诊断专属测试随功能删除；混合文件中保留当前权限、取消、发布、存储、语言与边界检查。
新增测试验证旧模块不可导入、旧接口不存在、旧数据保留、C端快捷检测进入工作流、
独立 admin 的 `--config` 生效，以及单次模型请求拒绝工具/不完整输出、密钥文件权限。
没有通过新增 skip、关闭检查或保留旧执行器空壳获得通过。

## 失败与修复

清理期间先后暴露旧测试/探针引用、源码内容锁漂移、保留功能误删的注册表方法和订单标记，均逐项修复。
Gateway 的 Typer 单命令模式最初丢掉 `serve` 子命令，补回服务组入口；管理 CLI 保留 `--config`。
知识同步探针固定端口被现有进程占用，改用临时回环端口，未停止其他进程。
最后两条失败是 fixture 仍断言已删除的费用模板方法，移除旧断言后全量通过，保留订单/设备范围断言。

镜像 tag：`aiops-m2-live-03-gateway:thin-harness-retired`。
镜像 ID：`sha256:bc49ad72b2f9fafc8c8f9ce817c35f1578a56fcd6a0f8fab5d6b58408638458c`。
上下文摘要：`eacce4beec8f63858ab2b63fb905fd545de6b8156c6ba3d3271abf2597bb6cc3`。
构建时间：2026-10-29 08:50:23 +08:00；身份由现有 `build-image.sh --update-lock` 生成。
完整本地日志在任务 worktree 忽略目录 `dist/verification/`；镜像身份在 `dist/retirement/gateway-image/identity.json`。

## 真实环境与交付限制

**未完成业务验收**：没有真实模型、公司登录、订单、Dify/Java 远端整链或生产服务操作。
C端订单检测现走状态工作流，不把状态快照当旧多源根因分析，也不宣称故障准确率。
本地镜像来自未提交工作树，只供验证；未提交、推送、PR、远端 CI、评审、合并或部署。
生产升级需核业务发布绑定、依赖环境和直接模型入口；既有生产适配器未停止。
目录迁移未开始；旧数据库与历史证据未删除。
