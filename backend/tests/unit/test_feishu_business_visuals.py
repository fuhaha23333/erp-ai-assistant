"""Charts preserve monetary totals, independent units and unknown-value semantics."""

from decimal import Decimal

import pytest

from app.integrations.feishu_business_visuals import charts, compact
from tests.unit.test_feishu_business_display import response


def specs(result):
    return [
        v[-1]["chart_spec"] for v in charts(result.plan.steps[0], result.results[0], "CNY") if v
    ]


def test_growth_top_five_plus_others_reconcile_to_full_totals(risk_report):
    result = response(risk_report, "growth", growth_basis="previous")
    item = result.results[0]
    item.rows[:] = [
        dict(item.rows[0], name=f"合成{i}", delta=str(i * 10000)) for i in range(8, 0, -1)
    ]
    item.totals["groups"][0].update(positive="360000", negative="25000")
    spec = specs(result)[0]
    points = spec["data"]["values"]
    assert len(points) == 7  # five increases + remaining increases + unlisted decreases
    assert sum(Decimal(p["exact"]) for p in points) == Decimal("335000")
    assert next(p for p in points if p["label"] == "其余增长")["exact"] == "60000"
    assert spec["axes"][0]["title"]["text"] == "万元"
    assert points[0]["value"] == 8


def test_quantity_charts_separate_units_and_comparison_bases(risk_report):
    result = response(risk_report, "growth", metric="quantity")
    item = result.results[0]
    item.rows.extend(dict(r, unit="瓶", delta="7") for r in list(item.rows))
    item.totals["groups"].extend(
        dict(g, unit="瓶", positive="7", negative="0") for g in list(item.totals["groups"])
    )
    views = specs(result)
    assert len(views) == 4
    assert [v["axes"][0]["title"]["text"] for v in views] == ["盒", "盒", "瓶", "瓶"]
    assert all("CNY" not in str(v) for v in views)


def test_price_charts_compare_one_variant_and_never_turn_unknown_cost_into_zero(risk_report):
    result = response(risk_report, "price", growth_basis="previous")
    row = result.results[0].rows[0]
    row["current_unit_cost"] = None
    row["current_spread"] = None
    points = specs(result)[0]["data"]["values"]
    assert len(points) == 2
    assert {p["label"] for p in points} == {"平均售价"}
    assert {p["series"] for p in points} == {"本期", "上期"}


def test_margin_bridge_matches_product_profit_change_and_missing_bridge_is_omitted(risk_report):
    result = response(risk_report, "margin", growth_basis="previous")
    points = specs(result)[0]["data"]["values"]
    assert sum(Decimal(p["exact"]) for p in points) == Decimal(result.results[0].rows[0]["delta"])
    result.results[0].rows[0]["volume_effect"] = None
    assert specs(result) == []


def test_inventory_chart_counts_overlap_and_stocking_is_reference_not_purchase(risk_report):
    result = response(risk_report, "inventory_risk")
    spec = specs(result)[0]
    assert spec["type"] == "bar" and len(spec["data"]["values"]) == 6
    assert spec["axes"][0]["title"]["text"] == "组"
    stocking = specs(response(risk_report, "stocking"))[0]
    assert {p["label"] for p in stocking["data"]["values"]} == {
        "同期参考需求",
        "近期速度对照",
        "效期合格库存",
    }


@pytest.mark.parametrize("number", ["9007199254740993.1234", "NaN", "Infinity"])
def test_unsafe_coordinates_never_reach_chart_json(risk_report, number):
    result = response(risk_report, "margin", growth_basis="previous")
    result.results[0].rows[0]["volume_effect"] = number
    assert specs(result) == []


def test_compact_kpi_uses_decimal_and_null_is_not_zero():
    assert compact("123456.7891", currency="CNY") == "¥ 12.35万"
    assert compact(None) == "—（无有效值）"
