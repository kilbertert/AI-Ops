"""一个 CPMS 只读工具是什么 —— **仓内唯一声明**（`operator_tools.json`）。

## 它解决的是什么

同一个工具名曾经在仓库里写了五遍：网关的路径表、证据字段表、发布白名单、Dify provider 的
OpenAPI 生成，以及节点侧的装配表。加一个工具要同步改五处，其中一处是 Java —— 于是
「值不值得做成工具」这个决定被变更成本主导，而不是被需求主导。

现在这五处都从这里读，或由它校验。**加一个工具只加一条声明。**

## 边界（有意为之，不是还没做完）

- 请求只能是 `GET` + 相对路径 + 声明里的查询串。「任意 HTTP / SQL 工具」仍然不在方案内。
- `response.kind` 只有两种：`single`（一个对象）与 `page`（一页记录 + 派生标记）。
  需要**新的**响应形状时，改的是引擎（两侧各一处），不是每个工具各写一遍 ——
  这与「每个工具改一次 Java」是两件事。
- 声明里没有凭据、没有地址、没有租户：它只说形状，不说去哪。

## 读取时机

模块导入即校验，任何不合格都抛 :class:`ToolDeclarationError`。**不降级**：一份写错的声明
如果被读成「少一个工具」，故障会以「清单里没有那个工具」的形状出现在运营页面上，而不是
出现在启动日志里。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

DECLARATION_NAME = "operator_tools.json"
SUPPORTED_SCHEMA = "aiops-operator-tools/1"

#: 工具名的形状。它同时出现在清单、provider OpenAPI 与证据里，不能有歧义。
_NAME = re.compile(r"[a-z][a-z0-9_]{2,63}")
#: 输入名同理：它既是占位符，也是证据里的键。
_INPUT = re.compile(r"[a-z][a-z0-9_]{1,63}")
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{1,63})\}")
#: 响应形状。新增一种要同时改两侧引擎，因此是显式枚举而不是自由字符串。
KINDS = ("single", "page")
#: 运行类型。工具必须声明它能出现在哪一类运行里 —— 「无订单回合也能带工具」不等于
#: 「任何工具都能在无订单回合里跑」：订单诊断工具没有订单就没有可取证的对象。
CONTEXTS = ("order", "conversation")


class ToolDeclarationError(ValueError):
    """声明缺失、不可读或不合格。调用方把它变成**启动失败**，不降级。"""


@dataclass(frozen=True, slots=True)
class OperatorTool:
    name: str
    description: str
    inputs: tuple[str, ...]
    model_inputs: tuple[str, ...]
    applies_to: tuple[str, ...]
    method: str
    path_template: str
    query: tuple[tuple[str, str, str], ...]
    source: str
    kind: str
    row_path: str
    page_size: int | None
    flags: dict[str, object]
    evidence_fields: tuple[str, ...]

    def request(self, values: dict[str, str]) -> dict[str, str]:
        """这一次要发的请求。**路径与查询串都在这里物化** —— 节点只执行它。

        查询参数表里每一项要么是常量（``value``），要么来自输入（``from``）；输入为空
        时那一项整条省略。于是「参数只能影响过滤器、分页上限写死在声明里」是结构事实，
        而不是拼字符串时小心出来的结果。
        """
        path = self.path_template
        for placeholder in _PLACEHOLDER.findall(self.path_template):
            value = values.get(placeholder, "")
            if not value:
                raise ToolDeclarationError(f"tool {self.name} is missing input {placeholder}")
            path = path.replace("{" + placeholder + "}", value)
        parts: list[str] = []
        for name, kind, raw in self.query:
            if kind == "value":
                parts.append(f"{name}={raw}")
                continue
            provided = values.get(raw, "")
            if provided:
                parts.append(f"{name}={quote(provided, safe='')}")
        return {"method": self.method, "path": path, "query": "&".join(parts)}

    def input_schema(self) -> str:
        """发布清单里的参数 schema。**只有模型可以提议的那部分**出现在这里。

        运行绑定的输入（订单号）不在 schema 里：模型不能选它，写进去等于给它一个入口。
        排序固定，因为清单身份是原始字节的 sha256。
        """
        properties = ",".join(f'"{name}":{{"type":"string","maxLength":80}}' for name in self.model_inputs)
        return (
            '{"type":"object","properties":{' + properties + '},"required":[],"additionalProperties":false}'
        )

    def declared_inputs(self, values: dict[str, str]) -> dict[str, str]:
        """这一次运行的输入。只保留声明里列过的那些，不多带。"""
        return {name: str(values.get(name, "")) for name in self.inputs}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ToolDeclarationError(message)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolDeclarationError(f"{field} must be a non-empty string")
    return value


def _object(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise ToolDeclarationError(f"{field} must be an object")
    return value


def _list(value: object, field: str) -> list:
    if not isinstance(value, list):
        raise ToolDeclarationError(f"{field} must be a list")
    return value


def _parse_query(name: str, raw: object, inputs: set[str]) -> tuple[tuple[str, str, str], ...]:
    """查询参数表。每项要么是常量（``value``），要么来自输入（``from``），二选一。

    分页上限写在这里而不是由调用方给：调用方能给的东西就不是上限。
    """
    if raw is None:
        return ()
    items = _list(raw, f"{name}: request.query")
    parsed: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for item in items:
        item = _object(item, f"{name}: request.query item")
        param = _text(item.get("name"), f"{name}: request.query name")
        _require(bool(_INPUT.fullmatch(param)), f"{name}: bad query parameter name {param!r}")
        _require(param not in seen, f"{name}: duplicate query parameter {param}")
        seen.add(param)
        if "value" in item:
            _require("from" not in item, f"{name}: query parameter {param} cannot be both value and from")
            parsed.append((param, "value", _text(item.get("value"), f"{name}: query value")))
            continue
        source = _text(item.get("from"), f"{name}: query source")
        _require(source in inputs, f"{name}: query parameter {param} reads an undeclared input {source!r}")
        parsed.append((param, "from", source))
    return tuple(parsed)


def _tool(raw: object, seen: set[str]) -> OperatorTool:
    raw = _object(raw, "every tool")
    name = _text(raw.get("name"), "tool name")
    _require(bool(_NAME.fullmatch(name)), f"tool name is not a valid identifier: {name!r}")
    _require(name not in seen, f"duplicate tool: {name}")
    seen.add(name)

    inputs = _list(raw.get("inputs"), f"{name}: inputs")
    for item in inputs:
        _require(isinstance(item, str) and bool(_INPUT.fullmatch(item)), f"{name}: bad input {item!r}")

    request = _object(raw.get("request"), f"{name}: request")
    method = _text(request.get("method"), f"{name}: request.method")
    # 只读面只允许 GET。这条不是风格：POST 会写，而写不在第一版的边界里。
    _require(method == "GET", f"{name}: only GET is allowed, got {method!r}")
    template = _text(request.get("path_template"), f"{name}: request.path_template")
    _require(
        template.startswith("/") and ".." not in template and "?" not in template,
        f"{name}: path_template must be an absolute path without query",
    )
    placeholders = set(_PLACEHOLDER.findall(template))
    # 路径占位符只能来自声明过的输入；反过来不必相等 —— 站点查询的输入只进查询串。
    _require(
        placeholders.issubset(set(inputs)),
        f"{name}: path placeholders {sorted(placeholders)} must come from inputs {sorted(inputs)}",
    )
    query = _parse_query(name, request.get("query", []), set(inputs))

    response = _object(raw.get("response"), f"{name}: response")
    kind = _text(response.get("kind"), f"{name}: response.kind")
    _require(kind in KINDS, f"{name}: unknown response kind {kind!r}")
    row_path = _text(response.get("row_path"), f"{name}: response.row_path")
    page_size = response.get("page_size")
    if kind == "page":
        _require(
            isinstance(page_size, int) and 1 <= page_size <= 100,
            f"{name}: page response needs a page_size between 1 and 100",
        )
    else:
        _require(page_size is None, f"{name}: only page responses declare page_size")
        _require(
            bool(response.get("require_envelope_identity")),
            f"{name}: single response must check the envelope",
        )

    fields = _list(raw.get("evidence_fields"), f"{name}: evidence_fields")
    _require(bool(fields), f"{name}: evidence_fields must be a non-empty list")
    for field in fields:
        _require(isinstance(field, str) and bool(field.strip()), f"{name}: bad evidence field {field!r}")
    _require(len(set(fields)) == len(fields), f"{name}: duplicate evidence field")

    flags = _object(response.get("flags", {}), f"{name}: response.flags")

    model_inputs = _list(raw.get("model_inputs", []), f"{name}: model_inputs")
    _require(
        set(model_inputs).issubset(set(inputs)),
        f"{name}: model_inputs must be a subset of inputs",
    )
    applies_to = _list(raw.get("applies_to"), f"{name}: applies_to")
    _require(bool(applies_to), f"{name}: applies_to must list the runs this tool may serve")
    for item in applies_to:
        _require(item in CONTEXTS, f"{name}: unknown run type {item!r}")

    return OperatorTool(
        name=name,
        description=_text(raw.get("description"), f"{name}: description"),
        inputs=tuple(inputs),
        model_inputs=tuple(model_inputs),
        applies_to=tuple(applies_to),
        method=method,
        path_template=template,
        query=query,
        source=_text(raw.get("source"), f"{name}: source"),
        kind=kind,
        row_path=row_path,
        page_size=page_size if isinstance(page_size, int) else None,
        flags=dict(flags),
        evidence_fields=tuple(fields),
    )


@lru_cache(maxsize=8)
def _parse(raw: str) -> tuple[OperatorTool, ...]:
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - 声明坏了就是启动失败
        raise ToolDeclarationError("operator tool declaration is not valid JSON") from exc
    body = _object(body, "declaration")
    _require(body.get("schema") == SUPPORTED_SCHEMA, "unsupported operator tool declaration schema")
    declared = _list(body.get("tools"), "tools")
    _require(bool(declared), "declaration must list at least one tool")
    seen: set[str] = set()
    return tuple(_tool(item, seen) for item in declared)


def load(path: Path | None = None) -> tuple[OperatorTool, ...]:
    """读声明。``path`` 缺省为包内那份。"""
    target = path or Path(__file__).with_name(DECLARATION_NAME)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolDeclarationError(f"operator tool declaration is unavailable: {target}") from exc
    return _parse(raw)


def tools() -> tuple[OperatorTool, ...]:
    return load()


def by_name() -> dict[str, OperatorTool]:
    return {tool.name: tool for tool in tools()}


def names() -> tuple[str, ...]:
    return tuple(tool.name for tool in tools())


def tool(name: str) -> OperatorTool:
    found = by_name().get(name)
    if found is None:
        # 拒绝判据没有放松，只是从编译期常量变成了数据。
        raise ToolDeclarationError(f"undeclared operator tool: {name}")
    return found
