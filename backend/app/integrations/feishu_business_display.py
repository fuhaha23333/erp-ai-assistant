"""Business-topic presentation over the same facts used by the web workspace."""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from app.analysis.sales_query import _label
from app.integrations.feishu_display_components import amount

KINDS = {"growth", "price", "margin", "inventory_risk", "stocking"}
BASES = {"previous": "环比 · 上期", "year_over_year": "同比 · 去年同期"}


def value(raw, *, money=False, currency="CNY"):
    if raw is None:
        return "—（无有效值，非零）"
    if money:
        return amount(raw, currency)
    return _label(str(raw), 300)


def interval(period):
    return f"{period['start']} 至 {date.fromisoformat(period['end_exclusive']) - timedelta(days=1)}"


def overview(step, result, currency):
    """Never reconstruct an overview by summing the truncated ranking rows."""
    totals = result.totals
    lines = []
    periods = totals.get("periods", {})
    if periods:
        lines.append("本期：" + interval(periods["current"]))
    for group in totals.get("groups", []):
        basis = group["basis"]
        previous_label = "上期" if basis == "previous" else "去年同期"
        lines.append(BASES[basis] + "：" + interval(periods[basis]))
        if result.kind == "growth":
            quantity = step.metric == "quantity"
            metric = "quantity" if quantity else "amount"
            label = "数量" if quantity else "金额"
            unit = f" {group['unit']}" if quantity else ""

            def fmt(key, group=group, quantity=quantity, unit=unit):
                return value(group.get(key), money=not quantity, currency=currency) + unit

            lines += [
                f"本期{label} {fmt('current_' + metric)} ｜ "
                f"{previous_label}{label} {fmt('previous_' + metric)}",
                f"净变化 {fmt('delta')} ｜ 覆盖 {group['groups']} 组",
                f"增长贡献 {fmt('positive')} ｜ 下降贡献（绝对值） {fmt('negative')}",
                f"榜外增长 {fmt('increase_other')} ｜ 榜外下降 {fmt('decrease_other')}"
                f" ｜ 持平 {group.get('unchanged', 0)} 组",
            ]
        elif result.kind == "price":
            lines.append(
                f"全部 {group['total']} 组：下降 {group['decrease']}、上涨 {group['increase']}、"
                f"持平 {group['unchanged']}、不可比 {group['unavailable']}。"
                "均价按品种和单位分别比较。"
            )
        elif result.kind == "margin":
            for field, label in (("amount", "收入"), ("cost", "采购成本"), ("profit", "毛利")):
                lines.append(
                    f"本期{label} {amount(group['current_' + field], currency)} ｜ "
                    f"{previous_label}{label} {amount(group['previous_' + field], currency)}"
                )
            rates = [
                f"{Decimal(group[key]) * 100:.2f}%"
                if group.get(key) is not None
                else "—（收入为零）"
                for key in ("current_margin", "previous_margin")
            ]
            lines += [
                f"毛利变化 {amount(group['delta'], currency)}",
                f"本期毛利率 {rates[0]} ｜ {previous_label}毛利率 {rates[1]}",
                f"全部 {group['total']} 组；销量增加但毛利未增 {group['attention']} 组。",
            ]
    if result.kind == "inventory_risk":
        lines = [
            f"库存时点 {totals['as_of']}（备份余额，非实时库存）",
            "出库观察期："
            + interval(
                dict(start=totals["lookback_start"], end_exclusive=totals["lookback_end_exclusive"])
            ),
            f"全部正库存批次分组 {totals['batch_count']}；"
            f"采购成本占用 {value(totals['occupied_amount'], money=True, currency=currency)}",
            f"已知成本占用 {amount(totals['known_occupied_amount'], currency)}；"
            f"成本缺失 {totals['unknown_cost_batches']} 组",
            "库存数量（分单位）："
            + "；".join(f"{quantity} {unit}" for unit, quantity in totals["quantities"].items()),
            f"库龄≥{totals['age_threshold_days']}天 {totals['aged']}组；"
            f"剩余效期≤{totals['expiry_threshold_days']}天 {totals['near_expiry']}组；"
            f"已过期 {totals['expired']}组",
            f"无近期出库 {totals['no_recent_sales']}组；"
            f"预计售完晚于到期 {totals['expiry_mismatch']}组；资料待核实 {totals['data_issue']}组。"
            "关注项可重叠。",
        ]
    elif result.kind == "stocking":
        lines = [
            "目标期间："
            + interval(
                dict(start=totals["target_start"], end_exclusive=totals["target_end_exclusive"])
            ),
            f"库存时点 {totals['snapshot_date']}（备份余额，非实时库存）",
            "去年同期："
            + interval(
                dict(start=totals["previous_start"], end_exclusive=totals["previous_end_exclusive"])
            ),
            "近期出库："
            + interval(
                dict(start=totals["recent_start"], end_exclusive=totals["recent_end_exclusive"])
            ),
            f"全部 {totals['product_count']} 个品种规格组合，数量按单位分别查看。",
            "以下为备货依据；在途、到货日、供货周期及目标需求需在网页按具体规格填写，"
            "再计算缺口与最晚下单日。参考需求不是采购量。",
        ]
    return "\n".join(lines)


