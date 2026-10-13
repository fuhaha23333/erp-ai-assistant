"""Web business facts must survive Feishu presentation and capacity reduction."""

import json
from copy import deepcopy

import pytest

from app.analysis.analytics import execute_analysis
from app.integrations.feishu_analytics_cards import _card, render_analysis
from app.integrations.feishu_display_components import MAX_CARD_BYTES, MAX_ELEMENTS, element_count
from app.schemas.analytics import AnalysisPlan
from tests.unit.test_feishu_cards import walk


def response(report, kind, **patch):
    step = dict(
        domain="shipping",
        kind=kind,
        metric="amount",
        start_date="2026-08-01",
        end_date_exclusive="2026-09-01",
    )
    if kind == "price":
        step["metric"] = "unit_price"
    elif kind == "margin":
        step["metric"] = "gross_profit"
    elif kind in {"inventory_risk", "stocking"}:
        step.update(
            domain="inventory",
            metric="stock",
            start_date="2026-09-16",
            end_date_exclusive="2026-09-17",
        )
        if kind == "stocking":
            step.update(start_date="2026-09-17", end_date_exclusive="2026-09-24")
    step.update(patch)
    return execute_analysis(report, AnalysisPlan(steps=[step]))


def visible(card):
    nodes = [node for node in card["body"]["elements"] if node.get("tag") != "collapsible_panel"]
    return " ".join(node["content"] for node in walk(nodes) if node.get("tag") == "plain_text")


def full_text(card):
    return json.dumps(card, ensure_ascii=False)


@pytest.mark.parametrize("kind", ["growth", "price", "margin", "inventory_risk", "stocking"])
def test_all_topics_render_full_details_without_mutating_business_facts(risk_report, kind):
    result = response(risk_report, kind)
    before = deepcopy(result.model_dump())
    card, fallback = render_analysis(result, conversation_notice="本轮会话已结束")
    raw = json.dumps(card, ensure_ascii=False)
    assert result.model_dump() == before
    assert "逐项详细数据" in raw and "合成规格" in raw and "合成厂家" in raw
    assert "本轮会话已结束" in raw and "本轮会话已结束" in fallback
    assert "30分钟内可" not in raw
    assert "单位：盒" in raw and "单位：盒" in fallback
    assert len(raw.encode()) <= MAX_CARD_BYTES and element_count(card) <= MAX_ELEMENTS
    assert not any(node.get("tag") == "table" for node in walk(card))
    assert 1 <= sum(node.get("tag") == "chart" for node in walk(card)) <= 2


def test_growth_previous_amount_and_current_amount_are_visible_without_opening_details(risk_report):
    card, fallback = render_analysis(response(risk_report, "growth", growth_basis="previous"))
    top = visible(card)
    assert "本期金额 ¥ 160.00" in top and "上期金额 ¥ 100.00" in top
    assert "净变化 ¥ 60.00" in top
    assert "2026-07-01 至 2026-07-31" in top
    assert "本期金额：160.0000" in full_text(card)
    assert "比较期金额：100.0000" in full_text(card)
    assert "变化率：60.00%" in full_text(card)
    assert "本期日均（当前指标）：5.1613" in fallback
    assert "贡献占比：100.00%" in fallback


def test_growth_quantity_keeps_units_and_both_bases_with_limited_rows(risk_report):
    # Synthetic second unit isolates presentation from execution coverage checks.
    result = response(risk_report, "growth", metric="quantity")
    item = result.results[0]
    item.rows.extend(dict(row, unit="瓶", delta="7.0000") for row in list(item.rows))
    item.totals["groups"].extend(
        dict(group, unit="瓶", current_quantity="7.0000", previous_quantity="0.0000")
        for group in list(item.totals["groups"])
    )
    card = _card(result, 4, False)
    assert "160.00盒" in visible(card) and "7.00瓶" in visible(card)
    from app.integrations.feishu_business_display import details

    _, rows = details(result.plan.steps[0], item, "CNY", 4)
    assert {r["comparison"] for r in rows} == {"环比 · 上期", "同比 · 去年同期"}
    assert all("¥" not in r["delta"] for r in rows)
    assert all("current_quantity" in r and "current_amount" not in r for r in rows)


def test_price_cost_and_spread_are_retained_without_a_fake_global_price(risk_report):
    card, fallback = render_analysis(response(risk_report, "price"))
    top = visible(card)
    assert "持平 1" in top and "不可比 0" in top
    assert "本期均价" not in top
    raw = json.dumps(card, ensure_ascii=False)
    for content in (
        "比较期单位采购成本：0.5000",
        "本期单位采购成本：0.9000",
        "比较期单位毛利空间：0.5000",
        "本期单位毛利空间：0.1000",
        "单位毛利空间变化：-0.4000",
        "两期均有出库客户数：2",
    ):
        assert content in raw and content in fallback


def test_margin_displays_both_periods_rates_and_reconciling_bridge(risk_report):
    card, fallback = render_analysis(response(risk_report, "margin", growth_basis="previous"))
    top = full_text(card)
    assert "本期收入 ¥ 160.00" in top and "上期收入 ¥ 100.00" in top
    assert "本期采购成本 ¥ 144.00" in top and "上期采购成本 ¥ 50.00" in top
    assert "本期毛利 ¥ 16.00" in top and "上期毛利 ¥ 50.00" in top
    assert "本期毛利率 10.00%" in top and "上期毛利率 50.00%" in top
    raw = json.dumps(card, ensure_ascii=False)
    for content in (
        "销量影响：30.0000",
        "售价影响：0.0000",
        "成本影响：-64.0000",
        "舍入调整：0.0000",
        "毛利变化：-34.0000",
        "销量增加但毛利未增：是",
    ):
        assert content in raw and content in fallback


