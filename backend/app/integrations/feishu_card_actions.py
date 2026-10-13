"""Signed, owner-bound actions over durable delivered results; no model or ERP calls on ingress."""

import hashlib
import hmac
import json
import re
from datetime import timedelta

from app.analysis.object_filters import binding_key
from app.analysis.sales_query import QueryUnavailable, _label
from app.integrations.feishu_app import authorized_identity
from app.integrations.feishu_conversation import conversation_closed, next_round
from app.integrations.feishu_display_components import panel, text
from app.schemas.analytics import AnalysisPlan
from app.semantic.catalog import load_catalog
from app.semantic.compiler import context_from_plan
from app.semantic.context import signing_key, snapshot_key

IDENTITY = ("app_id", "tenant_key", "chat_id", "user_open_id")
UNAVAILABLE = "按钮已失效或不属于你，请在本群重新 @我 提问。"


def signature(identity, job_id, action_id):
    principal = json.dumps([identity[k] for k in IDENTITY], separators=(",", ":"))
    return hmac.new(
        signing_key("feishu-card:" + principal).encode(),
        f"card.v1:{job_id}:{action_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def definitions(response):
    """Only explicit changes; keep every other step and filter unchanged."""
    actions = {}
    for index, (step, result) in enumerate(zip(response.plan.steps, response.results, strict=True)):
        if len(actions) >= 10:
            break  # Keep combined cards within native component and byte limits.
        if step.kind not in {"growth", "price", "margin"}:
            continue
        for basis, label in (
            ("previous", "只看环比"),
            ("year_over_year", "只看同比"),
            ("both", "同时比较"),
        ):
            if basis != step.growth_basis:
                actions[f"s{index}_{basis}"] = {
                    "label": f"{index + 1}. {label}",
                    "step": index,
                    "basis": basis,
                }
        if step.dimension == "buyer":
            continue
        seen = set()
        for row in result.rows:
            variant = {k: row.get(k) for k in ("specification", "manufacturer", "unit")}
            code = row.get("product_code") or row.get("code")
            key = json.dumps([code, variant], sort_keys=True)
            if not code or not all(variant.values()) or key in seen:
                continue
            seen.add(key)
            actions[f"s{index}_buyer{len(seen)}"] = {
                "label": f"{index + 1}. 客户贡献 · {_label(row['name'], 16)}",
                "description": _label(
                    f"{code} · {variant['specification']} · "
                    f"{variant['manufacturer']} · {variant['unit']}",
                    220,
                ),
                "step": index,
                "product": code,
                "variant": variant,
            }
            if len(seen) == 3:
                break
    return actions


def controls(actions, job):
    if not actions:
        return []
    elements = [
        text(
            "仅提问者可用，30分钟内有效；点击后发送新卡并计入轮次。组合查询按钮节选部分主题，"
            "其他品种或主题可继续用文字提问。",
            muted=True,
        )
    ]
    for action_id, action in actions.items():
        if action.get("description"):
            elements.append(text(action["description"], muted=True))
        elements.append(
            {
                "tag": "button",
                "type": "default",
                "width": "fill",
                "text": {"tag": "plain_text", "content": action["label"]},
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {
                            "job": job["job_id"],
                            "action": action_id,
                            "signature": signature(job, job["job_id"], action_id),
                        },
                    }
                ],
            }
        )
    group = panel("继续分析 · 切换比较／客户贡献", "")
    group["elements"] = elements
    return [group]


def latest_action_source(rows, arrived_at):
    """Isolated newest-first rows: pending, unknown, clear and newer results block old cards."""
    latest = next(
        (r for r in rows if r["payload"].get("kind") not in {"help", "coverage", "ping"}), None
    )
    if not latest:
        return None
    sent_at = latest.get("updated_at", latest["created_at"])
    if (
        latest["status"] != "sent"
        or latest["payload"].get("kind") != "analysis"
        or conversation_closed(latest["payload"])
        or not arrived_at - timedelta(minutes=30) <= sent_at <= arrived_at
        or not latest.get("result", {}).get("card_actions")
        or not latest.get("result", {}).get("reply_message_id")
    ):
        return None
    return latest


