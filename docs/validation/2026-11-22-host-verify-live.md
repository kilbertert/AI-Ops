# 受管主机身份验收：首次页面提交与独立取证

本事件验收 P4 [D38](../acceptance/environment-directory-qa-plan.md)与
[ADR0299](../adr/0299-managed-host-verification-task.md)（PR #1638 + 补漏 #1639）。
范围只证明：经既有 dev-host 指纹断言通道，受限执行端对任务目标**只读采集**
hostname、machine-id 摘要与服务身份存在性；不代替正式网络、容量与业务验收。

## 首次执行暴露桥层放行缺陷并已修复

2026-11-22 北京时间约 04:46，首次页面点击「主机身份验收」后任务卡片始终不出现。
控制库核对 `environment_tasks` 无任何 host-verify 行 —— 请求没有到达 41。

定位：`self_service_bridge` 同源桥对 `environment-task` 动作有两层白名单
（`_read_bounded_json` 顶层键集 + payload 形状枚举），`verify_task` 均不在册，
`{verify_task, plan_id}` 在桥层被 `400 无效环境任务` 拒绝。PR #1638 交付了
网关端点、worker 与页面按钮，但漏掉桥的放行面 —— 该缺陷单测与浏览器 spec
都拦不住（spec 桩 `manage()`，dispatch 测试直连 `submit_verify`）。

修复 PR #1639（键白名单 + `{verify_task, plan_id}` 形状枚举 + 跨动作禁键集
并入 `verify_task`，新增过桥正例与六种畸形/串参拒绝回归）已合并 `b8f1b73a`；
控制台桥经 `build-bundle.sh` + `deploy.sh` 现场更新至该修订
（备份 `/root/aiops-console-bridge.bak-b8f1b73a`，自探针六路由全过），
SHA256SUMS 与 `REVISION` 均与制品一致。

另实测确认登录代理 `_Backoff`：窗口 300 秒内同 `src:` 键 5 次失败即 429，
429 自身不记失败、窗口自然衰减 —— 自动化驱动须按 75 秒级退避，不能硬打。

## 页面提交到远端取证：124/LTA-CSMS 独立采集落地

2026-11-22 北京时间**05:35～05:42**，正常公司登录环境页（验证码由 ddddocr 自动识别，
首试通过），选择 124、草稿 V9/中央登记 R1，经「配置与制品预检」取得冻结计划
`plan_461d8d8d825a9d27eece4686cba79e36`（配置/制品分项 passed；
host-identity、capacity、network、business 仍 pending，state=blocked、
executable=false）。

点击「主机身份验收」，任务 `task_1f1f3464d7c7c5348955b266acbe3eab` 公开排队，
卡片先呈「等待执行端」，约 35 秒经既有 `aiops-site-preflight.timer` 领取后转终态：

```text
主机身份验收 · 124：本地观察通过
现场主机：ecs-2c54
服务身份：存在
仅证明任务落在已登记主机，不代替正式网络、容量与业务验收
```

重复点击「主机身份验收」返回**同一任务编号**，无第二个该计划任务；
刷新任务按钮后同一卡片与终态可读。

控制库核对任务 request/result 持久：`state=observed`，
`scope=managed-host-verification-only`、`check=managed-host-identity`、
`executable=false`；证据 `declared_target=singapore-server`、
`resolved_host=124`、`remote_hostname=ecs-2c54`、
`machine_id_sha256=be9927f4…71cf`（64-hex）、`service_user_present=true`。

## 整页退出验收

原生退出 `/console/api/logout` 返回 200/success；旧访问令牌重放
`/__aiops/publish/check` 返回 `allowed=false`、`SESSION_LOGIN_REQUIRED`、
不返回公司身份。浏览器与临时管理访问通道已结束。

## 边界与未验收项

本片只证明：环境页可提交 `managed-host-identity` 验收、任务持久领取、
受限执行端只读采集三字段齐全并如实标注范围。它**证明任务落在已登记主机**，
不证明正式双向网络、容量与业务可用；生效目录仍无该环境，租户与正式入口保持停用。
阻塞/待核对分支（不可达、歧义、字段缺失）未在现场触发，仅由单元测试与 Hack 覆盖；
`machine_id_sha256` 按合同只存摘要，原始 machine-id 未出目标。

证据私有存档：`~/.cache/aiops-host-verify-20261121/`
（验证码截图、任务日志、桥制品；不含明文凭据）。
