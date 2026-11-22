# 真空机一键冷开通（含初装）开发验证

版本：1.40.0（VERSION/pyproject/__init__.py 三处一致）。日期：2026-11-22。
代码栈：`a485c9cb`实现、`8ffa7db1`合入 docs-only `158382ee`、`14d06009`桥放行
修复（含页面文案修订与 No-ADR 声明）；对应[ADR0308](../adr/0308-cold-installation-before-preflight.md)及
[注册表手册「空机初装」节](../agents/environment-registry.md#空机初装包含初装adr-0308)。
本事件验证代码与替身边界内整链，未部署、未启用、未触达生产；95 旧 `/opt/aiops-95` 归档不恢复，
新声明为 `aiops-access-95` 四根。

## 已证明的行为

- 「准备候选」区新增「空机初装并准备」：`window.confirm` 显式确认后同源提交五键载荷
  `{candidate_plan,tenant_id,source_app_id,source_version,initial_installation:true}`；
  缺省或非 true 按无效载荷拒绝，父编号摘要计入该声明。页面文案明确普通准备仅准备资源、
  初装按钮会在空机上安装停用模块，均不启用正式入口。
- `site-install` 安装面子任务：同一 SQLite 行、waiting/executing/checking 租约、
  attempted/dispatched/authorize 闸、安装面互斥与取消级联；site/worker 认领面拒绝。
- stage 只出 `application`+`candidate`（无 runtime、无 expected），`python` 取计划目标
  声明，`observe_only` 沿用 attempted；意图 `cold-intent.json` 先于 apply 首写持久在
  worker 受管 staging 根；冷投影仅停用目录+公司连接；事务仍归原 `Installer.apply`。
- 远端 `task.py cold-run/cold-observe` 事实分类：`installed.json`→observed（installed/
  already_installed）只跳过；`root/state/config/unit`+`-preparing-*`残迹+旧单位→reconcile；
  声明 `backups` 根容忍可信私有空目录（apply 本就容忍），有内容→reconcile；
  observe/expired+空机→blocked not_installed；占端口→blocked REFUSED。
- complete 校验：observed 仅 installed/already_installed+版本/revision/3 digest，
  installed 另比对 staged 制品；父链 `site-install→STEPS`，cold observed 后
  `tasks.submit(after=cold id)` 延迟建 site-preflight，陈旧阻塞行不复用。
- 桥 `self_service_bridge` 两层放行 `initial_installation`（bounded-json 键集+五键形状），
  仅显式 true、其他取值 400；真实桥 HTTP 已证转发原样。
- `.dev-host.toml` 95 补 access* 四根；`build-installer.sh` 对未合并修订按 merge-base
  闸正确拒绝（无产物），冷装包合并后才能打。

## 独立发现与修复

- 初版全量单跑 5697 通过/9 跳过/3 失败：三处失败全为版本同步遗漏——pyproject 与
  `__init__.py` 已升 1.40.0 而 `VERSION` 文件漏同步（`test_release_version.py` 约束三处一致）。
  修复后定点 14 项通过；此后增量为桥放行+merge+文档/文案，无冷路径逻辑改动，按指示不再全量。
- 桥白名单缺口：environment-task 五键载荷在桥层 `set(payload)` 精确键集校验被拒，
  请求到不了 Gateway——与 #1638「桥白名单未放 verify_task」同类缺陷，若未发现即部署后必报错。
  已修两层（键集+形状门）并加真实桥 HTTP 测试：字段原样转发、False/"yes"/1/None 各 400。
- worker spec 缺陷：cold 路径在 `if cold:` 覆盖前先行读取 `staged["expected"]`，
  KeyError 被宽泛 except 吞掉、任务永远停 reconcile 骨架。改为条件赋值（cold 取
  `staged["python"]`、非 cold 取 `expected`），新增替身捕获真实 driver 调用的回归测试。
- 页面文案（root 21:20 修订）：原文案把整块准备区域说成不安装，实际初装按钮会安装
  停用模块；已改并留真实 Chromium 截图（新按钮可见态、确认后「空机初装·same：待安装」
  与父任务子步骤行）。
- 独立验收（`87219a80` 报告，root 已消费）：实际 5 场景——主管理 HTTP→claim/stage/
  authorize→原 `execute_cold` 真文件 `apply`→原 complete→延迟 site→workspace 接续通过；
  取消/拒绝/未知检查有效。其产物曾误用相对路径落 `.worktrees/cold`，root 已整体移至私有
  归档（候选未动），不因此重复验。
- root 裁决：worker 持内部 token 可伪造 already_installed 的「建议 attempted」暂不加守卫——
  原信任 worker 的边界不变，后继真实 site 仍核，独立未给普通身份越权后果，不扩猜测守卫。

## 检查与证据范围

- ruff check/format 触及文件全绿；ADR 结构检查绿（`14d06009` 以 No-ADR 声明豁免，
  理由为 ADR-0308 已定桥字段接线）。
- 定点：install task 103、installation 16（含冷装 worker spec 回归）、candidate_
  preparation、registration/validation/publication 91、`test_dual_entry_bridge` 16
  （含初装用例）、tests/dify 全目录 991 通过。
- 真实 Chromium（`.scratch/dify-1.17.1` 构建树复制组件）15/15：实际点击「空机初装并
  准备」、取消确认零 POST、确认载荷含 `initial_installation:true`、待安装/正在取证/
  结果待核对/已取消/已装/已装（原安装保留）标签读回；旧「准备候选」四字段用例不变。
- `uv build`：`dist/aiops_diagnostics-1.40.0` wheel+sdist。
- 原 `verify-aiops-gateway` skill：真实 `aiops-gateway serve` 启动，/health 200 且
  `"version":"1.40.0"`、管理/问答匿名 401、openapi 200、旧端点 404 全过；
  证据 `dist/verification/gateway/aiops-gateway.json`。
- `build-installer.sh origin/main` 打包通过（8 文件含 task.py）；对本片未合并 HEAD
  按 merge-base 闸正确拒绝。

## 原入口与替身边界

- 原入口全部复用：同一管理 POST、同一父任务状态机、同一 worker installation_run、
  同一远端 task.py/installer bundle、同一 Installer.apply、同一桥动作。
- 替身：systemctl/health/check_database、worker 侧 dev-host exec/cp 与 build-installer
  （形状/字节断言）、测试内 trusted owner 放宽 euid。cold spec 为等价构造的合成 staged
  字典，非真实远程搬运产物——不声称主链全真实无替身。
- 关键路径未 stub：apply 真跑、真 socket bind/listen、真 tar/digest、真 SQLite、
  真 TestClient、真桥 HTTP、真 Chromium、真实 serve。

## 仍须现场完成

- 真实 95 受限初装窗口执行：用户已批准，须本片合并后由 root 执行，作者无生产操作权。
- worker→dev-host→真机整链（exec/cp 为宿主替身，未验真实搬运）；Web 镜像内新按钮
  装配（补丁已入仓，镜像装配属部署窗口）。
- 95 目标仅 Python3.13、384MiB 规格；cold 页面成功后还须 runtime 安装/登记与目标
  回调健康——workspace 准备完不等于业务已开通（root 21:24 状态）。
- 部署顺序：旧执行端视 `site-install` 为未知 action 停 `checking`，须先升级执行端
  （注册表手册已记）。

原始日志、截图与临时脚本私有归档于 `aiops-onboarding-completion-20261122/pinggao/cold/`
（RESULT.md、CANDIDATE.md、evidence/ 两张页面截图）。