def selected_rows(result, row_limit):
    if len(result.rows) <= row_limit or result.kind not in {"growth", "price", "margin"}:
        return result.rows[:row_limit]
    # Keep both comparison bases/directions/units represented when space permits.
    buckets = defaultdict(list)
    for row in result.rows:
        buckets[(row.get("basis"), row.get("unit"), row.get("direction"))].append(row)
    selected = []
    for index in range(max(map(len, buckets.values()), default=0)):
        for rows in buckets.values():
            if index < len(rows):
                selected.append(rows[index])
                if len(selected) == row_limit:
                    return selected
    return selected


FIELDS = {
    "growth": (
        ("current_amount", "本期金额"),
        ("previous_amount", "比较期金额"),
        ("current_quantity", "本期数量"),
        ("previous_quantity", "比较期数量"),
        ("delta", "变化额（当前指标）"),
        ("change_percent", "变化率"),
        ("daily_current", "本期日均（当前指标）"),
        ("daily_previous", "比较期日均（当前指标）"),
        ("contribution_percent", "贡献占比"),
        ("contribution_basis", "贡献分母"),
        ("record_state", "记录情况"),
        ("comparability", "可比性说明"),
    ),
    "price": (
        ("previous_price", "比较期均价"),
        ("current_price", "本期均价"),
        ("delta", "每单位价差"),
        ("change_percent", "变化率"),
        ("previous_quantity", "比较期数量"),
        ("current_quantity", "本期数量"),
        ("previous_amount", "比较期金额"),
        ("current_amount", "本期金额"),
        ("previous_customers", "比较期客户数"),
        ("current_customers", "本期客户数"),
        ("common_customers", "两期均有出库客户数"),
        ("previous_unit_cost", "比较期单位采购成本"),
        ("current_unit_cost", "本期单位采购成本"),
        ("previous_spread", "比较期单位毛利空间"),
        ("current_spread", "本期单位毛利空间"),
        ("spread_delta", "单位毛利空间变化"),
        ("record_state", "可比说明"),
        ("cost_status", "成本口径"),
    ),
    "margin": (
        ("previous_quantity", "比较期数量"),
        ("current_quantity", "本期数量"),
        ("previous_amount", "比较期收入"),
        ("current_amount", "本期收入"),
        ("previous_cost", "比较期采购成本"),
        ("current_cost", "本期采购成本"),
        ("previous_profit", "比较期毛利"),
        ("current_profit", "本期毛利"),
        ("delta", "毛利变化"),
        ("change_percent", "毛利变化率"),
        ("previous_margin_percent", "比较期毛利率"),
        ("current_margin_percent", "本期毛利率"),
        ("previous_price", "比较期均价"),
        ("current_price", "本期均价"),
        ("previous_unit_cost", "比较期单位采购成本"),
        ("current_unit_cost", "本期单位采购成本"),
        ("volume_effect", "销量影响"),
        ("price_effect", "售价影响"),
        ("cost_effect", "成本影响"),
        ("rounding_adjustment", "舍入调整"),
        ("volume_up_profit_not_up", "销量增加但毛利未增"),
        ("record_state", "拆解说明"),
    ),
    "inventory_risk": (
        ("batch_code", "批次"),
        ("quantity", "库存数量"),
        ("occupied_amount", "采购成本占用"),
        ("purchase_unit_cost", "批次采购单价"),
        ("age_days", "库龄天数"),
        ("received_date", "入库日期"),
        ("expiry_date", "到期日"),
        ("expiry_remaining_days", "剩余效期天数"),
        ("recent_quantity", "观察期出库数量"),
        ("daily_quantity", "日均出库数量"),
        ("product_stock_days", "同品种库存覆盖天数"),
        ("estimated_clear_days", "先到期先售出累计预计售完天数"),
        ("risk_tags", "关注项"),
        ("data_note", "资料说明"),
    ),
    "stocking": (
        ("previous_quantity", "去年同期出库量"),
        ("recent_quantity", "近期出库量"),
        ("daily_quantity", "近期日均出库量"),
        ("baseline_demand", "去年同期日均折算参考需求"),
        ("recent_reference", "近期速度折算（仅对照）"),
        ("stock_quantity", "快照库存"),
        ("eligible_stock", "效期覆盖目标期库存"),
        ("excluded_stock", "效期不足库存"),
        ("unknown_expiry_stock", "效期未知库存"),
        ("scenario_ready", "资料满足网页情景测算"),
        ("needs_manual_demand", "需要手填目标需求"),
    ),
}


