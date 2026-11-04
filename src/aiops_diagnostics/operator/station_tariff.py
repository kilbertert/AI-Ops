"""当前站点的指定日期费率；只沿已核对站点的模板引用读取。"""

import re
from datetime import date

from aiops_diagnostics.identity.query_scope import resolve_query_scope
from aiops_diagnostics.operator.operator_conversation import OperatorRefused
from aiops_diagnostics.operator.operator_tools import tool
from aiops_diagnostics.operator.order_pricing import project

ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise OperatorRefused("CPMS_INVALID_FIELD")
    return value


def read_rules(caller, site_id, day, read, reauthorize):
    site_id = identifier(site_id)
    try:
        selected_date = date.fromisoformat(day)
    except (ValueError, TypeError) as exc:
        raise OperatorRefused("OPERATOR_PLAN_DATE_REQUIRED") from exc
    scope = resolve_query_scope(caller)
    if scope.site_ids is not None and site_id not in scope.site_ids:
        raise OperatorRefused("CPMS_SCOPE_MISMATCH")

    def get(path):
        reauthorize()
        body = read(path)
        reauthorize()
        if not isinstance(body, dict) or type(body.get("code")) is not int or body["code"] != 0:
            raise OperatorRefused("CPMS_INVALID_RESPONSE")
        return body.get("data")

    site = get("/chSite/" + site_id)
    if (
        not isinstance(site, dict)
        or site.get("id") != site_id
        or site.get("tenantId") != caller.effective_tenant_id
    ):
        raise OperatorRefused("CPMS_SCOPE_MISMATCH")
    if site.get("hasPendingFeeTemplateUpdate"):
        raise OperatorRefused("OPERATOR_PLAN_PENDING_RULE_CHANGE")
    if type(site.get("type")) is not int or site["type"] != 0:
        raise OperatorRefused("OPERATOR_PLAN_SITE_UNSUPPORTED")
    template_id = site.get("chargingBillingTemplateId")
    winter = site.get("winterChargingBillingTemplateId")
    if winter:
        try:
            start = date.fromisoformat(str(selected_date.year) + "-" + site["summerStart"])
            end = date.fromisoformat(str(selected_date.year) + "-" + site["summerEnd"])
            if start > end:
                raise ValueError("cross-year season")
        except (ValueError, TypeError, KeyError) as exc:
            raise OperatorRefused("OPERATOR_PLAN_SEASON_UNSUPPORTED") from exc
        if not start <= selected_date <= end:
            template_id = winter
    template_id = identifier(template_id)
    template = get("/chFeeTemplate/" + template_id)
    if (
        not isinstance(template, dict)
        or template.get("id") != template_id
        or template.get("siteId") not in (site_id, "-1")
    ):
        raise OperatorRefused("CPMS_SCOPE_MISMATCH")
    if template.get("tenantId") not in (None, caller.effective_tenant_id):
        raise OperatorRefused("CPMS_SCOPE_MISMATCH")
    holiday = False
    if template.get("holidayTemplate"):
        rows = get(
            f"/chHoliday/getHolidayBySiteId?year={selected_date.year}&month={selected_date.month}&siteId={site_id}"
        )
        if not isinstance(rows, list) or len(rows) > 31:
            raise OperatorRefused("CPMS_INVALID_RESPONSE")
        holidays = set()
        for row in rows:
            if (
                not isinstance(row, dict)
                or row.get("siteId") != site_id
                or row.get("tenantId") != caller.effective_tenant_id
            ):
                raise OperatorRefused("CPMS_SCOPE_MISMATCH")
            try:
                value = date.fromisoformat(row.get("holiday", ""))
            except (TypeError, ValueError) as exc:
                raise OperatorRefused("CPMS_INVALID_RESPONSE") from exc
            if (value.year, value.month) != (selected_date.year, selected_date.month) or value in holidays:
                raise OperatorRefused("CPMS_INVALID_RESPONSE")
            holidays.add(value)
            if value == selected_date:
                holiday = True
        if holiday:
            template = {
                **template["holidayTemplate"],
                "enablePackage": template.get("enablePackage"),
                "feeTemplatePackages": template.get("feeTemplatePackages"),
            }
    template_shape = tool("cpms_station_plan").evidence_shape["rules"]["template"]
    return {
        "site_id": site_id,
        "date": day,
        "holiday": holiday,
        "timezone": site.get("timezone"),
        "name": site.get("name"),
        "extras": {
            k: (bool(site[k]) if k in site else None)
            for k in (
                "parkFeeTemplateId",
                "occupyBillingTemplateId",
                "appointmentTemplateId",
                "insuranceTemplateId",
            )
        },
        "template": project(template, template_shape),
    }
