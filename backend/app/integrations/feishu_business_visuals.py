"""Mobile business cards: exact KPI facts and bounded native VChart coordinates."""

from decimal import Decimal, InvalidOperation

from app.analysis.sales_query import _label
from app.integrations import feishu_business_display as business
from app.integrations.feishu_display_components import indicators, panel, text


def compact(raw, unit="", *, currency=None):
    if raw is None:
        return "—（无有效值）"
    number = Decimal(str(raw))
    suffix = ""
    if currency and abs(number) >= 10000:
        number /= 10000
        suffix = "万"
    prefix = ("¥ " if currency == "CNY" else currency + " ") if currency else ""
    return f"{prefix}{number:,.2f}{suffix}{unit}"


def overview(step, result, currency):
    totals = result.totals
    elements = []
    for group_index, group in enumerate(totals.get("groups", [])):
        basis = group["basis"]
        previous = "上期" if basis == "previous" else "去年同期"
        elements.append(
            text(
                business.BASES[basis] + "：" + business.interval(totals["periods"][basis]),
                muted=True,
            )
        )
        if result.kind == "growth":
            quantity = step.metric == "quantity"
            metric = "quantity" if quantity else "amount"
            unit = group["unit"] if quantity else ""
            current, before = group.get("current_" + metric), group.get("previous_" + metric)
            rate = (
                f"{(Decimal(current) - Decimal(before)) / Decimal(before) * 100:+.2f}%"
                if current is not None and before is not None and Decimal(before) > 0
                else "—（无有效基数）"
            )
            pairs = [
                (
                    "本期" + ("数量" if quantity else "金额"),
                    compact(current, unit, currency=None if quantity else currency),
                ),
                (
                    previous + ("数量" if quantity else "金额"),
                    compact(before, unit, currency=None if quantity else currency),
                ),
                ("净变化", compact(group["delta"], unit, currency=None if quantity else currency)),
                ("变化率", rate),
            ]
        elif result.kind == "price":
            pairs = [
                (label, f"{group[key]} 组")
                for key, label in (
                    ("decrease", "售价下降"),
                    ("increase", "售价上涨"),
                    ("unchanged", "售价持平"),
                    ("unavailable", "不可比"),
                )
            ]
        else:
            pairs = [
                ("本期毛利", compact(group["current_profit"], currency=currency)),
                (previous + "毛利", compact(group["previous_profit"], currency=currency)),
                ("毛利变化", compact(group["delta"], currency=currency)),
                (
                    "本期毛利率",
                    f"{Decimal(group['current_margin']) * 100:.2f}%"
                    if group.get("current_margin") is not None
                    else "—（收入为零）",
                ),
            ]
        if group_index:
            elements.append(text(" ｜ ".join(f"{label} {value}" for label, value in pairs)))
        else:
            for offset in (0, 2):
                elements.extend(indicators(pairs[offset : offset + 2]))
    if result.kind == "inventory_risk":
        elements.append(text(f"库存时点 {totals['as_of']} · 备份余额，非实时", muted=True))
        pairs = [
            ("正库存批次分组", str(totals["batch_count"])),
            ("采购成本占用", compact(totals["occupied_amount"], currency=currency)),
            ("已知成本占用", compact(totals["known_occupied_amount"], currency=currency)),
            ("成本缺失", f"{totals['unknown_cost_batches']} 组"),
        ]
        for offset in (0, 2):
            elements.extend(indicators(pairs[offset : offset + 2]))
    elif result.kind == "stocking":
        elements.append(text(f"库存时点 {totals['snapshot_date']} · 备份余额，非实时", muted=True))
        elements.append(text(f"{totals['product_count']} 个品种规格组合 · 按单位分别比较"))
        elements.append(
            text(
                "参考需求不是采购量；在途、到货日等需在网页按具体规格填写后测算。",
                muted=True,
            )
        )
    elements.append(panel("完整概览与比较期间", business.overview(step, result, currency)))
    return elements