def test_inventory_risk_exposes_expiry_velocity_and_unknown_cost(risk_report):
    result = response(risk_report, "inventory_risk")
    item = result.results[0]
    item.totals.update(
        occupied_amount=None, known_occupied_amount="60.0000", unknown_cost_batches=1
    )
    item.rows[0].update(occupied_amount=None, estimated_clear_days=None)
    card, fallback = render_analysis(result)
    top = full_text(card)
    assert "采购成本占用 —（无有效值，非零）" in top
    assert "已知成本占用 ¥ 60.00" in top and "成本缺失 1 组" in top
    assert "库龄≥90天" in top and "剩余效期≤180天" in top
    raw = json.dumps(card, ensure_ascii=False)
    for content in (
        "到期日：2026-09-15",
        "日均出库数量：1.0000",
        "先到期先售出累计预计售完天数：—（无有效值，非零）",
        "关注项：已过期",
    ):
        assert content in raw and content in fallback


def test_stocking_uses_snapshot_date_and_explains_unavailable_scenario_inputs(risk_report):
    result = response(risk_report, "stocking")
    assert "as_of" not in result.results[0].totals
    card, fallback = render_analysis(result)
    top = full_text(card)
    assert "目标期间：2026-09-17 至 2026-09-23" in top
    assert "库存时点 2026-09-16" in top
    assert "去年同期：2025-09-17 至 2025-09-23" in top
    assert "网页按具体规格填写" in top
    raw = json.dumps(card, ensure_ascii=False)
    for content in (
        "去年同期日均折算参考需求：60.0000",
        "近期速度折算（仅对照）：7.0000",
        "效期不足库存：15.0000",
        "效期未知库存：0.0000",
    ):
        assert content in raw and content in fallback
    assert "estimated_gap" not in raw and "最晚下单日：" not in raw


def test_unavailable_rates_and_large_exact_amounts_are_not_zero(risk_report):
    result = response(risk_report, "margin")
    row = result.results[0].rows[0]
    row.update(
        current_profit="9007199254740993.1234",
        previous_profit="-1.0000",
        change_percent=None,
        volume_effect=None,
    )
    card, fallback = render_analysis(result)
    assert "本期毛利：9007199254740993.1234" in full_text(card)
    for payload in (json.dumps(card, ensure_ascii=False), fallback):
        assert "比较期毛利非正，不计算增长率" in payload
        assert "销量影响：—（无有效值，非零）" in payload


def test_empty_rankings_still_show_full_scope_overview(risk_report):
    result = response(risk_report, "growth")
    result.results[0].rows.clear()
    card, fallback = render_analysis(result)
    assert "本期金额 ¥ 160.00" in visible(card)
    assert "当前方向和排序条件下没有明细" in fallback
    assert not any(node.get("tag") == "table" for node in walk(card))


def test_mixed_six_topic_card_obeys_limits_and_keeps_summary_after_truncation(risk_report):
    responses = [
        response(risk_report, kind)
        for kind in ("growth", "price", "margin", "inventory_risk", "stocking", "growth")
    ]
    for result in responses:
        item = result.results[0]
        source = list(item.rows)
        item.rows[:] = [
            dict(source[i % len(source)], name="合成超长品种名称" * 30) for i in range(60)
        ]
    merged = responses[0].model_copy(
        update={
            "plan": AnalysisPlan(steps=[r.plan.steps[0] for r in responses]),
            "results": [r.results[0] for r in responses],
        }
    )
    card, fallback = render_analysis(merged)
    nodes = list(walk(card))
    assert len(json.dumps(card, ensure_ascii=False).encode()) <= MAX_CARD_BYTES
    assert element_count(card) <= MAX_ELEMENTS
    tables = [n for n in nodes if n.get("tag") == "table"]
    assert len(tables) <= 5 and all(t in card["body"]["elements"] for t in tables)
    assert "本期金额 ¥ 160.00" in visible(card)
    assert "卡片节选" in fallback and "概览不受节选影响" in fallback


def test_summary_comes_from_all_customers_not_returned_top_one(risk_report):
    result = response(
        risk_report,
        "growth",
        dimension="buyer",
        top_n=1,
        filters=[dict(field="product", operator="include", code="P")],
    )
    card, _ = render_analysis(result)
    assert "本期金额 ¥ 160.00" in visible(card)
    assert "本期金额：100.0000" in full_text(card)
    assert "榜外增长" in full_text(card)


def test_capacity_fallback_retains_core_values_and_discloses_omitted_details(
    risk_report, monkeypatch
):
    from app.integrations import feishu_analytics_cards as cards

    result = response(risk_report, "growth")
    limit = element_count(_card(result, 1, False, expanded=False))
    monkeypatch.setattr(cards, "MAX_ELEMENTS", limit)
    card, fallback = render_analysis(result)
    assert element_count(card) <= limit
    assert "本期金额 ¥ 160.00" in visible(card)
    assert "本期金额：160.0000" in full_text(card)
    assert "超过卡片容量" in visible(card) and "超过卡片容量" in fallback
    assert "逐项详细数据（与上表相同条目）" not in json.dumps(card, ensure_ascii=False)
