"""Offline synthetic layout harness, not an emulator of the Feishu client.

Run from repository root, then node scripts/check_feishu_display.cjs.
No ERP access, model calls, credentials, or message sends.
"""

import html
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ["ERP_ENV_FILE"] = str(ROOT / "backend/tests/.env.test-not-present")
os.environ["ERP_CONTEXT_SIGNING_KEY"] = "ab" * 32  # Public synthetic preview key.

from app.analysis.analytics import execute_analysis
from app.analysis.sales import analyze_sales
from app.core.business_rules import load_business_rules
from app.integrations.feishu_analytics_cards import render_analysis
from app.schemas.analytics import AnalysisPlan
from tests.conftest import (
    extract,
    growth_report,
    margin_report,
    multi_report,
    risk_report,
    scope,
    source_as_of,
    window,
)

OUTPUT = ROOT / ".local" / "feishu-display"


def fixtures():
    report = analyze_sales(
        extract.__wrapped__(),
        window.__wrapped__(),
        scope.__wrapped__(),
        source_as_of=source_as_of.__wrapped__(),
        rules=load_business_rules(),
        synthetic=True,
    )
    results = {}
    for kind in (
        "summary",
        "product_ranking",
        "buyer_ranking",
        "trend",
        "comparison",
        "anomalies",
    ):
        step = {"kind": kind, "start_date": "2026-09-01", "end_date_exclusive": "2026-09-03"}
        if kind == "comparison":
            step.update(
                start_date="2026-09-02",
                comparison_start_date="2026-09-01",
                comparison_end_date_exclusive="2026-09-02",
            )
        response = execute_analysis(report, AnalysisPlan(steps=[step]))
        results[kind] = response
    for kind in ("product_ranking", "anomalies"):
        empty = results[kind].model_copy(deep=True)
        empty.results[0].rows.clear()
        empty.results[0].totals.update(amount="0.0000", order_count=0)
        empty.results[0].findings.clear()
        results["empty_" + kind] = empty
    long = results["product_ranking"].model_copy(deep=True)
    long.results[0].rows[:] = [
        dict(
            long.results[0].rows[0],
            code=f"SKU-{i:03}",
            name="合成商品长名称与规格（演示）" * 20,
        )
        for i in range(50)
    ]
    long.plan.steps[0] = long.plan.steps[0].model_copy(update={"top_n": 50})
    results["long_names"] = long
    results["combined"] = long.model_copy(
        update={
            "results": long.results * 6,
            "plan": AnalysisPlan(steps=long.plan.steps * 6),
        }
    )
    huge = results["comparison"].model_copy(deep=True)
    huge.results[0].totals.update(
        amount="9007199254740993.1234",
        previous_amount="0.0000",
        delta="9007199254740993.1234",
        change_rate=None,
    )
    huge.results[0].rows[0].update(amount="9007199254740993.1234", delta="-1.0000")
    results["large_zero_base"] = huge
    orders = results["product_ranking"].model_copy(deep=True)
    orders.plan.steps[0] = orders.plan.steps[0].model_copy(update={"metric": "orders"})
    results["orders"] = orders
    unknown = results["product_ranking"].model_copy(deep=True)
    unknown.results[0] = unknown.results[0].model_copy(update={"kind": "future_kind"})
    results["generic"] = unknown
    business_report = risk_report.__wrapped__(
        margin_report.__wrapped__(
            growth_report.__wrapped__(
                multi_report.__wrapped__(
                    extract.__wrapped__(),
                    window.__wrapped__(),
                    scope.__wrapped__(),
                    source_as_of.__wrapped__(),
                )
            )
        )
    )
    for kind, metric in (
        ("growth", "amount"),
        ("price", "unit_price"),
        ("margin", "gross_profit"),
        ("inventory_risk", "stock"),
        ("stocking", "stock"),
    ):
        step = dict(
            domain="shipping",
            kind=kind,
            metric=metric,
            start_date="2026-08-01",
            end_date_exclusive="2026-09-01",
        )
        if kind == "inventory_risk":
            step.update(
                domain="inventory", start_date="2026-09-16", end_date_exclusive="2026-09-17"
            )
        elif kind == "stocking":
            step.update(
                domain="inventory", start_date="2026-09-17", end_date_exclusive="2026-09-24"
            )
        results[kind] = execute_analysis(business_report, AnalysisPlan(steps=[step]))
    quantity = results["growth"].plan.steps[0].model_copy(update={"metric": "quantity"})
    results["growth_quantity"] = execute_analysis(business_report, AnalysisPlan(steps=[quantity]))
    results["business_combined"] = results["growth"].model_copy(
        update={
            "plan": AnalysisPlan(
                steps=[
                    results[k].plan.steps[0]
                    for k in ("growth", "price", "margin", "inventory_risk", "stocking")
                ]
            ),
            "results": [
                results[k].results[0]
                for k in ("growth", "price", "margin", "inventory_risk", "stocking")
            ],
        }
    )
    return results


def safe_json(value):
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace("&", "\\u0026")


def content(node):
    return html.escape(node["content"]).replace("\n", "<br>")


