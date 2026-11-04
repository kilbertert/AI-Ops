"""同一账号的站点派生规则路径；禁止模型选择任意模板。"""

import pytest

from aiops_diagnostics.operator.operator_conversation import OperatorRefused
from operations.test_operator_cpms import scope


def responses():
    return {
        "/chSite/SITE-1": {
            "code": 0,
            "data": {
                "id": "SITE-1",
                "tenantId": "TENANT-A",
                "type": 0,
                "name": "合成站",
                "chargingBillingTemplateId": "TARIFF-1",
                "timezone": "Asia/Shanghai",
                "parkFeeTemplateId": None,
                "occupyBillingTemplateId": None,
            },
        },
        "/chFeeTemplate/TARIFF-1": {
            "code": 0,
            "data": {
                "id": "TARIFF-1",
                "siteId": "SITE-1",
                "type": 0,
                "billingMode": 2,
                "vat": 0,
                "startLimit": 0,
                "launchFee": 1,
                "enableLaunchFee": True,
                "electricityLoss": 0,
                "freeChargingTime": 0,
                "enablePackage": False,
                "holidayTemplate": None,
                "feeTemplateItems": [
                    {"startTime": "00:00", "endTime": "23:59", "price": "1", "feePrice": "0.5"}
                ],
            },
        },
    }


def test_rules_follow_visible_site_reference_only():
    from aiops_diagnostics.operator.station_tariff import read_rules

    calls = []
    data = responses()
    result = read_rules(
        scope(), "SITE-1", "2026-11-06", lambda path: calls.append(path) or data[path], lambda: None
    )
    assert calls == ["/chSite/SITE-1", "/chFeeTemplate/TARIFF-1"]
    assert result["template"]["feeTemplateItems"][0]["price"] == "1"


@pytest.mark.parametrize("fault", ["scope", "tenant", "site", "template"])
def test_bad_site_identity_never_leaks_tariff(fault):
    from aiops_diagnostics.operator.station_tariff import read_rules

    data = responses()
    caller = scope()
    calls = []
    if fault == "scope":
        caller = scope(site="OTHER")
    if fault == "tenant":
        data["/chSite/SITE-1"]["data"]["tenantId"] = "OTHER"
    if fault == "site":
        data["/chSite/SITE-1"]["data"]["id"] = "OTHER"
    if fault == "template":
        data["/chFeeTemplate/TARIFF-1"]["data"]["id"] = "OTHER"
    with pytest.raises(OperatorRefused):
        read_rules(
            caller, "SITE-1", "2026-11-06", lambda path: calls.append(path) or data[path], lambda: None
        )
    if fault != "template":
        assert "/chFeeTemplate/TARIFF-1" not in calls


def test_holiday_rule_keeps_parent_packages_and_checks_row_identity():
    from copy import deepcopy

    from aiops_diagnostics.operator.station_tariff import read_rules

    data = responses()
    template = data["/chFeeTemplate/TARIFF-1"]["data"]
    holiday = deepcopy(template)
    holiday["feeTemplateItems"][0]["price"] = "0.5"
    template.update(
        holidayTemplate=holiday, enablePackage=True, feeTemplatePackages=[{"type": 2, "price": 10}]
    )
    path = "/chHoliday/getHolidayBySiteId?year=2026&month=11&siteId=SITE-1"
    data[path] = {"code": 0, "data": [{"tenantId": "TENANT-A", "siteId": "SITE-1", "holiday": "2026-11-06"}]}
    result = read_rules(scope(), "SITE-1", "2026-11-06", data.__getitem__, lambda: None)
    assert result["holiday"] and result["template"]["feeTemplateItems"][0]["price"] == "0.5"
    assert result["template"]["feeTemplatePackages"] == [{"type": 2, "price": 10}]
    data[path]["data"][0]["tenantId"] = "OTHER"
    with pytest.raises(OperatorRefused):
        read_rules(scope(), "SITE-1", "2026-11-06", data.__getitem__, lambda: None)


def test_winter_selection_and_pending_rule_change_are_explicit():
    from copy import deepcopy

    from aiops_diagnostics.operator.station_tariff import read_rules

    data = responses()
    site = data["/chSite/SITE-1"]["data"]
    site.update(summerStart="04-01", summerEnd="09-30", winterChargingBillingTemplateId="WINTER")
    data["/chFeeTemplate/WINTER"] = deepcopy(data["/chFeeTemplate/TARIFF-1"])
    data["/chFeeTemplate/WINTER"]["data"]["id"] = "WINTER"
    data["/chFeeTemplate/WINTER"]["data"]["launchFee"] = 2
    assert (
        read_rules(scope(), "SITE-1", "2026-11-06", data.__getitem__, lambda: None)["template"]["launchFee"]
        == 2
    )
    site["hasPendingFeeTemplateUpdate"] = True
    with pytest.raises(OperatorRefused, match="PENDING"):
        read_rules(scope(), "SITE-1", "2026-11-06", data.__getitem__, lambda: None)
