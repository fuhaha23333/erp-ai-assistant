"""Versioned Feishu templates, native pagination and bounded cards."""

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from app.analysis.operations import DOMAIN_LABELS
from app.analysis.sales_query import _label
from app.integrations import feishu_business_display as business
from app.integrations import feishu_business_visuals as visuals
from app.integrations.feishu_analytics_layouts import RANKINGS, details, metrics, result_chart
from app.integrations.feishu_analytics_text import _blocks
from app.integrations.feishu_display_components import (
    MAX_CARD_BYTES,
    MAX_CHARTS,
    MAX_ELEMENTS,
    MAX_TABLES,
    ROW_LIMITS,
    amount,
    element_count,
    panel,
    table,
    text,
)
from app.schemas.analytics import AnalysisResponse


def _period(step) -> str:
    value = f"{step.start_date} — {step.end_date_exclusive - timedelta(days=1)}"
    if step.kind in RANKINGS:
        metric = (
            ("销售金额" if step.metric == "amount" else "订单数")
            if step.domain == "sales"
            else {
                "amount": DOMAIN_LABELS[step.domain] + "金额",
                "orders": "单据数",
                "quantity": "数量（分单位）",
                "stock": "库存数量（分单位）",
            }[step.metric]
        )
        direction = "从高到低" if step.order == "descending" else "从低到高"
        value += f"\n按{metric}{direction} · 每组前 {step.top_n} 名"
    if step.kind == "comparison":
        end = step.comparison_end_date_exclusive - timedelta(days=1)
        value += f"\n比较期 {step.comparison_start_date} — {end}"
    return value