def render(node):
    tag = node["tag"]
    if tag == "hr":
        return "<hr>"
    if tag == "div":
        value = node["text"]
        style = "muted" if value.get("text_color") == "grey" else ""
        style += " heading" if value.get("text_size") == "heading" else ""
        return f'<div class="text {style}">{content(value)}</div>'
    if tag == "column_set":
        return (
            '<div class="columns">'
            + "".join(
                "<div>" + "".join(map(render, column["elements"])) + "</div>"
                for column in node["columns"]
            )
            + "</div>"
        )
    if tag == "collapsible_panel":
        return (
            "<details><summary>"
            + content(node["header"]["title"])
            + "</summary>"
            + "".join(map(render, node["elements"]))
            + "</details>"
        )
    if tag == "table":
        return (
            '<div class="table-wrap"><script type="application/json">'
            + safe_json(node)
            + '</script><div class="scroll"><table></table></div><div class="pager"><button>上一页</button><span></span><button>下一页</button></div></div>'
        )
    if tag == "chart":
        return (
            '<div class="chart" style="height:' + html.escape(node.get("height", "240px"))
            + '"><script type="application/json">'
            + safe_json(node["chart_spec"])
            + "</script></div>"
        )
    if tag == "button":
        return '<button type="button" class="card-action">' + content(node["text"]) + '</button>'
    raise ValueError(tag)


CSS = """*{box-sizing:border-box}body{margin:0;padding:24px 12px;background:#f3f5f8;color:#1f2329;font:14px/1.6 "Microsoft YaHei",sans-serif}main{max-width:640px;margin:auto}.card{background:white;border:1px solid #dee0e3;border-radius:12px;overflow:hidden}.header{padding:16px;background:#e1eaff;color:#245bdb;font-size:19px;font-weight:700}.body{display:flex;flex-direction:column;gap:12px;padding:16px;min-width:0}.text{overflow-wrap:anywhere}.muted{color:#646c7a;font-size:12px}.heading{font-size:18px;font-weight:600}.columns{display:grid;grid-template-columns:1fr 1fr;gap:14px}.columns>div{min-width:0}hr{width:100%;border:0;border-top:1px solid #eceef1;margin:0}details{border:1px solid #e5e7eb;border-radius:6px;padding:10px}summary{cursor:pointer;color:#4b5563;font-size:13px}details .text{margin-top:8px}.chart{height:240px;width:100%;overflow:hidden}.scroll{overflow:auto;max-width:100%}table{border-collapse:collapse;width:100%;font-size:12px}th{background:#f5f6f7;color:#646c7a;text-align:left}td,th{padding:8px;border-bottom:1px solid #eff0f1;min-width:90px;max-width:200px;overflow-wrap:anywhere}td{white-space:pre-line}td:first-child{min-width:130px}td>div{max-height:48px;overflow:hidden}.pager{display:flex;justify-content:space-between;align-items:center;padding:8px 0;color:#646c7a;font-size:12px}button{border:1px solid #ddd;border-radius:4px;background:white;color:#245bdb;padding:4px 10px;cursor:pointer}button:disabled{color:#aaa}.caption{color:#646c7a;font-size:11px;margin:12px 0}@media(max-width:440px){body{padding:12px 6px}.body{padding:12px}.heading{font-size:16px}}"""

JS = """
window.previewErrors=[];
window.addEventListener('error', e=>window.previewErrors.push(e.message));
document.querySelectorAll('.table-wrap').forEach(wrap=>{
  const data=JSON.parse(wrap.querySelector('script').textContent);
  let page=0; const pages=Math.ceil(data.rows.length/data.page_size);
  const buttons=wrap.querySelectorAll('button');
  function draw(){
    const table=wrap.querySelector('table');table.replaceChildren();
    const tr=document.createElement('tr');
    data.columns.forEach(c=>{const th=document.createElement('th');th.textContent=c.display_name;tr.append(th)});
    table.append(tr);
    data.rows.slice(page*data.page_size,(page+1)*data.page_size).forEach(row=>{
      const tr=document.createElement('tr');
      data.columns.forEach(c=>{const td=document.createElement('td');const label=document.createElement('div');label.textContent=row[c.name];td.title=row[c.name];td.append(label);tr.append(td)});table.append(tr);
    });
    buttons[0].disabled=page===0;buttons[1].disabled=page===pages-1;
    wrap.querySelector('.pager span').textContent=`${page+1} / ${pages} 页 · ${data.rows.length} 行`;
  }
  buttons[0].onclick=()=>{page--;draw()};buttons[1].onclick=()=>{page++;draw()};draw();
});
window.chartReady=Promise.all([...document.querySelectorAll('.chart')].map(async (node,i)=>{
  const spec=JSON.parse(node.querySelector('script').textContent);node.id='chart_'+i;
  const chart=new VChart.default({...spec,animation:false},{dom:node.id});await chart.renderAsync();
}));
"""


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, response in fixtures().items():
        from app.integrations.feishu_card_actions import controls, definitions

        # Synthetic identity/key only. Preview never uses the developer's .env or sends events.
        actions = controls(definitions(response), {
            "job_id": "a" * 64, "app_id": "cli_preview", "tenant_key": "tenant-preview",
            "chat_id": "oc_preview", "user_open_id": "ou_preview",
        })
        card, _ = render_analysis(response, action_elements=actions)
        (OUTPUT / f"{name}.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        page = (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>'
            + CSS
            + '</style><main><article class="card"><div class="header">'
            + content(card["header"]["title"])
            + '</div><div class="body">'
            + "".join(map(render, card["body"]["elements"]))
            + '</div></article><div class="caption">合成数据布局检查 · VChart 1.12.3 · 最终显示以飞书客户端为准</div></main><script src="../vchart-1.12.3.min.js"></script><script>'
            + JS
            + "</script></html>"
        )
        (OUTPUT / f"{name}.html").write_text(page, encoding="utf-8")
    print(f"Generated {len(fixtures())} synthetic card fixtures in .local/feishu-display")


if __name__ == "__main__":
    main()
