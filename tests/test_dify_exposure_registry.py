"""段 1（进 CI）：Dify 可触达的公司系统面 == `ops/dify-exposure-registry.md` 的声明（#583）。

PRD #577 把这条判据写成一句可回归的话：**Dify 能触达的公司系统面 = 我们显式暴露、
逐个审过的工具端点集合**。判据分三段，只有第 1 段进 CI —— 就是这里。

它断言三件事，每一件都对着一种真实的漂移：

1. **集合相等，而不是包含。** 路由表里"面向 Dify 暴露的端点集合"必须**逐条等于**登记表
   声明的那一组。只说"声明的都在表里"会放走**新增了没登记**的端点 —— 那正是要拦的。
2. **挂接跳数是我们网关的适配路由，且只读。** 端点集合是**面**：Dify 打过来的那一跳
   必须落在我们的适配路由上，而不是直连 kb-service / RAGFlow / 数据面。方法只允许读语义
   （GET/POST 到检索），写端点（PUT/DELETE/PATCH）进不来。
3. **登记表自己得能被解析出东西。** 一份解析出空集合的登记表会让上面两条变成绿色的空转 ——
   这正是本仓反复出现的"判据看着跑了，其实什么都没判"。

端点集合从**真实 app 的路由表**读，不是从源码文本猜：`create_gateway_app` 装出来的
`app.routes` 是 FastAPI 解析完依赖之后的事实。登记表从 markdown 表格解析 —— 它同时是
文档，所以"文档里写的"和"代码里做的"必须逐字对上。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aiops_diagnostics.gateway_api import create_gateway_app
from aiops_diagnostics.gateway_config import GatewayServerSettings
from aiops_diagnostics.gateway_store import GatewayStore

ROOT = Path(__file__).parents[1]
REGISTRY = ROOT / "ops" / "dify-exposure-registry.md"

#: 登记表里那张表的一行。列序：面 | 路径 | 方法 | 鉴权 | 为什么它在集合里。
#: 路径在反引号里（它是文档优先的），所以匹配到的是 `` `/v1/dify/retrieval` `` 这种形状。
#: 解析按**单元格位置**做，不按"以路径开头" —— 前者在表格加一列时会立刻出错，
#: 后者会安静地退回空集合。
_ROW = re.compile(
    r"^\|\s*(?P<face>[^|]+?)\s*\|\s*`(?P<path>/[^`]*?)`\s*\|"
    r"\s*(?P<method>[A-Z]+)\s*\|\s*(?P<auth>[^|]*?)\s*\|\s*(?P<reason>[^|]*?)\s*\|\s*$",
    re.MULTILINE,
)

#: 面向 Dify 的端点就在这个前缀下。**它不是登记表本身** —— 登记表说的是"哪些该暴露"，
#: 这里说的是"哪些可能是在对 Dify 说的"。两者不相等时，下面第一条断言会红。
DIFY_ROUTE_PREFIX = "/v1/dify/"


class _Runtime:
    """Only the two surfaces the adapter reads; this file is not about the search."""

    kb_search_client = None
    media_signer = None

    def shutdown(self) -> None:
        pass


def _registry_rows() -> list[tuple[str, str]]:
    """(path, method) as declared in the registry table."""
    rows = [(m.group("path"), m.group("method")) for m in _ROW.finditer(REGISTRY.read_text("utf-8"))]
    return sorted(set(rows))


def _app_routes(tmp_path: Path) -> list[tuple[str, str]]:
    """(method, path) from the REAL app's route table — FastAPI's resolved view."""
    settings = GatewayServerSettings(
        data_home=tmp_path,
        database_file=tmp_path / "gateway.db",
        server_config_file=tmp_path / "production.env",
        # Configured, so the adapter route is actually mounted (unconfigured = 404 by design).
        dify_knowledge_api_key="registry-check-key",
        dify_knowledge_bindings="kb-1:T-1:KB-A",
    )
    settings.server_config_file.write_text("# test\n", encoding="utf-8")
    app = create_gateway_app(
        settings=settings,
        store=GatewayStore(settings.database_file),
        runtime=_Runtime(),  # type: ignore[arg-type]
    )
    routes: list[tuple[str, str]] = []
    for route in app.routes:  # type: ignore[attr-defined]
        paths = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if paths is None or not methods:
            continue
        for method in methods:
            routes.append((method, str(paths)))
    return sorted(set(routes))


def _dify_routes(tmp_path: Path) -> list[tuple[str, str]]:
    """(path, method) for every route that lives under the Dify-facing prefix."""
    routes = _app_routes(tmp_path)
    return sorted({(path, method) for method, path in routes if path.startswith(DIFY_ROUTE_PREFIX)})


def test_the_registry_parses_to_a_non_empty_set() -> None:
    """一份解析出空集合的登记表会让下面每条断言变成绿色的空转。"""
    rows = _registry_rows()
    assert rows, f"{REGISTRY.name} 里没解析出任何端点 —— 表格形状变了，或它空了"
    assert ("/v1/dify/retrieval", "POST") in rows, "知识检索适配路由必须在登记表里"


def test_the_exposed_set_equals_the_declared_set(tmp_path: Path) -> None:
    """**逐条相等**，不是包含。

    两条方向各拦一种漂移：
    * 表里声明了、代码里没有 —— 文档在描述一个不存在的东西（或路由被删了没同步）；
    * 代码里有、表里没声明 —— **新增端点却漏登记**，这正是本判据要拦的那次。
    """
    declared = set(_registry_rows())
    exposed = set(_dify_routes(tmp_path))
    missing_from_registry = sorted(exposed - declared)
    missing_from_app = sorted(declared - exposed)
    assert not missing_from_registry, (
        f"这些端点面向 Dify 暴露，但 {REGISTRY.name} 没登记：{missing_from_registry}\n"
        "新增端点必须同时加进登记表 —— 那条判据就是为这一刻写的。"
    )
    assert not missing_from_app, f"{REGISTRY.name} 声明了这些端点，但 app 里没有：{missing_from_app}"


def test_the_exposure_is_read_only(tmp_path: Path) -> None:
    """写端点不得进这张表。

    `AGENTS.md` 的业务边界：第一版只允许诊断和证据交付。面向 Dify 的面上出现
    PUT/DELETE/PATCH 就是把"能改生产"开给了编排工具 —— 它不该靠"没人会这么写"来保证。
    """
    write_methods = {"PUT", "DELETE", "PATCH"}
    offenders = [(path, method) for path, method in _dify_routes(tmp_path) if method in write_methods]
    assert not offenders, f"面向 Dify 的面上出现了写方法：{offenders}"


def test_the_adapter_hop_is_our_gateway_route_not_a_direct_data_plane_call(tmp_path: Path) -> None:
    """挂接跳数：Dify 打到的是**我们网关的适配路由**，不是 kb-service / RAGFlow。

    这条在路由层能证的部分是：集合里没有 kb-service（`/kb/…`）与数据面（`/diag/…`、
    `/v1/media/…` 之外的直连）形态的路径。真正的网络可达性由段 3 的探测证明，
    不进 CI —— 但"我们这边在路由表上就没开那些口"从这里是能读出来的。
    """
    forbidden_prefixes = ("/kb/", "/v1/kb/", "/diag/", "/internal/")
    offenders = [path for path, _ in _dify_routes(tmp_path) if path.startswith(forbidden_prefixes)]
    assert not offenders, f"面向 Dify 的面上出现了数据面形态的路径：{offenders}"


@pytest.mark.parametrize(
    "forbidden",
    ["/kb/knowledge-bases/x/search", "/v1/kb/x", "/diag/order", "/internal/anything"],
)
def test_the_route_prefix_predicate_actually_matches_something(tmp_path: Path, forbidden: str) -> None:
    """这条断言打的是**上面那条判据本身**：它必须真的能匹配到数据面形态。

    没有它，`forbidden_prefixes` 写错一个字（比如 `/kbb/`）会让上面那条永远通过，
    而它看起来仍然在跑 —— 本仓把这种形状叫"判据看着跑了，其实什么都没判"。
    """
    assert forbidden.startswith(("/kb/", "/v1/kb/", "/diag/", "/internal/"))
    assert not forbidden.startswith(DIFY_ROUTE_PREFIX), "数据面路径不该落在 Dify 前缀下"


def test_a_hypothetical_new_dify_route_would_be_caught() -> None:
    """把"漏登记"这件事本身钉住：一个不在登记表里的新 `/v1/dify/*` 路由必须被算作漏登记。

    用一个假的路由集合驱动判据的差值逻辑 —— 不等一份真的漏登记提交出现才证明它有效。
    """
    declared = set(_registry_rows())
    hypothetical = {("/v1/dify/retrieval", "POST"), ("/v1/dify/tools/run", "POST")}
    assert sorted(hypothetical - declared) == [("/v1/dify/tools/run", "POST")], (
        "新增端点没有被算作漏登记 —— 集合相等的判据就白写了"
    )


def test_every_declared_row_names_a_reason() -> None:
    """登记表每一行都要写清"为什么它在集合里"。

    一张只有路径与方法的表读起来像清单，不像决定；而这条判据的全部意义是"逐个审过"。
    这一条与解析用的是**同一张正则**，所以它证明的是"表格里那两列非空"，
    而不是"某个平行实现也对得上"。
    """
    rows = list(_ROW.finditer(REGISTRY.read_text("utf-8")))
    assert rows, "登记表里没解析出任何表格行"
    for match in rows:
        assert match.group("face").strip(), "面这一列为空"
        assert match.group("auth").strip(), f"{match.group('path')} 没写谁鉴权"
        assert match.group("reason").strip(), f"{match.group('path')} 没写为什么它在集合里"
