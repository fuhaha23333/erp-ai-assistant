"""Select presentation from result semantics, never from the user's phrasing."""

from app.analysis.operations import DOMAIN_LABELS
from app.analysis.sales_query import _label
from app.integrations import feishu_business_display as business
from app.integrations.feishu_display_components import amount, chart, indicators, percent, text

RANKINGS = {"product_ranking", "buyer_ranking"}


def metrics(result, currency: str) -> list[dict]:
    totals = result.totals
    if result.kind in {"growth", "price", "margin", "inventory_risk", "stocking"}:
        return []
    elif result.domain != "sales":
        if result.domain == "inventory":
            return indicators(
                [
                    ("有库存商品", f"{totals.get('positive_product_count', 0)} 种"),
                    ("库存批次记录", str(totals.get("batch_count", 0))),
                ]
            )
        label = DOMAIN_LABELS[result.domain]
        return indicators(
            [
                (label + "金额", amount(totals.get("amount"), currency)),
                (label + "单据数", str(totals.get("document_count", 0))),
            ]
        )
    if result.kind == "comparison":
        return indicators(
            [
                ("本期金额", amount(totals.get("amount"), currency)),
                ("比较期金额", amount(totals.get("previous_amount"), currency)),
            ]
        ) + indicators(
            [
                ("变化金额", amount(totals.get("delta"), currency)),
                ("变化率", percent(totals.get("change_rate"))),
            ]
        )
    elements = []
    if "amount" in totals:
        elements += indicators(
            [
                ("有效销售金额", amount(totals["amount"], currency)),
                ("有效订单", f"{totals.get('order_count', 0):,} 单"),
            ]
        )
    if result.kind == "summary" and result.rows:
        row = result.rows[0]
        elements += indicators(
            [
                ("采购企业", f"{row.get('buyer_count', 0):,} 家"),
                ("销售商品", f"{row.get('product_count', 0):,} 种"),
            ]
        )
        elements.append(
            text(f"平均每单金额  {amount(row.get('average_order_amount'), currency)}", muted=True)
        )
    return elements


def result_chart(step, result, row_limit: int) -> dict | None:
    if result.domain != "sales":
        if step.metric in {"quantity", "stock"} or result.kind not in RANKINGS | {"trend"}:
            return None
        if result.kind == "trend" and len(result.rows) > row_limit:
            return None
        return chart(
            result.rows[: row_limit if result.kind == "trend" else min(5, row_limit)],
            kind="line" if result.kind == "trend" else "bar",
            field="amount" if step.metric == "amount" else "document_count",
            title=DOMAIN_LABELS[result.domain] + ("金额" if step.metric == "amount" else "单据数"),
        )
    if result.kind == "trend":
        # A truncated date range must not pretend to be the requested full trend.
        if len(result.rows) > row_limit:
            return None
        return chart(
            sorted(result.rows, key=lambda row: row["day"]),
            kind="line",
            field="amount" if step.metric == "amount" else "order_count",
            title="每日销售金额" if step.metric == "amount" else "每日订单数",
        )
    if result.kind in RANKINGS:
        rows = result.rows[: min(5, row_limit)]
        return chart(
            rows,
            kind="bar",
            field="amount" if step.metric == "amount" else "order_count",
            title=f"前 {len(rows)} 项 · " + ("销售金额" if step.metric == "amount" else "订单数"),
        )
    return None


def details(
    step, result, currency: str, row_limit: int
) -> tuple[list[tuple[str, str]], list[dict]]:
    if result.kind in business.KINDS:
        return business.details(step, result, currency, row_limit)
    if result.domain != "sales":
        columns = [
            (key, label)
            for key, label in result.columns.items()
            if key not in {"source_ids", "code"}
        ][:6]
        if result.kind == "list" and step.dimension != "buyer" and result.domain != "inventory":
            columns = [
                (key, result.columns[key])
                for key in ("name", "amount", "quantity", "unit", "document_number", "day")
            ]
    elif result.kind in RANKINGS:
        columns = [("name", "商品 / 编码" if result.kind == "product_ranking" else "企业 / 编码")]
        columns += (
            [("amount", "销售金额"), ("order_count", "订单数")]
            if step.metric == "amount"
            else [("order_count", "订单数"), ("amount", "销售金额")]
        )
    elif result.kind == "comparison":
        columns = [
            ("name", "商品 / 编码" if step.dimension == "product" else "企业 / 编码"),
            ("delta", "变化金额"),
            ("amount", "本期金额"),
            ("previous_amount", "比较期金额"),
        ]
    elif result.kind == "trend":
        columns = [("day", "日期"), ("amount", "销售金额"), ("order_count", "订单数")]
    elif result.kind == "anomalies":
        columns = [
            ("day", "日期"),
            ("amount", "当日金额"),
            ("previous_amount", "前日金额"),
            ("change_rate", "变化率"),
        ]
    else:
        columns = list(result.columns.items())[:6]
    rows = []
    source_rows = (
        sorted(result.rows, key=lambda row: row["day"]) if result.kind == "trend" else result.rows
    )
    for index, row in enumerate(source_rows[:row_limit], 1):
        output = {}
        for key, _ in columns:
            value = row.get(key)
            if key == "name":
                name = _label(str(value or row.get("code", "—")), 80)
                code = _label(str(row.get("code", "")), 64)
                value = f"{row.get('rank', index)}. {name}\n{code}"
            elif key in {"amount", "previous_amount", "delta", "average_order_amount"} and not (
                result.kind == "price" or result.kind == "growth" and step.metric == "quantity"
            ):
                value = amount(value, currency)
            elif key == "change_rate":
                value = percent(value)
            else:
                value = _label(str(value), 120) if value is not None else "—"
            output[key] = value
        rows.append(output)
    return columns, rows
