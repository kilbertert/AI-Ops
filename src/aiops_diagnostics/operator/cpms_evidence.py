"""管家节点工具契约、参数放行、证据投影与上游只读 HTTP 客户端。

从 `operator_cpms.py` 拆出，使该模块只承担无状态契约校验、证据投影与错误归一，
与 `OperatorCpms` 的运行期状态机和并发锁解耦。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping

from aiops_diagnostics.common.bounded_http import (
    ENVELOPE_SUCCESS_CODES,
    ErrorMapping,
    RequestSpec,
    json_body,
    parse_code_data_envelope,
    request_json,
)
from aiops_diagnostics.common.scalar_rules import format_stored_time, now_utc
from aiops_diagnostics.data.sources import GUN_COLUMN_NAMES
from aiops_diagnostics.execution import execution_manifest
from aiops_diagnostics.identity.query_scope import resolve_query_scope
from aiops_diagnostics.identity.scope_context import is_tenant_level_account
from aiops_diagnostics.operator import (
    operator_agent,
    operator_conversation,
    operator_tools,
)
from aiops_diagnostics.operator.operator_conversation import OperatorRefused

SCENARIO = "operator_order_diagnosis"
PREFIX = "operator-cpms-node:"
#: 工具名、路径、输入与证据字段全部来自 `operator_tools.json` —— 仓内唯一声明。
TOOL_NAMES = operator_tools.names()

NODE_FAILURE_CODES = {
    "model_quota_exhausted": "OPERATOR_MODEL_QUOTA_EXHAUSTED",
    "model_failed": "OPERATOR_DEPENDENCY_UNAVAILABLE",
    "node_failed": "OPERATOR_DEPENDENCY_UNAVAILABLE",
    "model_stream_incomplete": "OPERATOR_MODEL_OUTPUT_UNSTABLE",
}

#: 节点上报的工具拒绝 reason 前缀（原始拒绝码跟在冒号后）。
TOOL_REFUSED_PREFIX = "tool_refused:"

#: 可以回喂模型的业务拒绝：模型拿到提示后能修正参数、补齐前置依赖或如实告知用户。
FEEDBACK_REFUSALS = {
    "OPERATOR_OFFLINE_UNAVAILABLE": (
        "离线资料暂不可用或范围过大，请缩小范围或联系管理员；不能据此判断没有离线。"
    ),
    "OPERATOR_ORDER_REQUIRED": "缺少订单号；请向用户确认要查询的订单后再调用。",
    "OPERATOR_ORDER_NOT_REFERENCED": "只能查询用户本句提到或已选定的订单；请向用户确认订单号。",
    "OPERATOR_ORDER_CONFLICT": "每轮只能查询一笔订单；与当前订单不同请向用户说明不能同时查询。",
    "OPERATOR_TOOL_ARGUMENTS_INVALID": "参数未通过校验；参数值必须来自用户原话，不能猜测。",
    "OPERATOR_TOOL_ARGUMENTS_CHANGED": "同一工具本轮只能使用同一组参数；请沿用之前的参数。",
    "OPERATOR_ANALYSIS_INVALID": "该工具不接受参数；需先取得订单证据后再调用。",
    "OPERATOR_DETAIL_REQUIRED": "需先调用 cpms_order_detail 读取订单详情。",
    "OPERATOR_TARIFF_REQUIRED": "需先调用 cpms_order_tariff 读取计费模板。",
    "OPERATOR_EVIDENCE_MISSING": "需先取得该工具依赖的订单证据再调用。",
    "OPERATOR_SITE_SCOPE_REQUIRED": "当前账号没有可见站点；请如实告知用户。",
    "CPMS_INVALID_RESPONSE": "上游返回的数据无法识别；如实告知暂时无法查询，不要编造。",
    "CPMS_SCOPE_MISMATCH": "该订单不在当前账号可见范围或不存在；如实告知查无此单或无权查看。",
}

#: 运行日志行级摘要的每工具显示字段白名单（ADR-0251）。
TOOL_SUMMARY_FIELDS = {
    "cpms_order_detail": [
        ("status", "状态"),
        ("totalAmount", "应付"),
        ("payAmount", "实付"),
        ("refundAmount", "退款"),
        ("electricity", "电量"),
        ("stationName", "站点"),
    ],
    "cpms_order_tariff": [("feeTemplate", "费率模板"), ("periodFeeDetail", "分时"), ("missing", "缺项")],
    "cpms_order_billing": [
        ("serviceFee", "服务费"),
        ("electricityFee", "电费"),
        ("totalAmount", "合计"),
        ("payAmount", "实付"),
        ("vatFee", "税费"),
    ],
    "cpms_charging_samples": [("total", "样本数"), ("count", "样本数")],
    "cpms_site_search": [
        ("total", "站点数"),
        ("count", "站点数"),
        ("shown_sites", "展示"),
        ("limited", "超限"),
    ],
    "cpms_station_plan": [("name", "计划"), ("energy", "电量"), ("power", "功率"), ("date", "日期")],
    "cpms_order_amount_check": [("conclusion", "结论"), ("checks", "检查"), ("limitations", "限制")],
    "cpms_order_process_stats": [
        ("sample_count", "样本数"),
        ("average_power_kw", "平均功率"),
        ("complete_timeline", "时序完整"),
    ],
    "cpms_charging_timeline": [("total", "记录数"), ("samples_returned", "返回"), ("truncated", "截断")],
    "cpms_device_offline": [("summary", "摘要"), ("window", "窗口")],
    "cpms_order_communications": [("association", "关联"), ("sources", "来源"), ("window_truncated", "截断")],
}

#: 工具授权契约版本：节点领取授权时必须逐字声明。
TOOL_CONTRACT = "request-target/1"
OBJECT_CONTRACT = "request-target/2"
TOOL_CONTRACT_HEADER = "X-AI-Ops-Tool-Contract"

#: 证据 ``data`` 里的派生形状字段；逐字核验只比对出处原值，跳过派生字段。
DERIVED_EVIDENCE_FIELDS = frozenset(
    {
        "absence",
        "complete_timeline",
        "current",
        "ordering_guaranteed",
        "sample_limit",
        "samples_returned",
        "size",
        "truncated",
    }
)

#: 工具参数的禁止字符（ADR-0176）。
_ARGUMENT_FORBIDDEN = ("%", "_", "\\", "\r", "\n")

SITE_TOOL = "cpms_site_search"
TIMELINE_TOOL = "cpms_charging_timeline"
TIMELINE_LIMIT = 64
TIMELINE_FIELDS = ("_ts", *(name for name in GUN_COLUMN_NAMES if name != "batteryMinTemperature"))
_TIMELINE_DEVICE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TIMELINE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d{1,3})?)?")

DETAIL_FIELDS = list(operator_tools.tool("cpms_order_detail").evidence_fields)
SAMPLE_FIELDS = list(operator_tools.tool("cpms_charging_samples").evidence_fields)


def _arg_summary(task, tool):
    """工具调用入参的一行展示摘要：``k=v`` 拼接、标量截断、列表取前三个、字典跳过。"""
    args = task.arguments.get(tool)
    if not isinstance(args, dict) or not args:
        return None
    parts = []
    for key, value in list(args.items())[:6]:
        if value is None or isinstance(value, dict):
            continue
        if isinstance(value, list):
            value = ",".join(str(v)[:12] for v in value[:3])
        parts.append(f"{key}={str(value)[:24]}")
    return "、".join(parts)[:48] or None


def _provenance(data):
    """证据 ``data`` 的出处投影：剥掉派生形状字段，只剩可逐字核验的上游原值。"""
    if not isinstance(data, dict):
        return data
    return {key: value for key, value in data.items() if key not in DERIVED_EVIDENCE_FIELDS}


def _declared(tool: str):
    """取工具声明；未声明的工具名直接拒绝。"""
    try:
        return operator_tools.tool(tool)
    except operator_tools.ToolDeclarationError as exc:
        raise OperatorRefused("OPERATOR_TOOL_DISABLED") from exc


def tool_request(tool: str, values: dict[str, str | list[str]]) -> dict[str, str]:
    """按工具名物化本次请求目标（路径与查询串的唯一物化点）。"""
    return _declared(tool).request(values)


def tool_inputs(tool: str, values: dict[str, str | list[str]]) -> dict[str, str]:
    """返回本次工具运行的声明输入。"""
    return _declared(tool).declared_inputs(values)


def run_values(
    tool: str, order_no: str, arguments: dict[str, str | list[str]] | None = None
) -> dict[str, str | list[str]]:
    """合成运行绑定输入（订单号）与模型提议参数。"""
    values = dict(arguments or {})
    if "order_no" in _declared(tool).inputs:
        values["order_no"] = order_no
    return values


def diagnoses_an_order(run) -> bool:
    """统一循环取证后才绑定对象；旧运行按原类型回读。"""
    return bool(run.order_no) if operator_agent.is_agent(run) else run.operation == SCENARIO


def allowed_in(tool: str, run) -> bool:
    """判断工具是否允许在当前运行上下文中调用。"""
    if operator_agent.is_agent(run):
        return tool in TOOL_NAMES
    context = "order" if diagnoses_an_order(run) else "conversation"
    return context in _declared(tool).applies_to or (
        context == "conversation" and "operator_conversation" in _declared(tool).applies_to
    )


def validate_tool_arguments(tool: str, arguments, question: str) -> dict[str, str | list[str]]:
    """校验模型提议的工具参数：键必须在声明中，且值必须是用户提问原文的子串。"""
    declared = _declared(tool)
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict) or set(arguments) - set(declared.inputs):
        raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
    cleaned: dict[str, str | list[str]] = {}
    for key, value in arguments.items():
        if tool == SITE_TOOL and key == "address_terms":
            if not isinstance(value, list) or len(value) > 4:
                raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
            terms = []
            for term in value:
                if (
                    not isinstance(term, str)
                    or not term.strip()
                    or len(term) > 80
                    or any(char in term for char in _ARGUMENT_FORBIDDEN)
                ):
                    raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
                original = operator_conversation.question_span(term, question)
                if not original:
                    raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
                if original not in terms:
                    terms.append(original)
            if sum(map(len, terms)) > 80:
                raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
            cleaned[key] = terms
            continue
        if not isinstance(value, str):
            raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
        forbidden = tuple(
            c
            for c in _ARGUMENT_FORBIDDEN
            if not (tool == "cpms_station_plan" and key == "timezone" and c == "_")
        )
        if len(value) > 80 or any(char in value for char in forbidden):
            raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
        stripped = value.strip()
        if not stripped:
            continue
        original = operator_conversation.question_span(stripped, question)
        if not original:
            raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
        if tool == "cpms_station_plan" and key in {"energy", "power"}:
            units = r"(?:kwh|千瓦时|度)" if key == "energy" else r"(?:kw(?!h)|千瓦(?!时))"
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", original) or not re.search(
                r"(?<![0-9.])" + re.escape(original) + r"\s*" + units, question, re.I
            ):
                raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
        cleaned[key] = original
    if tool == SITE_TOOL and cleaned.get("address") and "address_terms" in cleaned:
        raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
    return cleaned


def _proxy_tool(task, run, method, path, query):
    """白名单命中的工具名；查无对应授权目标时返回 ``None``。"""
    return next(
        (
            name
            for name in TOOL_NAMES
            if name in task.arguments
            and allowed_in(name, run)
            and tool_request(name, run_values(name, run.order_no, task.arguments.get(name)))
            == {"method": method, "path": path, "query": query}
        ),
        None,
    )


def still_authorized(binding, run, *, trial=False) -> bool:
    """冻结运行的模型/工具必须仍被当前应用授权，提示词可单独换版。"""
    if binding is None or binding.entry != "operator" or binding.route_revision < run.route_revision:
        return False
    if not run.publication_app_id:
        return binding.route_revision == run.route_revision
    if binding.app_id != run.publication_app_id:
        return False
    if trial or str(run.chat_binding).startswith("operator-trial:"):
        return binding.route_revision == run.route_revision
    try:
        frozen = execution_manifest.from_text(run.execution_manifest)
        current_text = binding.manifest_text
        if not operator_agent.is_agent(run) and not diagnoses_an_order(run):
            return binding.route_revision == run.route_revision
        current = execution_manifest.from_text(current_text)
        old_tools = {t["name"]: t["input_schema"] for t in json.loads(run.execution_manifest)["tools"]}
        new_tools = {t["name"]: t["input_schema"] for t in json.loads(current_text)["tools"]}
    except (ValueError, TypeError, KeyError):
        return False
    return (
        frozen.identity == run.expected_agent_version
        and all(
            getattr(frozen, key) == getattr(current, key)
            for key in ("provider_key", "adapter_key", "model_key", "endpoint", "history_policy")
        )
        and all(new_tools.get(name) == schema for name, schema in old_tools.items())
    )


def operator_scope(caller, *, allow_empty=False):
    if not caller.subject.b_user_id or caller.delegated or caller.data_scope.type not in {"all", "organ"}:
        raise OperatorRefused("OPERATOR_SITE_SCOPE_REQUIRED")
    scope = resolve_query_scope(caller)
    if (scope.site_ids == () and not allow_empty) or (
        scope.site_ids is None and not is_tenant_level_account(caller.subject.user_type)
    ):
        raise OperatorRefused("OPERATOR_SITE_SCOPE_REQUIRED")
    return scope


def minimized_response(body, tool):
    data = body["data"]
    fields = {"tenantId", *(DETAIL_FIELDS if tool == TOOL_NAMES[0] else SAMPLE_FIELDS)}
    if tool == TOOL_NAMES[0]:
        selected = {
            "orderNo": data["orderNo"],
            "siteId": data["siteId"],
            "chOrderInfoModel": {k: v for k, v in data["chOrderInfoModel"].items() if k in fields},
        }
    else:
        selected = {k: data[k] for k in ("total", "current", "size")}
        selected["records"] = [{k: v for k, v in row.items() if k in fields} for row in data["records"]]
        selected["samples_returned"] = len(data["records"])
        selected["absence"] = "no_samples" if not data["records"] else None
    return {"code": 0, "data": selected}


def validate_body(body, caller, order, tool):
    if (
        not isinstance(body, dict)
        or type(body.get("code")) is not int
        or body["code"] != 0
        or not isinstance(body.get("data"), dict)
    ):
        raise OperatorRefused("CPMS_INVALID_RESPONSE")
    data = body["data"]
    scope = operator_scope(caller)
    rows = [data.get("chOrderInfoModel")] if tool == TOOL_NAMES[0] else data.get("records")
    if not isinstance(rows, list) or len(rows) > 50:
        raise OperatorRefused("CPMS_INVALID_RESPONSE")
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("orderNo") != order
            or row.get("tenantId") != caller.effective_tenant_id
            or not isinstance(row.get("siteId"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", row["siteId"])
            or (scope.site_ids is not None and row["siteId"] not in scope.site_ids)
        ):
            raise OperatorRefused("CPMS_SCOPE_MISMATCH")
    if tool == TOOL_NAMES[0]:
        if data.get("orderNo") != order or data.get("siteId") != rows[0].get("siteId"):
            raise OperatorRefused("CPMS_SCOPE_MISMATCH")
    elif (
        type(data.get("total")) is not int
        or data["total"] < len(rows)
        or type(data.get("current")) is not int
        or data["current"] != 1
        or type(data.get("size")) is not int
        or data["size"] != 50
    ):
        raise OperatorRefused("CPMS_INVALID_PAGE")


def project_evidence(body, order, tool):
    data = body["data"]

    def pick(row, fields):
        result = {key: row[key] for key in fields if key in row}
        if any(
            isinstance(v, (dict, list)) or (isinstance(v, str) and len(v) > 1000) for v in result.values()
        ):
            raise OperatorRefused("CPMS_INVALID_FIELD")
        return result

    selected = (
        pick(data["chOrderInfoModel"], DETAIL_FIELDS)
        if tool == TOOL_NAMES[0]
        else {
            "records": [pick(row, SAMPLE_FIELDS) for row in data["records"]],
            "total": data["total"],
            "sample_limit": 50,
            "truncated": data["total"] > len(data["records"]),
            "ordering_guaranteed": False,
            "complete_timeline": False,
            "samples_returned": len(data["records"]),
            "absence": "no_samples" if not data["records"] else None,
        }
    )
    return {
        "tool": tool,
        "order_no": order,
        "source": "cpms.orderDetail" if tool == TOOL_NAMES[0] else "cpms.chargingMonitor",
        "observed_at": format_stored_time(now_utc()),
        "data": selected,
    }


def _timeline_time(value) -> str | None:
    """CPMS 时间（``yyyy-MM-dd HH:mm:ss`` 或 ISO）归一成 tsdata 接受的本地时间串。"""
    if not isinstance(value, str) or not _TIMELINE_TIME.fullmatch(value.strip()):
        return None
    return value.strip().replace("T", " ")


def _failure_code(exc: BaseException) -> str:
    """将内部失败收敛为面向用户的稳定错误码。"""
    if isinstance(exc, OperatorRefused):
        code = str(exc)
        if code in NODE_FAILURE_CODES.values() or code == "OPERATOR_HISTORY_LIMIT":
            return code
        if code in {
            "OPERATOR_NODE_REJECTED",
            "OPERATOR_NODE_CHANGED",
            "OPERATOR_TARGET_MISSING",
            "CPMS_UNAVAILABLE",
            "TSDATA_UNAVAILABLE",
            "OPERATOR_SCOPE_UNAVAILABLE",
            "OPERATOR_SCOPE_CHANGED",
        }:
            return "OPERATOR_DEPENDENCY_UNAVAILABLE"
        if code in {"OPERATOR_RESULT_UNSTABLE", "OPERATOR_ANSWER_NOT_GROUNDED"}:
            return "OPERATOR_MODEL_OUTPUT_UNSTABLE"
    return "OPERATOR_DIAGNOSIS_FAILED"


def _failure_reason(exc: BaseException) -> str:
    """日志归因用摘要，不记录凭据与上游原文。"""
    if isinstance(exc, OperatorRefused):
        return str(exc)
    return type(exc).__name__


def _answer_refused(reason, detail=""):
    """拒绝不合格回答并记录脱敏规则命中日志。"""
    logging.getLogger("aiops.operator").warning(
        "operator_answer_rejected reason=%s%s", reason, f" {detail}" if detail else ""
    )
    raise OperatorRefused("OPERATOR_INVALID_ANSWER")


def cpms_get(url, bearer, tenant, *, timeout=10):
    def unavailable(_):
        return OperatorRefused("CPMS_UNAVAILABLE")

    return request_json(
        RequestSpec(
            url=url,
            headers={
                "Authorization": "Bearer " + bearer,
                "tenant-id": tenant,
                "client-type": "admin",
                "Accept": "application/json",
            },
            timeout=timeout,
            max_read_bytes=262144,
            follow_redirects=False,
        ),
        mapping=ErrorMapping(
            auth_rejected=unavailable,
            http_error=unavailable,
            unavailable=unavailable,
            invalid_body=unavailable,
            invalid_envelope=unavailable,
        ),
    )


class TsdataInvalidColumn(OperatorRefused):
    """TDengine 门面因租户表缺少可选列拒绝本次 `fields` 列表，仅允许无 `fields` 单次回退。"""


def _parse_tsdata_envelope(payload, mapping):
    if isinstance(payload, Mapping):
        code = payload.get("code")
        if code not in ENVELOPE_SUCCESS_CODES:
            detail = payload.get("msg") if payload.get("msg") is not None else payload.get("message")
            if "Invalid column name" in str(detail or ""):
                raise TsdataInvalidColumn("TSDATA_UNAVAILABLE")
    return parse_code_data_envelope(payload, mapping)


def tsdata_query(url, payload, *, timeout=10):
    """POST tsdata ``QueryRequest``；一切失败形状均归一为 ``TSDATA_UNAVAILABLE``。"""

    def unavailable(_):
        return OperatorRefused("TSDATA_UNAVAILABLE")

    return request_json(
        RequestSpec(
            url=url,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            body=json_body(payload),
            timeout=timeout,
            max_read_bytes=262144,
            follow_redirects=False,
        ),
        mapping=ErrorMapping(
            auth_rejected=unavailable,
            http_error=unavailable,
            unavailable=unavailable,
            invalid_body=unavailable,
            invalid_envelope=unavailable,
        ),
        envelope=_parse_tsdata_envelope,
    )
