"""「请求入口 + 主体属性 → 业务数据范围」的单一权威（#644）。

「一次请求落在哪个内容域，就得到哪一种业务数据范围」是仓库的一条规则，
但它曾经在**两条凭据链上各写一遍**：会话链
（``third_session_auth.RedisThirdSessionResolver._data_scope``）与公司令牌链
（``company_token_auth.CompanyTokenCallerResolver._data_scope``）是逐分支同形
的同一段四步判定，差异只有「店铺集合从哪来」——而它没被写成参数，于是和
「怎么判入口、怎么判顶层、失败落到哪」这些本应只有一份的判定混在同一段里。
这条序列收利息的方式已经出现过：`#628` 修顶层账号可见性，必须在两处各加
一遍同一个分支；`#638` 是同一个错位在代理商账号上的另一半。

四个判定步只有这一份：

1. **入口判据**——运营商站点范围的语义是为管家端设计的（运营商员工看本
   运营商名下站点的订单），但它发生在身份层：若不按入口分流，消费者侧
   会被一并收窄（本人订单可能落在站点集合外）与放宽（同站点他人的订单
   被放行，#436 实测过）。因此只有 ``operator`` 入口走运营商分支，其余
   （``consumer``、缺失、非法）一律保持最窄的 ``self``——**未知入口不
   放宽**：非法入口随后由平台决策拒绝，范围在这里先收紧。
2. **顶层账号**——``type ∈ {-1,1}`` 的账号不受公司店铺隔离门约束
   （``ShopIdInterceptor.judge()`` 直接 return），可见范围是**整个租户**，
   数据上表现为「没有店铺绑定」。空店铺集合在这里不能读成「看不到任何
   订单」（#628 修过的错位），也不值得为它打一次店铺归属查询。
3. **主体可用性**——会话链独有：拿不到**同租户内唯一** B 端主体、或
   运营商目录根本没注入时按最窄 ``self``。令牌链没有这一支（主体来自
   令牌声明，缺身份在更早处就被拒），调用方显式传 ``False``——分支的
   有无写在调用点上，而不是埋在两段代码里。
4. **运营商站点集合**——「站点集合怎么来」是**调用方的显式职责**：
   会话链实时查 UPMS ``/shopuser/getShops`` 再映射站点，令牌链取令牌里的
   ``shop_ids`` 快照（ADR-0009 D2）。拿到之后只剩一个答案：``organ``。

**解析失败的落点也只有一种**：失败关闭为**空集合**（``organ`` +
``site_ids=()``，拒绝全部订单查询）——不回落为租户级放行，也绝不套用
``self``（那会把「查不到运营商范围」伪装成「只能看本人的单」，二者对同
一张单给出相反答案）。失败原因由调用方用自己的日志行区分（两条链的故障
要去不同的地方看上游），本函数只保证「失败」与「合法空集」不会被混为一谈。

平台凭据路径（``UpmsCallerResolver`` / ``IntrospectionCallerResolver``）
**不经过这里**：它们的范围由令牌载荷与目录断言，入口头在那两条链上不被
读取——这是 #644 的显式决定，写成 ``del platform_entry`` 与测试断言，
不是没人提起的沉默。
"""

from __future__ import annotations

from collections.abc import Callable

from aiops_diagnostics.data.sources import SourceError
from aiops_diagnostics.identity.scope_context import (
    SCOPE_TYPE_ALL,
    SCOPE_TYPE_ORGAN,
    SCOPE_TYPE_SELF,
    DataScope,
    ScopeError,
    is_operator_entry,
    is_tenant_level_account,
)

#: 「运营商站点范围解析失败」覆盖的逃逸方式全集：上游不可达/响应形状
#: 非法/数量超界（``ScopeError``）、充电库不可用（``SourceError``）、
#: 范围 ID 不可用（``ValueError``）。两条链曾各自列一遍同一组——漏掉
#: 一种就等于让一次授权故障冒名成 500，所以它们只在这一处出现。
_UNAVAILABLE = (ScopeError, SourceError, ValueError)


def entry_data_scope(
    platform_entry: str | None,
    user_type: str | None,
    *,
    subject_unavailable: bool,
    site_ids: Callable[[], tuple[str, ...]],
    on_unavailable: Callable[[Exception], None],
) -> DataScope:
    """判定序列的唯一实现（语义见模块文档）。

    ``site_ids`` 是调用方职责：它封装「这条链从哪里取站点集合」，只在
    前三个判定都放行的分支上被调用一次。``on_unavailable`` 接收解析失败
    的异常——两条链各记各的日志行，词汇不同但**分支相同**。
    """
    if not is_operator_entry(platform_entry):
        return DataScope(type=SCOPE_TYPE_SELF)
    if is_tenant_level_account(user_type):
        return DataScope(type=SCOPE_TYPE_ALL)
    if subject_unavailable:
        return DataScope(type=SCOPE_TYPE_SELF)
    try:
        resolved = site_ids()
    except _UNAVAILABLE as exc:
        on_unavailable(exc)
        return DataScope(type=SCOPE_TYPE_ORGAN, site_ids=())
    return DataScope(type=SCOPE_TYPE_ORGAN, site_ids=resolved)
