# 来源版本下拉改标「客户 / Dify 应用名」

分支`fix/catalogue-source-labels`，基线`7b5d62f3`（起草时`83ba786a`；期间基线前移六次，每次 `src/` 变化都重建锁）。

`GET /v1/dify/management/catalogue` 的 `sources[].name` 原来直接取 Gateway 智能体名
（`ops-methodology-700`、`operator-<app_id>`）。这两个字符串只存在于 Gateway：
它们不是 Dify 应用名，也不出现在任何工作区列表里，于是「复制并开通」的「来源版本」
下拉里出现了运营**从未见过、也无法对照归属**的条目。

本片把标签改成运营能核对的组合，并补齐字段：

- `name` = `客户名 / Dify应用名`（无客户名时只用应用名）——前端只读这一个字段，
  所以**不需要重建 Dify web 镜像**；
- 新增 `tenant_id`、`tenant_name`、`app_name`，为后续前端文案改造备好数据；
- 客户名取公司 `/tenant/list/my`（与可开通客户目录同源）；Dify 应用名按应用所属
  workspace 读 `/console/api/apps/<id>`（与 `adopt_draft` 同一调用方式）；
- 应用名按 app_id 缓存 120 秒（工作室每打开一次就拉一次目录）；读失败退回智能体名，
  **读名失败不挡住开通入口**，也不让 Dify 慢的时候每次打开工作室都撞一轮超时。

形态决定记在 [ADR-0178](../adr/0178-catalogue-label-reads-the-dify-app-name.md)：名字在请求期读
Dify、带 120 秒缓存、读不到就降级，不在登记时存一份会过期的快照。

不在本片范围：下拉里 `· V{版本}` 仍是 Gateway 智能体正式版本号，不是 Dify 应用发布版本；
要改这处文案需重建 web 镜像，未做。

验证（详见[验收记录](../validation/2026-10-30-catalogue-source-labels.md)）：
`tests/dify` 753 passed；全量 3441 tests / 0 失败 / 9 既有跳过；ruff 通过；
Gateway 镜像按新源码重建，`--update-lock` 写回 `deploy/m2-live-03/images.lock.json`
（`source_sha256=e3ff5c96d0e3b07f…`）。锁文件不能并行编辑：并行期间撞到兄弟工作树未提交的同名
改动与一次 rebase 冲突，两次都靠**重新构建**解决，没有手工改摘要。

已知缺口：尚未部署到 41；真实平台账号下的下拉文案、以及真实客户工作区的应用名读取
都还没在线上验收；读不到应用名时的降级表现也只在合成数据上验证过。