def _bar(points, title, unit, *, money=False):
    """Each point is (category, series, exact value); never coerce missing to zero."""
    if not points or any(raw is None for _, _, raw in points):
        return None
    try:
        numbers = [Decimal(str(raw)) for _, _, raw in points]
    except (InvalidOperation, ValueError):
        return None
    if any(not n.is_finite() or abs(n) > 2**53 - 1 for n in numbers):
        return None
    divisor = Decimal(10000) if money and max(map(abs, numbers)) >= 10000 else Decimal(1)
    axis_unit = ("万" if divisor == 10000 else "") + unit.replace("CNY", "元")
    values = [
        {"label": label, "series": series, "value": float(number / divisor), "exact": str(raw)}
        for (label, series, raw), number in zip(points, numbers, strict=True)
    ]
    series = list(dict.fromkeys(p[1] for p in points))
    spec = {
        "type": "bar",
        "direction": "horizontal",
        "animation": False,
        "data": {"values": values},
        "xField": "value",
        "yField": ["label", "series"] if len(series) > 1 else "label",
        "seriesField": "series",
        "color": ["#f59e42"]
        if series == ["下降"]
        else ["#3370ff", "#f59e42", "#7e67d2", "#36a692"],
        "barMaxWidth": 32,
        "label": {"visible": True, "position": "outside"},
        "axes": [
            {"orient": "bottom", "title": {"visible": True, "text": axis_unit}},
            {"orient": "left", "label": {"autoLimit": True}, "maxWidth": 100},
        ],
        "legends": {"visible": len(series) > 1, "orient": "bottom"},
    }
    return [
        text(title, size="heading"),
        {"tag": "chart", "chart_spec": spec, "height": "280px", "preview": True},
    ]


def charts(step, result, currency):
    """Yield independently labelled charts, preserving basis, unit and variant boundaries."""
    if result.kind == "growth":
        for group in result.totals["groups"]:
            unit = group["unit"] if step.metric == "quantity" else currency
            rows = [
                r
                for r in result.rows
                if r["basis"] == group["basis"] and (step.metric != "quantity" or r["unit"] == unit)
            ]
            points = []
            for direction, label, total in ((1, "增长", "positive"), (-1, "下降", "negative")):
                selected = [r for r in rows if Decimal(r["delta"]) * direction > 0][:5]
                points.extend(
                    (f"{label}{i}. {_label(r['name'], 12)}", label, r["delta"])
                    for i, r in enumerate(selected, 1)
                )
                rest = Decimal(group[total]) * direction - sum(
                    (Decimal(r["delta"]) for r in selected), Decimal(0)
                )
                if rest:
                    points.append(("其余" + label, label, str(rest)))
            yield _bar(
                points,
                business.BASES[group["basis"]] + " · 增减贡献（各取前5项）",
                unit,
                money=step.metric != "quantity",
            )
    elif result.kind in {"price", "margin"}:
        for row in result.rows:
            name = _label(row["name"], 18)
            caption = f"{business.BASES[row['basis']]} · {name}"
            identity = f"规格 {row['specification']} · {row['manufacturer']} · {row['unit']}"
            previous = "上期" if row["basis"] == "previous" else "去年同期"
            if result.kind == "price":
                points = [
                    (label, series, row.get(prefix + field))
                    for field, label in (
                        ("price", "平均售价"),
                        ("unit_cost", "单位成本"),
                        ("spread", "单位毛利"),
                    )
                    if row.get("previous_" + field) is not None
                    and row.get("current_" + field) is not None
                    for prefix, series in (("previous_", previous), ("current_", "本期"))
                ]
                graphic = _bar(points, caption, currency + "/" + row["unit"])
            else:
                points = [
                    (label, "影响金额", row.get(key))
                    for key, label in (
                        ("volume_effect", "销量影响"),
                        ("price_effect", "售价影响"),
                        ("cost_effect", "成本影响"),
                        ("rounding_adjustment", "舍入调整"),
                    )
                ]
                graphic = _bar(points, caption + " · 毛利变化拆解", currency, money=True)
            if graphic:
                yield [text(_label(identity, 180), muted=True)] + graphic
    elif result.kind == "inventory_risk":
        totals = result.totals
        points = [
            (label, "批次分组", totals[key])
            for key, label in (
                ("aged", f"库龄≥{totals['age_threshold_days']}天"),
                ("near_expiry", f"效期≤{totals['expiry_threshold_days']}天"),
                ("expired", "已过期"),
                ("no_recent_sales", "无近期出库"),
                ("expiry_mismatch", "售完晚于到期"),
                ("data_issue", "资料待核实"),
            )
        ]
        yield _bar(points, "库存关注分布 · 各项可重叠，不可相加", "组")
    else:
        for row in result.rows:
            graphic = _bar(
                [
                    ("同期参考需求", "数量", row.get("baseline_demand")),
                    ("近期速度对照", "数量", row.get("recent_reference")),
                    ("效期合格库存", "数量", row.get("eligible_stock")),
                ],
                _label(row["name"], 18) + " · 备货依据",
                row["unit"],
            )
            if graphic:
                yield [
                    text(_label(f"{row['specification']} · {row['manufacturer']}", 150), muted=True)
                ] + graphic


def compact_details(step, result, currency, row_limit):
    columns, rows = business.details(step, result, currency, row_limit)
    return panel(
        "核心明细（展开逐项阅读）",
        "\n\n".join("\n".join(f"{label}：{row[key]}" for key, label in columns) for row in rows),
    )
