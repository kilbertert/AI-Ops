"""产品金额样例和反例；真实Decimal计算，不以模型输出充当验算。"""

import json
from copy import deepcopy
from decimal import Decimal

import pytest

from aiops_diagnostics.operator import order_billing
from aiops_diagnostics.operator.operator_conversation import OperatorRefused
from operations.test_operator_cpms import scope


def example():
    detail = dict(
        type=0,
        launchType="app",
        status=1,
        packageType=None,
        electricity="1.2",
        electricityFee="2.4",
        serviceFee="2.4",
        launchFee="3",
        dsElectricFee="0",
        dsServiceFee="0",
        parkFee="0",
        totalAmount="7.8",
        marketAmount="0.68",
        vat="20",
        vatFee="1.42",
        payAmount="8.54",
    )
    snapshot = {"feeTemplate": {"name": "合成模板", "vat": "20", "enableLaunchFee": True, "launchFee": "3"}}
    billing = {
        **detail,
        "discounts": {"serviceDiscount": "8", "couponPrice": "0.2", "carRightPrice": "0.48"},
        "reduceBalance": "0",
        "insuranceAmount": "0",
    }
    return snapshot, detail, billing


def rules(result):
    return {row["label"]: row for row in result["checks"]}


def test_product_example_calculates_all_four_rules_without_inventing_recorded_discount():
    snapshot, detail, billing = example()
    result = order_billing.verify(snapshot, detail, billing)
    rows = rules(result)
    assert rows["规则1 服务费折扣优惠"]["calculated"] == Decimal("0.48")
    assert rows["规则1 服务费折扣优惠"]["recorded"] is None
    assert rows["规则1 服务费折扣优惠"]["status"] == "待验证"
    for label, expected in [
        ("规则2 充电费用", "7.80"),
        ("规则3 增值税费用", "1.42"),
        ("规则4 实付款", "8.54"),
    ]:
        assert rows[label]["calculated"] == Decimal(expected)
        assert rows[label]["status"] == "通过"
    assert rows["独立串联复算"]["calculated"] == Decimal("8.54")
    assert "整单计费仍待验证" in result["conclusion"]
    text = order_billing.report(snapshot, detail, billing, "zh")
    assert "2.40 + 2.40 + 3.00 = 7.80" in text
    assert "(7.80 - 0.68) × 20% = 1.42" in text
    assert "后台显示" in text and "实际计算值" in text and "差额" in text


def test_wrong_intermediate_values_cannot_launder_final_amount():
    s, d, b = example()
    d.update(totalAmount="8.8", vatFee="1.62", payAmount="9.74")
    b.update({k: d[k] for k in ("totalAmount", "vatFee", "payAmount")})
    r = rules(order_billing.verify(s, d, b))
    assert r["规则2 充电费用"]["status"] == "不一致"
    assert r["规则3 增值税费用"]["status"] == "通过"
    assert r["规则4 实付款"]["status"] == "通过"
    assert r["独立串联复算"]["status"] == "不一致"
    assert r["独立串联复算"]["difference"] == Decimal("1.20")


@pytest.mark.parametrize(
    "field,value", [("launchType", "operator"), ("type", 1), ("status", 0), ("packageType", 1)]
)
def test_inapplicable_orders_do_not_report_unqualified_rule_pass(field, value):
    s, d, b = example()
    d[field] = value
    r = order_billing.verify(s, d, b)
    assert r["limitations"]
    assert all(c["status"] != "通过" for c in r["checks"] if c["label"].startswith("规则"))


@pytest.mark.parametrize("value", [None, True, "bad", "NaN", "Infinity", "-1"])
def test_missing_or_invalid_money_is_not_zero(value):
    s, d, b = example()
    d["launchFee"] = value
    b["launchFee"] = value
    r = rules(order_billing.verify(s, d, b))
    assert r["规则2 充电费用"]["calculated"] is None
    assert r["规则2 充电费用"]["status"] == "待验证"


def test_missing_template_does_not_use_order_rate_as_replacement():
    _, d, b = example()
    r = rules(order_billing.verify({"missing": True}, d, b))
    assert r["规则3 增值税费用"]["calculated"] is None
    assert all(c["status"] != "通过" for c in r.values())


def test_different_template_rate_cannot_pass_even_if_paid_matches_order_rate():
    s, d, b = example()
    s["feeTemplate"]["vat"] = "10"
    r = rules(order_billing.verify(s, d, b))
    assert r["模板税率"]["status"] == "不一致"
    assert r["规则3 增值税费用"]["calculated"] == Decimal("0.71")
    assert r["规则3 增值税费用"]["status"] == "不一致"


