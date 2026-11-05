# 226. 状态词收敛单一权威，回读语言按签发时记录重算

日期：2026-11-05。

Status: Accepted（PRD #1371，修复以提交形式落库前已核对四份实现的差异）。

「这个状态在这门语言里怎么说」在仓库里有四份实现，给三个答案：
`consumer_plain._status_name` 查业务状态表再按 `_TRADITIONAL_STATUS` 换繁
（只覆盖 5 个 code 中的 3 个，其余落回简体——同一状态在客户端与回读
渲染出两个词）；`consumer_language.COPY` 的九门语言模板；各处自行
`role.replace("_", " ")`。回读侧再加一层：`answer_matches_snapshot` 按
`ANSWER_LANGUAGES` 枚举三门语言逐一重算比对——枚举外的语言（如
portuguese/spanish）缓存正文必然对不上任何一次重算，正常答复被判成
篡改、回读 503。

决定：两件事都改成「按签发时那一条」。

- `consumer_language.status_word(language, role)` 是状态词的**唯一权威**：
  九门语言走 `COPY`；`zh` 由 `ORDER_STATUS`/`role_of` 同构反查（不新造
  第三份标签表）；`en` 用角色名。键是角色不是状态码——「码 → 角色」
  的唯一权威 `role_of` 已经存在，不重复造映射。
- `agent_runs` 增加 `language` 列，签发 run 时记录本次语言，与
  `chat_binding`/`route_revision` 同一种「按签发时那一条」的纪律。旧行
  为 NULL，读取回落默认语言，不回填、不猜。
- 回读比对与渲染都取签发语言：`answer_matches_snapshot` 按 `run.language`
  重算一次（不再枚举语言组，也不用请求头——用请求头比对会把一次正常
  的换语言回读判成篡改）；`answer_from_run` 缺省取 `run.language`。
- `consumer_plain.status_text` 只拼装、不产词：`status_word` 为必传参数。
  本模块是叶子，不能反过来依赖 `consumer_language`——依赖方向破了，
  包内循环边就出现（这次 CI 实测抓到）。

## 备选方案

- 回读仍枚举语言、只是扩到九门：枚举越多越慢也越易误放行，且「这条
  run 用了哪门语言」签发时就知道，枚举是在丢掉已有的信息，否决。
- `status_text` 保留懒加载 fallback 自动取词：`consumer_plain` 会反向依赖
  `consumer_language` 形成循环边（懒加载只延迟到运行时，架构检查按
  静态边判），否决。
- 语言列不回填旧行、而是迁移时刷成默认值：旧行的真实渲染语言无人
  知道，刷默认值是把「不知道」写成像「知道」；NULL 回落语义更诚实，
  否决。
