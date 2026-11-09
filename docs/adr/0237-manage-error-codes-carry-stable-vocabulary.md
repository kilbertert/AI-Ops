# 0237：管理接口错误码槽位只装稳定词表

状态：已接受

## 原因与决定

`POST /v1/dify/management/*` 的信封只有 `error.code` 一个槽位给客户端判别。实测发现
该槽位混装两种东西：稳定词表（`SETUP_*`、`ROUTE_BINDING_*`）与给人看的英文短句——
`node_publication.advance()` 把 `RouteBindingError` 用 `str(exc)` 包成
`NodeRouteConflict`，而该类无 `__str__` 覆盖，`str` 只回 message，于是
'route binding moved' 这类句子坐上了 code 位。前端译文表按 code 匹配，对这种
“消息当码”的形状永远不可能命中，未收录 code 再被统一兜底成「请重新登录」，
撤回、并发冲突、凭据缺失全被伪装成会话问题。

决定：**code 位只装稳定词表**。`advance()` 改透 `exc.code`（ROUTE_BINDING_*）；
前端 `failureText()` 按「本表译文 → 后端正文附码 → 原样带码兜底」解析，
未收录 code 最差显示「兜底（CODE）」——可定位，且永远不编造原因。
`NodeRouteConflict` 自带的四个英文短句维持现状作 code，由译文表直译。

## 备选方案

保留 `str(exc)` 最省事，调用链一行不动；但信封没有第二个槽位，客户端只能拿英文
短句当匹配键，后端改一句措辞译文表就静默失效——正是本次修复的缺陷形态，故不用。
前端把英文短句也逐条收进译文表可行且已部分采用（NodeRouteConflict 自带四句），
但对被包裹的 `RouteBindingError` 每条 message 都要登记，是把同一张手工穷举表再
扩大，故只用于存量四句，不为包裹路径续用。
把四个英文短句也改成 ROUTE_* 词表是更彻底的统一；但 `str(exc)` 同时是异常文本，
换词表会改变日志与既有匹配的可见形状，收益不抵本轮范围，留作后端词表收敛的
独立里程碑。

## 代价与验证

收益：管理面每一种拒绝都带稳定 code，新 code 的可定位性不再依赖译文表先行更新。
代价：code 词表暂时两套（ROUTE_* 与英文短句并存），读者需知后者是存量而非新约定；
前端对未收录 code 会向操作员展示原始码，属刻意的可诊断性取舍。验证：
`tests/execution/test_node_publication.py` + `tests/dify/test_dual_entry.py`
46 passed；浏览器契约规格钉住「裸 code 兜底不含『重新登录』」。
