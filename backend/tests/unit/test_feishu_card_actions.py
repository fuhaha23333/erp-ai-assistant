"""Offline callback lifecycle, isolation, deduplication and exact variant drilldown."""

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from app.analysis.sales_query import QueryUnavailable
from app.integrations.feishu_card_actions import (
    action_payload,
    controls,
    definitions,
    latest_action_source,
)
from app.integrations.feishu_sales import SalesBot
from app.schemas.analytics import AnalysisPlan
from app.semantic.context import snapshot_key
from tests.unit.test_feishu_analytics import AnalyticsStore
from tests.unit.test_feishu_business_display import response
from tests.unit.test_feishu_cards import walk
from tests.unit.test_feishu_sales import config


class ActionStore(AnalyticsStore):
    def mark(self, job_id, status, error_code=None, *, reply_message_id=None):
        super().mark(job_id, status, error_code)
        if reply_message_id:
            self.rows[int(job_id)]["result"]["reply_message_id"] = reply_message_id

    def latest_card_action_source(self, job):
        rows = [
            r
            for r in reversed(self.rows)
            if r["job_id"] != job["job_id"]
            and r["created_at"] <= job["created_at"]
            and all(r[k] == job[k] for k in ("app_id", "tenant_key", "chat_id", "user_open_id"))
        ]
        return latest_action_source(rows, job["created_at"])


def setup_source(report, kind="growth", *, round_number=1):
    store = ActionStore()
    result = response(report, kind)
    identity = dict(
        app_id="cli_test",
        tenant_key="tenant-test",
        chat_id="oc_test",
        user_open_id="ou_user",
        message_id="om_question",
        event_id="evt_question",
    )
    store.enqueue(
        identity,
        {
            "kind": "analysis",
            "plan": result.plan.model_dump(mode="json"),
            "interpretation": [],
            "snapshot_key": snapshot_key(report),
            "conversation_round": round_number,
        },
    )
    bot = SalesBot(config, "ou_bot", store, lambda: report, analysis_planner=object())
    assert bot.process_pending(lambda *a: "om_result", card_sender=lambda *a: "om_result") == 1
    return bot, store, store.rows[0]


def event_for(source, action="s0_previous"):
    button = next(
        n
        for n in walk(source["result"]["reply_card"])
        if n.get("tag") == "button" and n["behaviors"][0]["value"]["action"] == action
    )
    return P2CardActionTrigger(
        {
            "schema": "2.0",
            "header": {
                "event_id": "evt_action",
                "app_id": "cli_test",
                "tenant_key": "tenant-test",
                "event_type": "card.action.trigger",
            },
            "event": {
                "host": "im_message",
                "operator": {"tenant_key": "tenant-test", "open_id": "ou_user"},
                "context": {"open_chat_id": "oc_test", "open_message_id": "om_result"},
                "action": {"tag": "button", "value": deepcopy(button["behaviors"][0]["value"])},
            },
        }
    )


def test_switch_is_durable_local_and_replies_to_clicked_card(risk_report):
    bot, store, source = setup_source(risk_report)
    event = event_for(source)
    bot.report_loader = lambda: pytest.fail("ingress must not load report")
    assert bot.accept_card_action(event) == "queued"
    assert bot.accept_card_action(event) == "duplicate"
    bot.report_loader = lambda: risk_report
    sent = []
    assert (
        bot.process_pending(
            lambda *a: pytest.fail("expected card"),
            card_sender=lambda mid, *a: sent.append(mid) or "om_next",
        )
        == 1
    )
    row = store.rows[1]
    assert sent == ["om_result"]
    assert row["payload"]["plan"]["steps"][0]["growth_basis"] == "previous"
    assert row["payload"]["conversation_round"] == 2
    assert row["payload"]["answered_card_job_id"] == "0"
    assert row["payload"]["reply_to_message_id"] == "om_result"
    assert row["result"]["reply_message_id"] == "om_next"
    assert {r["basis"] for r in row["result"]["results"][0]["rows"]} == {"previous"}