def accept(bot, data):
    """Called only by the authenticated SDK long-connection dispatcher."""
    try:
        header, event = data.header, data.event
        identity = {
            "app_id": header.app_id,
            "tenant_key": header.tenant_key,
            "chat_id": event.context.open_chat_id,
            "user_open_id": event.operator.open_id,
        }
        if (
            header.event_type != "card.action.trigger"
            or event.host != "im_message"
            or event.operator.tenant_key != header.tenant_key
            or not authorized_identity(bot.config_loader(), identity)
        ):
            return "ignored"
        value = event.action.value
        if not isinstance(value, dict) or set(value) != {"job", "action", "signature"}:
            return "ignored"
        source_id, action_id, sig = value["job"], value["action"], value["signature"]
        if (
            not isinstance(source_id, str)
            or not re.fullmatch(r"[a-f0-9]{1,64}", source_id)
            or not isinstance(action_id, str)
            or not re.fullmatch(r"s[0-5]_[a-z0-9_]{1,32}", action_id)
            or not isinstance(sig, str)
            or len(sig) != 64
            or not hmac.compare_digest(sig, signature(identity, source_id, action_id))
        ):
            return "ignored"
        if not isinstance(header.event_id, str) or not 0 < len(header.event_id) <= 200:
            return "ignored"
        message_id = event.context.open_message_id
        if (
            not isinstance(message_id, str)
            or not message_id.startswith("om_")
            or len(message_id) > 200
        ):
            return "ignored"
        # Synthetic message identity deduplicates repeated clicks of the same action.
        identity.update(
            event_id=header.event_id,
            message_id="card_" + hashlib.sha256(f"{source_id}:{action_id}".encode()).hexdigest(),
        )
        return bot.store.enqueue(
            identity,
            {
                "kind": "card_action",
                "source_job_id": source_id,
                "action_id": action_id,
                "reply_to_message_id": event.context.open_message_id,
            },
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return "ignored"


def action_payload(job, store, report):
    source = store.latest_card_action_source(job)
    request = job["payload"]
    if (
        not source
        or source["job_id"] != request["source_job_id"]
        or source["result"]["reply_message_id"] != request["reply_to_message_id"]
        or source["payload"].get("snapshot_key") != snapshot_key(report)
        or source["result"].get("card_action_catalog") != load_catalog()["version"]
    ):
        raise QueryUnavailable(UNAVAILABLE)
    action = source["result"]["card_actions"].get(request["action_id"])
    if not action:
        raise QueryUnavailable(UNAVAILABLE)
    plan = AnalysisPlan.model_validate(source["payload"]["plan"]).model_dump(mode="json")
    step = plan["steps"][action["step"]]
    if "basis" in action:
        step["growth_basis"] = action["basis"]
    else:
        # The selected row was already inside the original include/exclude scope.
        step["dimension"] = "buyer"
        step["filters"] = [
            f for f in step["filters"] if f["field"] != "product" or f["operator"] == "exclude"
        ] + [
            {
                "field": "product",
                "operator": "include",
                "code": action["product"],
                "variant": action["variant"],
            }
        ]
    plan = AnalysisPlan.model_validate(plan)
    context = context_from_plan(plan)
    bindings = []
    intents = []
    # Keep identifiers and variants in server bindings, never in cloud-visible terms.
    for intent, planned in zip(context.intents, plan.steps, strict=True):
        filters = []
        for index, (condition, bound) in enumerate(
            zip(intent.filters, planned.filters, strict=True)
        ):
            condition = condition.model_copy(
                update={
                    "id": f"filter_{index}",
                    "value": f"已选{'商品' if bound.field == 'product' else '客户'}{index + 1}",
                }
            )
            filters.append(condition)
            bindings.append(
                {
                    **binding_key(intent, condition),
                    "code": bound.code,
                    **({"variant": bound.variant.model_dump()} if bound.variant else {}),
                }
            )
        intents.append(intent.model_copy(update={"filters": filters}))
    context = context.model_copy(
        update={
            "intents": intents,
            "entity_bindings": bindings,
            "product_scope_note": source["payload"].get("product_scope_note", False),
        }
    )
    interpretation = ["通过结果卡操作：" + action["label"] + "；其余日期和筛选条件保留。"]
    if context.product_scope_note:
        interpretation.append(
            "沿用当前商品范围，未按药品类别筛选，可能包含器械、保健品等其他商品。"
        )
    return {
        **next_round(source["payload"]),
        "kind": "analysis",
        "plan": plan.model_dump(mode="json"),
        "snapshot_key": snapshot_key(report),
        "interpretation": interpretation,
        "semantic_context": context.model_dump(mode="json"),
        "product_scope_note": context.product_scope_note,
        "answered_card_job_id": source["job_id"],
        "answered_card_action_id": request["action_id"],
    }
