"""新版WF01的可发布编排契约：产品可改开关/提示词，取数来源和授权路径固定。"""

from urllib.parse import urlsplit

PATHS = {kind: "/v1/internal/consumer-data/" + kind for kind in ("knowledge", "status", "billing")}
TYPES = {
    "start": "start",
    "data_route": "if-else",
    "status_enabled": "if-else",
    "billing_enabled": "if-else",
    "cpms_status": "http-request",
    "cpms_billing": "http-request",
    "photo_gate": "if-else",
    "photo": "llm",
    "photo_context": "variable-aggregator",
    "knowledge": "http-request",
    "data_context": "variable-aggregator",
    "answer": "llm",
    "end": "answer",
    "disabled": "answer",
}
EDGES = {
    ("start", "source", "data_route"),
    ("data_route", "status", "status_enabled"),
    ("data_route", "billing", "billing_enabled"),
    ("data_route", "false", "photo_gate"),
    ("status_enabled", "true", "cpms_status"),
    ("status_enabled", "false", "disabled"),
    ("billing_enabled", "true", "cpms_billing"),
    ("billing_enabled", "false", "disabled"),
    ("photo_gate", "true", "photo"),
    ("photo_gate", "false", "photo_context"),
    ("photo", "source", "photo_context"),
    ("photo_context", "source", "knowledge"),
    ("knowledge", "source", "data_context"),
    ("cpms_status", "source", "data_context"),
    ("cpms_billing", "source", "data_context"),
    ("data_context", "source", "answer"),
    ("answer", "source", "end"),
}


class ConsumerFlowError(ValueError):
    """可向运营展示的本地编排拒绝原因，不包含上游正文。"""


def publication_settings(workflow, ceiling, public_base, *, require_split=False):
    graph = workflow.get("graph", {})
    nodes = {n.get("id"): n.get("data", {}) for n in graph.get("nodes", [])}
    variables = nodes.get("start", {}).get("variables", [])
    split = require_split or "data_route" in nodes or any(v.get("variable") == "data_kind" for v in variables)
    split = split or any("/consumer-data/" in n.get("url", "") for n in nodes.values())
    if not split:
        return ceiling

    def require(ok, reason):
        if not ok:
            raise ConsumerFlowError("WF01编排不完整：" + reason)

    require(
        len(nodes) == len(graph.get("nodes", [])) and set(nodes) == set(TYPES), "必需节点缺失或存在未登记节点"
    )
    require(all(n.get("type") == TYPES[key] for key, n in nodes.items()), "节点类型已改变")
    edges = [(e.get("source"), e.get("sourceHandle"), e.get("target")) for e in graph.get("edges", [])]
    require(len(edges) == len(EDGES) and set(edges) == EDGES, "取数分支连线不符合契约")
    base = public_base.rstrip("/")
    parsed = urlsplit(base)
    require(
        bool(parsed.netloc) and (parsed.scheme == "https" or parsed.hostname in {"127.0.0.1", "localhost"}),
        "缺少可信回调地址",
    )
    require(
        not parsed.query and not parsed.fragment and not parsed.username and parsed.path in {"", "/"},
        "回调地址必须为服务根地址",
    )
    for kind, node_id in [("knowledge", "knowledge"), ("status", "cpms_status"), ("billing", "cpms_billing")]:
        n = nodes[node_id]
        require(
            n.get("url") == base + PATHS[kind] and n.get("method", "").lower() == "post",
            node_id + "取数地址或方法错误",
        )
        headers = [line.strip() for line in n.get("headers", "").splitlines() if line.strip()]
        require(
            headers
            == ["Authorization: Bearer {{#start.data_grant#}}", "Content-Type: text/plain; charset=utf-8"],
            node_id + "凭据必须直接绑定且禁止业务身份头",
        )
        require(
            n.get("authorization", {}).get("type") == "no-auth" and not n.get("params"),
            node_id + "不能另加认证或查询参数",
        )
        body = n.get("body", {})
        require(
            body
            == (
                {"type": "raw-text", "data": [{"type": "text", "value": "{{#photo_context.output#}}"}]}
                if kind == "knowledge"
                else {"type": "none", "data": []}
            ),
            node_id + "请求体错误",
        )
        require(
            not n.get("retry_config", {}).get("retry_enabled")
            and not n.get("error_strategy")
            and n.get("ssl_verify") is not False,
            node_id + "不允许重试、忽略失败或关闭TLS验证",
        )

    def cases(key, expected):
        actual = []
        for case in nodes[key].get("cases", []):
            require(
                case.get("logical_operator") == "and" and len(case.get("conditions", [])) == 1,
                key + "条件错误",
            )
            c = case["conditions"][0]
            actual.append(
                (
                    case.get("case_id"),
                    c.get("variable_selector"),
                    c.get("comparison_operator"),
                    c.get("value"),
                )
            )
        require(actual == expected, key + "分支必须使用本轮可信能力")

    cases(
        "data_route",
        [
            ("status", ["start", "data_kind"], "is", "status"),
            ("billing", ["start", "data_kind"], "is", "billing"),
        ],
    )
    for kind in ("status", "billing"):
        cases(kind + "_enabled", [("true", ["env", "cpms_" + kind + "_enabled"], "is", "true")])
    cases("photo_gate", [("true", ["start", "has_photo"], "is", "yes")])
    require(
        {v.get("variable") for v in variables} == {"data_grant", "language", "has_photo", "data_kind"},
        "开始输入错误",
    )
    require(
        nodes["data_context"].get("variables")
        == [[key, "body"] for key in ("knowledge", "cpms_status", "cpms_billing")],
        "本轮结果聚合错误",
    )
    require(nodes["data_context"].get("output_type") == "string", "结果必须为字符串")
    require(
        nodes["photo_context"].get("variables") == [["photo", "text"], ["start", "has_photo"]],
        "图片聚合仅允许图片线索，不允许凭据或其他输入",
    )
    require(nodes["end"].get("answer") == "{{#answer.text#}}", "答复引用错误")
    require(nodes["disabled"].get("answer") == '{"kind":"unavailable","faq_id":""}', "关闭提示错误")
    for key, node in nodes.items():
        if node.get("type") == "llm":
            require("data_grant" not in str(node), key + "不得向模型传递凭据")
    require(
        "{{#data_context.output#}}" in str(nodes["answer"].get("prompt_template")), "模型没有接收本轮结果"
    )
    env = {v.get("name"): v for v in workflow.get("environment_variables", [])}
    require(len(env) == len(workflow.get("environment_variables", [])), "环境变量重复")
    flags = {}
    for kind, setting in [("status", "live_order_status"), ("billing", "live_billing_record")]:
        entry = env.get("cpms_" + kind + "_enabled", {})
        require(
            entry.get("value_type") == "string" and entry.get("value") in {"true", "false"},
            "能力开关必须为true或false",
        )
        flags[setting] = ceiling[setting] and entry["value"] == "true"
    return {**ceiling, **flags, "callback_version": 2}
