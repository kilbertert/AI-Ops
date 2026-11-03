# 管家 H5 首页「智能检测」入口撤回

目标：公司要求撤销此前加在管家端首页的「智能检测」入口。只撤入口，不撤聊天页。

基线是公司仓 `s2b2c-front/qumall-app` 的 `ac9624f2`（`abroad_standard` 最新），
本仓侧基线 `7116edd0`。

## 已完成

- 查清归属：首页按钮由我们 `57b4018e` 加入；聊天页本身是公司 `e91d7bbc`（2026-09-30）引入，
  不在撤销范围。同轮我们加的三处行为修复（发送/停止文字按钮、生产构建关闭诊断模拟、
  请求不打印含授权头 cURL）也未撤销。
- 公司 MR **!146** 合并为 `bb5529829dd03554d162b8e1bbfdc9beac1df4ca`，仅删
  `pages/home/index.vue` 的一行入口按钮，并同步 `tools/verify-h5.py` 的首页断言。
- 干净合并修订构建 594 文件（`dirty=false`），经 `dev-host cp --artifact-sha256` 送到 41，
  `deploy/operator-h5.py` 安装为 `/www/wwwroot/ulinkmanage-h5-bb552982`，vhost 静态根已切。
- 旧根 `ulinkmanage-h5-5121d9f8` 与配置备份 `/root/h5-backup-bb552982` 保留，回滚命令已写进回执。

## 验证结果

- 本仓 `tests/test_h5_assistant_routes.py` 3 passed；全量 `pytest` exit 0。
- 真实页面（验收机 Playwright，加密清单账号正常登录）：首页源码无「智能检测」；
  直达 `/aiPackage/pages/chat/chat` 后一次提问 `completed`，
  `agent_runs.chat_binding = operator_order_diagnosis:production-41:1942105476598861824`，rev 17。
- 同域路由白名单安装后与备份逐字相同，7 条声明路由 401、未声明 POST 405、未声明 GET 落 SPA 回退。
- OH-04 单订单验收通过：真实订单 `2117490429826162689` 走 `operator_order_diagnosis`，
  `cpms_order_detail` + `cpms_charging_samples` 两项工具同次取得，监测为空时页面写明缺证。

## 已知缺口

- 未覆盖多语言界面、原生 APK/IPA、业务写路径（预约/计费/退款）。
- 公司仓该分支无流水线，构建与浏览器检查在本地与验收机执行，不代表公司 CI。

## 下一步

- 若产品仍需该能力，应选定明确位置（不是首页导航栏）提供入口，由调用方导航到聊天页。
- 若产品决定该验收项不再适用于本制品，更新 `docs/acceptance/operator-h5-deploy/` 的判据。