def identity(row, index):
    return f"{index}. {_label(str(row.get('name') or row.get('code', '')), 80)}\n" + " · ".join(
        f"{label}：{value(row.get(key))}"
        for key, label in (
            ("code", "编码"),
            ("specification", "规格"),
            ("manufacturer", "厂家"),
            ("unit", "单位"),
        )
    )


def field_value(result, row, key):
    raw = row.get(key)
    if isinstance(raw, bool):
        return "是" if raw else "否"
    if raw is None and key == "change_percent":
        return (
            "—（比较期毛利非正，不计算增长率）" if result.kind == "margin" else "—（无有效变化率）"
        )
    return value(raw)


def expanded_details(result, row_limit, currency):
    blocks = [f"金额币种 {currency}；金额、单价及数量保留原始精度，数量单位见各品种。"]
    for index, row in enumerate(selected_rows(result, row_limit), 1):
        lines = [identity(row, index)]
        if row.get("basis"):
            lines.append(BASES[row["basis"]] + " · " + row.get("direction", ""))
        lines += [f"{label}：{field_value(result, row, key)}" for key, label in FIELDS[result.kind]]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def details(step, result, currency, row_limit):
    if result.kind == "growth":
        metric = "quantity" if step.metric == "quantity" else "amount"
        keys = (
            "name",
            "comparison",
            "current_" + metric,
            "previous_" + metric,
            "delta",
            "change_percent",
        )
    elif result.kind == "price":
        keys = ("name", "comparison", "previous_price", "current_price", "delta", "change_percent")
    elif result.kind == "margin":
        keys = (
            "name",
            "comparison",
            "previous_profit",
            "current_profit",
            "delta",
            "change_percent",
        )
    elif result.kind == "inventory_risk":
        keys = ("name", "quantity", "occupied_amount", "age_days", "expiry_date", "risk_tags")
    else:
        keys = (
            "name",
            "previous_quantity",
            "recent_quantity",
            "baseline_demand",
            "stock_quantity",
            "eligible_stock",
        )
    labels = dict(FIELDS[result.kind]) | result.columns
    labels["name"] = result.columns.get("name", "品种") + "／规格／单位"
    if step.growth_basis == "previous" and result.kind in {"growth", "price", "margin"}:
        labels = {key: label.replace("比较期", "上期") for key, label in labels.items()}
    columns = [(key, labels[key]) for key in keys]
    rows = []
    for index, row in enumerate(selected_rows(result, row_limit), 1):
        output = {key: field_value(result, row, key) for key in keys}
        output["name"] = identity(row, index)
        if result.kind == "inventory_risk":
            output["name"] += "\n批次：" + value(row.get("batch_code"))
        if "comparison" in output:
            output["comparison"] = BASES[row["basis"]]
        rows.append(output)
    return columns, rows