def test_half_up_rounding_uses_rounded_tax_and_rounds_discounted_price_first():
    s, d, b = example()
    d.update(
        serviceFee="0.05",
        dsServiceFee="0",
        totalAmount="0.75",
        marketAmount="0",
        vat="2",
        vatFee="0.02",
        payAmount="0.77",
    )
    s["feeTemplate"]["vat"] = "2"
    b.update(d)
    b["discounts"]["serviceDiscount"] = "5"
    r = rules(order_billing.verify(s, d, b))
    assert r["规则1 服务费折扣优惠"]["calculated"] == Decimal("0.02")
    assert r["规则3 增值税费用"]["calculated"] == Decimal("0.02")
    assert r["规则4 实付款"]["calculated"] == Decimal("0.77")


def test_nonzero_loss_and_parking_are_not_dropped_from_subtotal():
    s, d, b = example()
    d.update(dsElectricFee="0.1", dsServiceFee="0.2", parkFee="1")
    b.update(d)
    r = rules(order_billing.verify(s, d, b))
    assert r["规则2 充电费用"]["calculated"] == Decimal("9.10")
    assert r["规则2 充电费用"]["status"] == "不一致"


def test_billing_projection_checks_identity_and_strips_private_data():
    _, d, _ = example()
    row = {
        **d,
        "orderNo": "O1",
        "tenantId": "TENANT-A",
        "siteId": "SITE-1",
        "phone": "PRIVATE",
        "discountDetails": json.dumps(
            {"serviceDiscount": 8, "couponPrice": 0.2, "carRightPrice": 0.48, "account": "PRIVATE"}
        ),
    }
    result = order_billing.read_billing({"code": 0, "data": row}, scope(), "O1", "SITE-1")
    assert result["discounts"]["serviceDiscount"] == 8
    assert "PRIVATE" not in str(result)
    for key in ("orderNo", "tenantId", "siteId"):
        wrong = deepcopy(row)
        wrong[key] = "OTHER"
        with pytest.raises(OperatorRefused, match="CPMS_SCOPE_MISMATCH"):
            order_billing.read_billing({"code": 0, "data": wrong}, scope(), "O1", "SITE-1")


@pytest.mark.parametrize("raw", ["[]", '{"serviceDiscount":8,"serviceDiscount":5}', "x" * 4097])
def test_unreliable_discount_json_is_rejected(raw):
    row = {"orderNo": "O1", "tenantId": "TENANT-A", "siteId": "SITE-1", "discountDetails": raw}
    with pytest.raises(OperatorRefused, match="CPMS_INVALID_FIELD"):
        order_billing.read_billing({"code": 0, "data": row}, scope(), "O1", "SITE-1")


def test_non_chinese_report_does_not_leak_chinese_labels():
    s, d, b = example()
    text = order_billing.report(s, d, b, "en")
    assert "Rule 3: tax" in text and "1.42" in text
    assert not any("\u4e00" <= c <= "\u9fff" for c in text)


def test_billing_requires_prior_tariff_before_get(tmp_path, monkeypatch):
    from aiops_diagnostics.operator import operator_cpms
    from operations.test_operator_cpms import CONTRACT, NODE_A_TOKEN, environment, request, terminal

    observed = []
    with environment(tmp_path, monkeypatch) as f:

        def dispatch(_, config, run_id):
            head = {"Authorization": "Bearer " + NODE_A_TOKEN, **CONTRACT}
            config = f["client"].post("/v1/internal/charging-status/execution-config/" + run_id, headers=head)
            head.update(
                {
                    "X-AI-Ops-Exec-Config": config.json()["sha256"],
                    "X-AI-Ops-Tool-Contract": operator_cpms.OBJECT_CONTRACT,
                }
            )
            f["ctx"].operator_cpms.tasks[run_id].delivered.add("cpms_order_detail")
            grant = (
                f["client"]
                .post(
                    f"/v1/internal/operator-runs/{run_id}/tools/cpms_order_billing/authorization",
                    headers=head,
                    json={},
                )
                .json()
            )
            result = f["client"].get(
                grant["base_url"] + grant["request"]["path"],
                headers={"Authorization": "Bearer " + grant["bearer"]},
            )
            observed.append(result.status_code)
            assert "OPERATOR_TARIFF_REQUIRED" in result.text

        monkeypatch.setattr(operator_cpms.SnailDispatcher, "dispatch", dispatch)
        qa = request(f["client"]).json()["qa_id"]
        terminal(f, qa)
        assert observed and all(code == 403 for code in observed)
        assert not any(path.startswith("/chOrderInfo/ORDER") for path, _ in f["state"]["calls"])