@pytest.mark.parametrize("kind", ["growth", "price", "margin"])
def test_drilldown_binds_exact_variant_and_private_context(risk_report, kind):
    bot, store, source = setup_source(risk_report, kind)
    original = deepcopy(source["payload"]["plan"])
    assert bot.accept_card_action(event_for(source, "s0_buyer1")) == "queued"
    assert bot.process_pending(lambda *a: "om_next", card_sender=lambda *a: "om_next") == 1
    row = store.rows[1]
    assert not row["result"].get("failed") and not row["result"].get("unavailable")
    step = row["payload"]["plan"]["steps"][0]
    assert step["dimension"] == "buyer" and step["growth_basis"] == "both"
    selected = next(f for f in step["filters"] if f["field"] == "product")
    assert selected["code"] == "P"
    assert selected["variant"] == dict(specification="合成规格", manufacturer="合成厂家", unit="盒")
    context = row["payload"]["semantic_context"]
    assert context["intents"][0]["filters"][0]["value"] == "已选商品1"
    assert context["entity_bindings"][0]["variant"] == selected["variant"]
    assert source["payload"]["plan"] == original
    assert all(r["product_code"] == "P" for r in row["result"]["results"][0]["rows"])
    # A subsequent natural-language patch must retain the server-only variant binding.
    from app.semantic.compiler import compile_request
    from app.semantic.entities import bind_entities
    from app.semantic.schemas import SemanticContext, SemanticRequest

    previous = SemanticContext.model_validate(context)
    compiled = compile_request(
        SemanticRequest(mode="followup", intents=[dict(growth_basis="previous")]),
        question="只看环比",
        today=datetime.now().date(),
        previous=previous,
        domains=("shipping",),
    )
    bound = bind_entities(compiled, risk_report)
    assert bound.status == "ready"
    assert bound.plan.steps[0].filters[0].variant.model_dump() == selected["variant"]


@pytest.mark.parametrize(
    "mutation", ["user", "chat", "tenant", "app", "signature", "action", "message", "shape"]
)
def test_tampered_or_other_member_clicks_are_rejected_before_enqueue(risk_report, mutation):
    bot, store, source = setup_source(risk_report)
    event = event_for(source)
    if mutation == "user":
        bot.config_loader = lambda: config().model_copy(
            update={"user_access_mode": "all_group_members"}
        )
        event.event.operator.open_id = "ou_other"
    elif mutation == "chat":
        event.event.context.open_chat_id = "oc_other"
    elif mutation == "tenant":
        event.event.operator.tenant_key = "other"
    elif mutation == "app":
        event.header.app_id = "cli_other"
    elif mutation == "message":
        event.event.context.open_message_id = None
    elif mutation == "shape":
        event.event.action.value["plan"] = {}
    else:
        event.event.action.value[mutation] = "forged"
    assert bot.accept_card_action(event) == "ignored" and len(store.rows) == 1


@pytest.mark.parametrize(
    "barrier", ["queued", "unknown", "clear", "expired", "closed", "receipt", "snapshot", "catalog"]
)
def test_old_or_unconfirmed_cards_cannot_execute(risk_report, barrier):
    bot, store, source = setup_source(risk_report)
    assert bot.accept_card_action(event_for(source)) == "queued"
    if barrier in {"queued", "unknown"}:
        source["status"] = barrier
    elif barrier == "clear":
        source["payload"]["kind"] = "clear_context"
    elif barrier == "expired":
        source["updated_at"] -= timedelta(minutes=31)
    elif barrier == "closed":
        source["payload"]["conversation_round"] = 5
    elif barrier == "receipt":
        source["result"]["reply_message_id"] = "om_different"
    elif barrier == "catalog":
        source["result"]["card_action_catalog"] = "stale"
    else:
        source["payload"]["snapshot_key"] = "stale"
    with pytest.raises(QueryUnavailable, match="按钮已失效"):
        action_payload(store.rows[1], store, risk_report)


def test_newer_pending_result_blocks_old_card_and_fifth_round_has_no_buttons(risk_report):
    _, _, last = setup_source(risk_report, round_number=5)
    assert not any(n.get("tag") == "button" for n in walk(last["result"]["reply_card"]))
    bot, store, source = setup_source(risk_report)
    newer = dict(source, job_id="pending", status="analyzing")
    assert latest_action_source([newer, source], datetime.now(timezone.utc)) is None
    assert bot.accept_card_action(event_for(source)) == "queued"
    source["result"]["card_actions"].pop("s0_previous")
    with pytest.raises(QueryUnavailable):
        action_payload(store.rows[1], store, risk_report)


def test_buttons_contain_no_plan_or_business_identifiers_and_fit_combined_card(risk_report):
    from app.integrations.feishu_analytics_cards import render_analysis
    from app.integrations.feishu_display_components import (
        MAX_CARD_BYTES,
        MAX_ELEMENTS,
        element_count,
    )

    result = response(risk_report, "growth")
    result = result.model_copy(
        update={
            "plan": AnalysisPlan(steps=result.plan.steps * 6),
            "results": result.results * 6,
        }
    )
    actions = definitions(result)
    job = dict(
        app_id="cli_test",
        tenant_key="tenant-test",
        chat_id="oc_test",
        user_open_id="ou_user",
        job_id="a" * 64,
    )
    elements = controls(actions, job)
    for node in walk(elements):
        if node.get("tag") == "button":
            assert set(node["behaviors"][0]["value"]) == {"job", "action", "signature"}
    card, _ = render_analysis(result, action_elements=elements)
    assert len(json.dumps(card, ensure_ascii=False).encode()) <= MAX_CARD_BYTES
    assert element_count(card) <= MAX_ELEMENTS