def _card(
    response: AnalysisResponse,
    row_limit: int,
    charts: bool,
    conversation_notice: str | None = None,
    *,
    expanded: bool = True,
    action_elements: list[dict] | None = None,
) -> dict:
    p = response.provenance
    scope = "全平台采购企业" if p.scope.all_buyers else "当前授权采购企业范围"
    source = "合成样例" if p.source_kind == "synthetic" else "历史备份"
    elements = [text(f"{scope} · {source} · {_label(p.currency, 8)}", muted=True)]
    # Keep this caveat visible, not hidden behind the disclosure panel.
    scope_notes = [line for line in response.interpretation if "未按药品类别筛选" in line]
    if scope_notes:
        elements.append(text("\n".join(dict.fromkeys(scope_notes)), muted=True))
    table_count = chart_count = 0
    for index, (step, result) in enumerate(zip(response.plan.steps, response.results, strict=True)):
        if index:
            elements.append({"tag": "hr"})
        if len(response.results) > 1:
            elements.append(text(f"{index + 1}. {result.title}", size="heading"))
        elements.append(text(_period(step), muted=True))
        if result.kind in business.KINDS:
            elements.extend(
                visuals.overview(step, result, p.currency)
                if expanded
                else [
                    text(business.overview(step, result, p.currency)),
                ]
            )
            available = [v for v in visuals.charts(step, result, p.currency) if v]
            chosen = available[: MAX_CHARTS - chart_count] if charts else []
            for visual in chosen:
                elements.extend(visual)
            chart_count += len(chosen)
            if chosen:
                elements.append(
                    text(
                        "点击图表可放大；图中金额为绘图近似值，精确值见展开明细。",
                        muted=True,
                    )
                )
            if len(chosen) < len(available):
                elements.append(
                    text(
                        "卡片图表已节选；其余比较基准、品种及单位见下方明细。",
                        muted=True,
                    )
                )
            elif not available and result.rows:
                elements.append(
                    text("本次未生成图表；有效数值及不可比原因见展开明细。", muted=True)
                )
            if not result.rows:
                elements.append(text("当前方向和排序条件下没有明细；完整范围统计见上方概览。"))
            elif expanded:
                elements.append(
                    panel(
                        "逐项详细数据（原始精度）",
                        business.expanded_details(result, row_limit, p.currency),
                    )
                )
            else:
                elements.append(visuals.compact_details(step, result, p.currency, row_limit))
                elements.append(
                    text(
                        "组合结果超过卡片容量，保留概览与核心明细；"
                        "逐项详细数据请单独询问该主题，或在网页查看。",
                        muted=True,
                    )
                )
            if len(result.rows) > row_limit:
                elements.append(
                    text(
                        f"卡片节选 {row_limit} 行，本次返回 {len(result.rows)} 行；"
                        "概览不受节选影响；完整明细请在网页查看或下载。",
                        muted=True,
                    )
                )
            if result.notes:
                elements.append(
                    panel(
                        "分析说明",
                        "\n".join(_label(n, 300) for n in result.notes[:8]),
                    )
                )
            continue
        elif result.domain == "inventory":
            elements.append(
                text(
                    f"库存时点 {result.totals['as_of']}；不是实时或日末库存，数量按单位分开。",
                    muted=True,
                )
            )
        elements.extend(metrics(result, p.currency))
        if result.kind == "comparison":
            elements.append(text("按变化金额绝对值排序 · 贡献拆解不代表因果", muted=True))
        if result.kind == "anomalies":
            elements.append(
                text("波动阈值50%；区间首日不参与检测，规则线索不代表因果。", muted=True)
            )
        if not result.rows:
            elements.append(
                text(
                    "没有达到波动阈值的记录。"
                    if result.kind == "anomalies"
                    else "没有满足条件的记录。可换一个完整日期区间。"
                )
            )
        elif result.kind != "summary" or result.domain != "sales":
            visual = (
                result_chart(step, result, row_limit)
                if charts and chart_count < MAX_CHARTS
                else None
            )
            if visual:
                elements.append(visual)
                chart_count += 1
            columns, rows = details(step, result, p.currency, row_limit)
            use_table = table_count < MAX_TABLES and bool(columns)
            if use_table:
                elements.append(table(columns, rows))
                table_count += 1
            else:
                # Feishu permits at most five tables, and only at the root.
                elements.append(
                    panel(
                        "展开明细（精简列表）",
                        "\n\n".join(
                            "；".join(f"{label}：{row[key]}" for key, label in columns)
                            for row in rows
                        ),
                    )
                )
            if len(result.rows) > row_limit:
                elements.append(
                    text(
                        f"卡片节选前 {row_limit} 行，共 {len(result.rows)} 行；"
                        "可缩短日期或减少排行条数查看。",
                        muted=True,
                    )
                )
            else:
                elements.append(
                    text(
                        f"共 {len(result.rows)} 行 · 表格可翻页查看"
                        if use_table
                        else f"共 {len(result.rows)} 行",
                        muted=True,
                    )
                )
        if result.kind == "comparison":
            elements.append(
                text(
                    f"其余维度变化金额 {amount(result.totals.get('other_delta'), p.currency)}",
                    muted=True,
                )
            )
        notes = result.findings + result.notes
        if notes:
            elements.append(panel("分析说明", "\n".join(_label(note, 300) for note in notes[:8])))
    source_time = p.source_as_of.astimezone(ZoneInfo(p.business_timezone))
    elements.extend(action_elements or [])
    explanation = [
        "本次问题理解：\n" + "\n".join(response.interpretation),
        "销售纳入状态："
        + "、".join(p.included_statuses)
        + "；其他业务纳入状态和日期依据见各项分析说明。金额非资金收付，销售未扣退款。",
        "金额概览显示2位小数；新经营主题明细保留原始精度。"
        "计算及留存金额保留4位。图表用于观察趋势，精确值以明细为准。",
        f"数据截至 {source_time:%Y-%m-%d %H:%M}（{p.business_timezone}）",
        f"规则 {_label(p.policy_id, 64)}",
        "数据说明：\n" + "\n".join(_label(warning, 300) for warning in response.warnings[:8]),
    ]
    elements += [
        panel("统计口径与数据说明", "\n\n".join(explanation)),
        text(f"查询编号 {response.run_id}", muted=True),
        text(
            conversation_notice
            or (
                "30分钟内可在本群继续 @我，说明想修改的条件或接着想了解什么；"
                "如需开始新话题，发送“重新开始”。"
            ),
            muted=True,
        ),
    ]
    title = response.results[0].title if len(response.results) == 1 else "AI 数据分析"
    return {
        "schema": "2.0",
        "config": {
            "width_mode": "fill",
            "update_multi": True,
            "summary": {"content": title + " · 分析已完成"},
        },
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}},
        "body": {"padding": "12px", "vertical_spacing": "12px", "elements": elements},
    }


def render_analysis(
    response: AnalysisResponse,
    *,
    conversation_notice: str | None = None,
    action_elements: list[dict] | None = None,
) -> tuple[dict, str]:
    # Keep business graphics while reducing duplicate detail text. Exact facts stay stored.
    has_business = any(result.kind in business.KINDS for result in response.results)
    for row_limit in ROW_LIMITS:
        for charts in (
            (True,) if has_business else ((True, False) if row_limit == ROW_LIMITS[0] else (False,))
        ):
            card = _card(
                response, row_limit, charts, conversation_notice, action_elements=action_elements
            )
            if (
                element_count(card) <= MAX_ELEMENTS
                and len(json.dumps(card, ensure_ascii=False).encode("utf-8")) <= MAX_CARD_BYTES
            ):
                return card, "\n\n".join(_blocks(response, row_limit, conversation_notice))
    if has_business:
        for row_limit in ROW_LIMITS:
            card = _card(
                response,
                row_limit,
                False,
                conversation_notice,
                expanded=False,
                action_elements=action_elements,
            )
            if (
                element_count(card) <= MAX_ELEMENTS
                and len(json.dumps(card, ensure_ascii=False).encode("utf-8")) <= MAX_CARD_BYTES
            ):
                return card, "\n\n".join(
                    _blocks(response, row_limit, conversation_notice, expanded=False)
                )
    raise ValueError("分析卡片超出展示范围，请缩小问题范围")
