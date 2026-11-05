# Runner 队列治理与复验

设计见[队列治理方案](../architecture/2026-11-05-runner-queue-design.md)，决策见[ADR](../adr/0234-runner-queue-isolation.md)。

## 职责

- GitHub托管runner：CI、Workflow policy、依赖推进、架构待办计数、过期任务清理和CD Watch。
- 原agent runner：模型工作，保持一个实例/执行槽。
- 专用`aiops-deploy`：CD部署、发布说明与无副作用Runner Probe。只能带此自定义标签，不能带`self-hosted`。

标签隔离只保证调度。两个实例仍同账户/主机，CPU、Docker和凭据边界未变；
MemoryHigh/Max只限制该用户unit进程，不宣称限制Docker守护进程创建的所有容器。

## 过期PR任务

三个PR agent工作流的run-name固定为`<workflow> / pr-<number>`。
开始前与交付前读取当前PR；关闭、fork、非所有者或head变化即停止，不调用模型或推送过期结果。
最终push入口再核对PR；现有远端head与非强推保护保持。

Agent Lifecycle在closed事件或半小时兜底扫描运行，枚举完整分页，只取消白名单内、
创建于关闭前、同仓库/分支且PR当前仍关闭的运行。旧格式无可靠编号的运行不自动取消。
重新打开/关闭时间变化、API失败或分页不完整均不推测；普通取消后最多等待2分钟确认终态，
超时让本作业失败并输出run编号，不force-cancel、不批量清理容器。
仅取消明确失效任务，其他PR与CD保持运行。取消前的最后一次读取与请求之间仍有窄竞争窗，
GitHub取消API没有条件版本参数；它不能推送代码，极端竞争仅终止已被判为过期的计算。

## 部署runner安装与撤回

只在已有治理发布单元的开发主机操作，不在服务宿主运行runner。使用官方runner发行压缩包与其SHA256，
解压到`~/Projects/_runners/AI-Ops-deploy-runner`，不能复制别人的`.runner`或`.credentials`。
本轮验证版本2.338.2，后续按官方发布核验；不禁用自动更新。

注册token通过`gh api --method POST repos/<owner>/<repo>/actions/runners/registration-token`运行时取得，
传给子进程环境`ACTIONS_RUNNER_INPUT_TOKEN`，不打印、不写文件、不进入命令行：

```bash
./config.sh --unattended --url https://github.com/<owner>/<repo> \
  --name aiops-deploy --no-default-labels --labels aiops-deploy --work _work
```

unit模板是`.github/runner/AI-Ops-deploy-runner.service`，安装到用户systemd目录：

```bash
install -m 640 .github/runner/AI-Ops-deploy-runner.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now AI-Ops-deploy-runner.service
systemctl --user show AI-Ops-deploy-runner.service -p ActiveState -p MemoryHigh -p MemoryMax -p CPUWeight
gh api repos/{owner}/{repo}/actions/runners --jq '.runners[] | {id,name,status,busy,labels}'
```

新runner online、唯一标签通过后才合并CD路由切换。回滚先以PR恢复CD路由，确认无新任务使用专用槽，
再停用unit、通过GitHub API删除该runner注册并保留必要回执；不删除其他runner或工作目录。

## 复验和日常排障

```bash
uv run pytest tests/tooling/test_runner_queue.py tests/tooling/test_actions_delivery.py
node .sandcastle/cd-watch.test.mjs
npx --no-install tsx .sandcastle/queue/cancellation.check.ts
node .sandcastle/queue/retire-runs.mjs --dry-run
node .sandcastle/cd-watch.mjs --dry-run
gh workflow run runner-probe.yml --ref main
```

PR合并前可由所有者给同仓库PR加`queue:probe`触发同一探针；不checkout候选或使用凭据。
Runner Probe是两条无模型/无生产访问作业：普通槽占用90秒，专用槽检查部署命令存在。
验收要读jobs API确认不同runner ID和执行时间重叠，不只看两个绿灯。
容器取消验收使用同一SDK+合成sleep agent，发送SIGINT后确认本任务容器和子进程消失。
不要通过取消真实他人工作来验证。长期观察至少20次真实合并，不人为造20次生产部署。

CD Watch每5分钟运行（平台可能延迟），等待2分钟输出诊断，5分钟告警。runner清单需administration:read，
使用已有受控AGENT_PAT仅供只读查询；GITHUB_TOKEN继续负责告警。token缺失或查询失败明确为未知。
作业status从部署job读取，发布等待不冒充部署等待；不要只凭job.started_at判断拿到runner。

查看等待者用`gh run list --status queued`及`--status in_progress`，再用
`gh run view <run> --json jobs`核对runner、实际步骤与并发锁。
停止前先确认该run已失效；生产作业继续由原部署锁串行执行。
