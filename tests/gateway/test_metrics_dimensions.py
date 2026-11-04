"""三个度量维度的词表必须与**它的生产者**对得上（#799）。

## 这个文件为什么存在

本仓已经因为同一形状静默丢过**三次**指标：

| 时间 | 维度 | 丢的是什么 |
|---|---|---|
| #392 | `routing` | 分类器自身的判定路径 |
| #697 | `operations_analysis` | 运维分析面的每一条结果 |
| #799 | `trial` | 试运行终态（41 上实测 **181 次运行 / 0 行指标**） |

三次的形状相同：**生产者写了一个词表里没有的字面量** → `MetricsStore.record` 抛
`MetricsValidationError` → 被 `_record_metric` 的 `contextlib.suppress(Exception)` 吞掉。
抑制本身是对的（度量不该让一次运行失败），所以**症状恒为零**：请求成功、页面正常、
只是那一行指标永远不存在。

只有把「词表」与「生产点」钉在一起，下一次才会在 CI 红，而不是三个月后靠人读注释发现。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aiops_diagnostics.gateway.metrics_store import (
    OUTCOME_TYPES,
    RETRIEVAL_STATUSES,
    ROUTE_TYPES,
    MetricsStore,
    MetricsValidationError,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "aiops_diagnostics"
TENANT = "TENANT-A"


# ─────────────────────────────── 词表成员真的能落库
#
# 形状照抄 `tests/operations/test_ops_analysis_runtime.py` 里那一条
# （`operations_analysis` 事故的事后补丁）：真实 `MetricsStore` + 真实 `record()`
# + `list_runs()` 断言有行。这里把它推广到**每一个成员、三个维度**。


@pytest.mark.parametrize("route_type", sorted(ROUTE_TYPES))
def test_every_route_type_is_recordable(tmp_path: Path, route_type: str) -> None:
    """词表里的每个 `route_type` 都必须真的能写进去、读得出来。

    参数化到**全体成员**而不是只钉 `trial`：这次修的虽是 `trial`，但下一个人加成员时
    未必想到「还要能被 record」—— 让这条测试替他想到。
    """
    metrics = MetricsStore(tmp_path / "gateway.db")
    metrics.record(tenant_id=TENANT, route_type=route_type, outcome="completed")
    assert [row["route_type"] for row in metrics.list_runs(TENANT, limit=5)] == [route_type]


@pytest.mark.parametrize("outcome", sorted(OUTCOME_TYPES))
def test_every_outcome_is_recordable(tmp_path: Path, outcome: str) -> None:
    metrics = MetricsStore(tmp_path / "gateway.db")
    metrics.record(tenant_id=TENANT, route_type="qa", outcome=outcome)
    assert [row["outcome"] for row in metrics.list_runs(TENANT, limit=5)] == [outcome]


@pytest.mark.parametrize("status", sorted(RETRIEVAL_STATUSES))
def test_every_retrieval_status_is_recordable(tmp_path: Path, status: str) -> None:
    metrics = MetricsStore(tmp_path / "gateway.db")
    metrics.record(tenant_id=TENANT, route_type="qa", outcome="completed", retrieval_status=status)
    assert [row["retrieval_status"] for row in metrics.list_runs(TENANT, limit=5)] == [status]


# ─────────────────────────────── 判据只有一处
#
# `record()` 与 `list_runs()` 曾各写一遍同形判断（`if route_type not in ROUTE_TYPES`）。
# 改一处漏一处没有任何信号 —— 这条测试把「只许一处」变成可执行的判据。


def test_the_dimension_checks_are_not_duplicated():
    """三个维度的合法性判断只许在 `check_dimension` 里写一次。

    只查**判据形态**（`X not in DIMENSION_SET`），不查业务语义：`ROUTE_TYPES` 本身
    当然可以出现在别处（比如测试、文档生成）。
    """
    pattern = re.compile(r"(route_type|outcome|retrieval_status)\s+not\s+in\s+\w+")
    offenders = [
        f"{path.relative_to(SRC)}:{number}"
        for path in SRC.rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    # `check_dimension` 内部用的是 `value not in allowed`，不会被这个模式命中。
    assert offenders == [], f"维度判据被写在了第二处：{offenders}"


# ─────────────────────────────── 生产点对账
#
# 这一条是真正防住「下一次」的那条：**产而无判据**与**判据而无生产者**都要红。


def _produced_literals(dimension: str) -> set[str]:
    """机械读出生产点写了哪些字面量。

    两种写法都算：
    - `route_type="trial"`（关键字参数直接写字面量）
    - `route_type=ROUTE_TRIAL`（引用命名常量，解析回它的值）
    """
    found: set[str] = set()
    keyword = re.compile(rf'\b{dimension}\s*=\s*"([a-z0-9_]+)"')
    named = re.compile(rf"\b{dimension}\s*=\s*(?:metrics_dims\.)?([A-Z][A-Z_]+)\b")
    # **从模块里取真实的值，不在这里重写一遍**：硬编码映射会让守卫在常量被改成
    # 词表外的值时仍然通过（2026-11-04 复审指出）。这样写，常量改坏了守卫就红。
    from aiops_diagnostics.gateway import metrics_store as dims

    constants = {
        name: getattr(dims, name)
        for name in dir(dims)
        if name.startswith(("ROUTE_", "OUTCOME_")) and isinstance(getattr(dims, name), str)
    }
    for path in SRC.rglob("*.py"):
        if path.name == "metrics_store.py":
            continue
        text = path.read_text(encoding="utf-8")
        found.update(keyword.findall(text))
        for name in named.findall(text):
            if name in constants:
                found.add(constants[name])
    return found


def test_every_produced_route_type_is_declared():
    """**产而无判据** —— 这一条才是防住那三次事故的那一条。

    `trial`（#799）、`routing`（#392）、`operations_analysis`（#697）三次都是**同一个方向**：
    生产点写了词表里没有的值，被 `record()` 拒绝、被 `suppress` 吞掉，症状恒为零。

    （反方向「判据而无生产者」故意**不做机械断言**：`faq`/`debug`/`routing` 等经由
    `route_tag` 变量或路由标签产生，按字面量 grep 读不到它们。这正是本 PRD 指出的
    「按字面量 grep 做守卫是不可靠的」—— 硬写一条会在第一次运行时就误报，
    然后被人用 `xfail` 关掉，守卫等于没有。）
    """
    produced = _produced_literals("route_type")
    undeclared = produced - ROUTE_TYPES
    assert undeclared == set(), (
        f"这些 route_type 有生产点但不在词表里：{sorted(undeclared)}。"
        "它们会被 record() 拒绝、被 suppress 吞掉 —— 静默丢失，没有症状。"
    )


def test_trial_specifically_reaches_the_database(tmp_path: Path) -> None:
    """#799 的正例：**走生产路径** `_record_trial_metric`，而不是直接调存储层。

    41 上实测的损失：**181 次 trial 运行，`agent_run_metrics` 里 trial 零行**。

    只调 `store.record(route_type="trial")` 是不够的 —— 那样测的是"存储层收不收这个值"，
    而真正会出问题的是"**生产点还在不在写**"（2026-11-04 复审指出：直接写入的测试
    在生产点被删掉后仍然会通过）。所以这里构造一个最小的 runtime 替身，
    调它自己的 `_record_trial_metric`，再看库里有没有行。
    """
    from types import SimpleNamespace

    from aiops_diagnostics.gateway import metrics_store as dims
    from aiops_diagnostics.gateway.gateway_runtime import GatewayRuntime

    assert dims.ROUTE_TRIAL == "trial"
    metrics = MetricsStore(tmp_path / "gateway.db")
    # 只取这一个方法真正用到的东西：`self.metrics_store` 与 `context.effective_tenant_id`。
    # 不构造整个 GatewayRuntime —— 那需要平台、凭据与一堆与指标无关的装配。
    runtime = SimpleNamespace(
        metrics_store=metrics,
        _record_metric=GatewayRuntime._record_metric.__get__(SimpleNamespace(metrics_store=metrics)),
    )
    context = SimpleNamespace(effective_tenant_id=TENANT)

    GatewayRuntime._record_trial_metric(runtime, context, "completed", __import__("time").monotonic())

    rows = metrics.list_runs(TENANT, route_type=dims.ROUTE_TRIAL)
    assert len(rows) == 1 and rows[0]["route_type"] == "trial"
    assert rows[0]["outcome"] == "completed"


def test_an_unknown_value_is_still_rejected():
    """对照组：放宽词表不能放宽成「什么都收」。"""
    from aiops_diagnostics.gateway.metrics_store import check_dimension

    for name, value in (
        ("route_type", "not_a_route"),
        ("outcome", "not_an_outcome"),
        ("retrieval_status", "not_a_status"),
    ):
        with pytest.raises(MetricsValidationError):
            check_dimension(name, value)


def test_an_empty_route_filter_is_still_rejected(tmp_path: Path) -> None:
    """`?route_type=`（空串）必须继续是 422，不能悄悄变成「返回空列表」。

    `list_runs()` 的过滤参数里，`None` 是「不按路由过滤」，空串**不是** —— 它是
    「按一个空路由过滤」，那不是一个合法的路由。第一版把两者一起放过了
    （2026-11-04 复审指出：原本 422 的请求变成了空列表），这条盯住它。
    """
    metrics = MetricsStore(tmp_path / "gateway.db")
    metrics.record(tenant_id=TENANT, route_type="qa", outcome="completed")
    # `None` = 不过滤，照常返回全部。
    assert len(metrics.list_runs(TENANT, route_type=None)) == 1
    # 空串 = 一个非法的过滤值。
    with pytest.raises(MetricsValidationError):
        metrics.list_runs(TENANT, route_type="")


def test_a_null_retrieval_status_is_rejected_at_validation_not_at_sqlite(tmp_path: Path) -> None:
    """`retrieval_status=None` 必须被判为**校验错误**，不能漏到 SQLite 的 NOT NULL。

    漏过去的话抛的是 `sqlite3.IntegrityError`，而 `_record_metric` 的
    `contextlib.suppress(Exception)` 会把两者一起吞掉 —— 症状与「词表里少一个成员」
    完全一样：一行都没有，且没有任何提示。2026-11-04 复审指出。

    合法的空值是**空字符串 `""`**（它本来就在集合里），不是 `None`。
    """
    metrics = MetricsStore(tmp_path / "gateway.db")
    with pytest.raises(MetricsValidationError):
        metrics.record(tenant_id=TENANT, route_type="qa", outcome="completed", retrieval_status=None)
    # 空字符串仍然是合法的，照常落行。
    metrics.record(tenant_id=TENANT, route_type="qa", outcome="completed", retrieval_status="")
    assert len(metrics.list_runs(TENANT, limit=5)) == 1


#: #799 的剩余欠账，按文件名钉住：**只许减少**。
#:
#: `trial`：`consumer_trial` / `dify_workflow_trial` 的 `JobRegistration` 只登记
#: 「这是哪种作业」，真正的指标行由 `_record_trial_metric` 经 `ROUTE_TRIAL` 写。
#: `qa`：operator 预览路径（`gateway_runtime:1138/:1190`、`gateway_api:6395`）今天
#: 压根不写指标行 —— 它与 #799 的其余部分分开，见该 issue 的评论。
BARE_LITERAL_ALLOWED = {
    ("consumer/consumer_trial.py", "trial"),
    ("dify/dify_workflow_trial.py", "trial"),
    ("gateway/gateway_runtime.py", "qa"),
    ("gateway/gateway_api.py", "qa"),
}


def test_the_allow_list_only_shrinks():
    """白名单不能变成新的第二份词表：条目数只许减少。"""
    assert len(BARE_LITERAL_ALLOWED) <= 4, (
        "白名单变长了 —— 新增生产点应引用 metrics_store 的常量，不是加到这里"
    )


def test_producers_reference_the_named_constants():
    """生产点引用 `ROUTE_*` 常量，而不是各写一份字面量。

    `route_type="trial"` 写在五个文件里时，「加一个路由类型要改哪几行」没有答案；
    引用常量之后，`ROUTE_TYPES` 与常量表就是同一处。这条测试钉住**不再增加新的一处**。
    """
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "metrics_store.py":
            continue
        rel = path.relative_to(SRC).as_posix()
        for value in re.findall(r'\broute_type\s*=\s*"([a-z0-9_]+)"', path.read_text(encoding="utf-8")):
            if (rel, value) not in BARE_LITERAL_ALLOWED:
                offenders.append(f"{rel}: route_type={value!r}")
    assert offenders == [], "这些生产点写了裸字面量，应改为引用 metrics_store 的常量：\n  " + "\n  ".join(
        offenders
    )
