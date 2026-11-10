"""管家离线工具接线：复用受控读面，历史引用不代替当前授权。"""

import re

from aiops_diagnostics.common.scalar_rules import now_utc
from aiops_diagnostics.data import offline_source
from aiops_diagnostics.data.sources import scoped_mysql_source
from aiops_diagnostics.identity.query_scope import resolve_query_scope
from aiops_diagnostics.operator.operator_conversation import OperatorRefused

NAME = "cpms_device_offline"


def prior_data(owner, task):
    rows = []
    for run_id in task.history_runs:
        run = owner.context.agent_run_store.peek(run_id)
        if run and run.snapshot:
            data = run.snapshot.get("evidence", {}).get(NAME, {}).get("data", {})
            if data:
                rows.append(data)
    current = task.evidence.get(NAME, {}).get("data", {}) if NAME in task.delivered else {}
    return [*rows, *([current] if current else [])]


def mentioned(value, question):
    return bool(re.search(r"(?<![A-Za-z0-9_.:-])" + re.escape(value) + r"(?![A-Za-z0-9_.:-])", question))


def selectors(data):
    devices, sites, orders = set(), set(), set()
    for result in data:
        for group in ("top_devices", "open_incident_list"):
            for row in result.get(group, {}).get("items", []):
                devices.update(str(row[k]) for k in ("device_id", "device_code") if row.get(k))
                sites.add(row["site_id"])
        for row in result.get("order_overlap", {}).get("items", []):
            orders.add(row["order_no"])
    return devices, sites, orders


def arguments(owner, task, run, values):
    if not isinstance(values, dict) or set(values) - {"start", "end", "site_id", "device"}:
        raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
    if any(not isinstance(v, str) or len(v) > 80 for v in values.values()):
        raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
    devices, sites, _ = selectors(prior_data(owner, task))
    for item in task.evidence.get("cpms_site_search", {}).get("data", {}).get("records", []):
        if "cpms_site_search" in task.delivered:
            sites.add(item["id"])
    detail = task.evidence.get("cpms_order_detail", {}).get("data", {})
    if "cpms_order_detail" in task.delivered:
        devices.update(str(detail[k]) for k in ("deviceId", "deviceCode") if detail.get(k))
        if detail.get("siteId"):
            sites.add(detail["siteId"])
    for key, known in (("device", devices), ("site_id", sites)):
        value = values.get(key, "")
        if value and (
            not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value)
            or (value not in known and not mentioned(value, task.question))
        ):
            raise OperatorRefused("OPERATOR_TOOL_ARGUMENTS_INVALID")
    return {k: v for k, v in values.items() if v}


def query(owner, task, run):
    values = task.arguments.get(NAME, {})
    scope = resolve_query_scope(owner.authorize_execution(run.run_id, tool=NAME)[0].caller)
    with scoped_mysql_source(owner.context.runtime.diagnostic_settings, scope) as source:
        sites = (values["site_id"],) if values.get("site_id") else None
        device_id = ""
        if values.get("device"):
            device_id = offline_source.resolve_device(source, values["device"], sites)
        _, data = offline_source.read(
            source, sites, values.get("start"), values.get("end"), now_utc(), device_id=device_id
        )
        refs = data.pop("references")
        offline_source.require_references(source, refs)
    owner.authorize_execution(run.run_id, tool=NAME)
    return data, refs


def require_current(owner, task, refs):
    with scoped_mysql_source(
        owner.context.runtime.diagnostic_settings, resolve_query_scope(task.authorize())
    ) as source:
        offline_source.require_references(source, refs)
