"""Durable bridge between Feishu's synchronous worker and shared analysis planning."""

import asyncio
import json
from datetime import date

from pydantic import ValidationError

from app.ai.analytics_language import compact_question
from app.ai.sales_intent import parse_local_intent
from app.analysis.analytics import execute_analysis
from app.analysis.planning import describe_interpretation, resolve_question
from app.analysis.sales_query import QueryUnavailable
from app.integrations.feishu_analytics_cards import render_analysis
from app.integrations.feishu_conversation import conversation_closed, next_round, round_notice
from app.integrations.feishu_guidance import numbered_choices as _numbered_choices
from app.integrations.feishu_guidance import render_guidance as _render_guidance
from app.schemas.analytics import AnalysisPlan, AnalysisQuestion
from app.semantic.context import snapshot_key as snapshot_key
from app.semantic.dialogue_schemas import ConversationRequest, DialogueTurn
from app.semantic.schemas import SemanticContext


def question_payload(question: str, today: date) -> dict:
    try:
        body = AnalysisQuestion(question=question)
    except ValidationError:
        raise QueryUnavailable("请将分析问题控制在1—1000字。") from None
    try:
        intent = parse_local_intent(question, today)
    except QueryUnavailable:
        return {"kind": "analysis_question", "question": body.question, "today": today.isoformat()}
    # Existing finite commands remain usable without a model call.
    kind = {
        "sales_summary": "summary",
        "sales_trend": "trend",
        "product_ranking": "product_ranking",
    }[intent.tool]
    plan = AnalysisPlan(steps=[dict(kind=kind, **intent.to_query().model_dump())])
    return {
        "kind": "analysis",
        "plan": plan.model_dump(mode="json"),
        "interpretation": describe_interpretation(body, plan),
        "product_scope_note": False,
    }


def _save(store, job, payload):
    if job["payload"].get("reply_to_message_id"):
        payload = dict(payload, reply_to_message_id=job["payload"]["reply_to_message_id"])
    if not store.replace_payload(job, payload):
        raise QueryUnavailable("任务已被重新领取，请重新提问。")
    job["payload"] = payload


def _store_result(result, payload, job=None):
    from app.integrations.feishu_card_actions import controls, definitions

    actions = definitions(result) if job and not conversation_closed(payload) else {}
    card, text = render_analysis(result, conversation_notice=round_notice(payload),
                                 action_elements=controls(actions, job) if actions else [])
    # Plotly stays on the web; Feishu gets native charts, exact facts and evidence IDs.
    stored = result.model_dump(mode="json", exclude={"results": {"__all__": {"chart"}}})
    stored["reply_card"] = card
    if actions:
        from app.semantic.catalog import load_catalog

        stored["card_actions"] = actions
        stored["card_action_catalog"] = load_catalog()["version"]
    return stored, text


def _number_needs_words(job, store, previous, key):
    payload = {
        **(next_round(previous) if not conversation_closed(previous) else {
            "conversation_round": previous["conversation_round"],
        }),
        "kind": "semantic_notice", "guided": True, "snapshot_key": key,
        "status": "needs_input", "number_reply_allowed": False,
        "notice": (
            "上一段对话已结束，旧编号不再有效。请重新发送完整问题。"
            if conversation_closed(previous)
            else "无法确定你选的是哪轮建议，请用文字说明。"
        ),
    }
    if previous:
        for field in ("semantic_context", "dialogue_turn", "product_scope_note"):
            if previous.get(field) is not None:
                payload[field] = previous[field]
    _save(store, job, payload)
    return _render_guidance(payload)


def _answer_guided(job, store, report, planner, original, previous, key, conversation):
    from app.analysis.dialogue import converse

    question = original["question"].strip()
    body = ConversationRequest(question=question)
    answered_round = None
    if len(question) == 1 and question in "123456789":
        candidate = store.latest_numbered_choice_context(job)
        if (
            not candidate or candidate.get("snapshot_key") != key
            or conversation_closed(candidate)
        ):
            return _number_needs_words(job, store, previous, key)
        pending = DialogueTurn.model_validate(candidate["dialogue_turn"])
        choices = _numbered_choices(pending)
        index = int(question) - 1
        if index >= len(choices):
            return _number_needs_words(job, store, previous, key)
        conversation = SemanticContext.model_validate(candidate["semantic_context"])
        previous = candidate
        answered_round = conversation.dialogue_id
        body = ConversationRequest(choice_id=choices[index].id)
    body = body.model_copy(update={"request_id": "job-" + str(job["job_id"])})
    owner = json.dumps([
        job.get("app_id") or getattr(store, "app_id", ""),
        job.get("tenant_key") or getattr(store, "tenant_key", ""),
        job["chat_id"], job["user_open_id"],
    ])
    outcome = asyncio.run(converse(
        body, report, planner, date.fromisoformat(original["today"]), previous=conversation,
        channel="feishu", owner=owner,
    ))
    turn = outcome.turn
    common = {
        **next_round(previous),
        "snapshot_key": key,
        "semantic_context": outcome.context.model_dump(mode="json"),
        "product_scope_note": outcome.context.product_scope_note,
    }
    if answered_round:
        # Audit the exact validated round selected; the next decision and its
        # choices are durable before delivery and survive worker restarts.
        common["answered_dialogue_id"] = answered_round
    if turn.status != "result":
        payload = {
            **common, "kind": "semantic_notice", "guided": True,
            "status": turn.status, "dialogue_turn": turn.model_dump(mode="json"),
        }
        # The complete decision precedes delivery and survives worker recovery.
        _save(store, job, payload)
        return _render_guidance(payload)
    result = turn.result
    _save(store, job, {
        **common, "kind": "analysis", "plan": result.plan.model_dump(mode="json"),
        "interpretation": result.interpretation,
    })
    return _store_result(result, job["payload"], job)


def answer_analysis(job, store, report_loader, planner):
    payload = job["payload"]
    if payload["kind"] == "card_action":
        from app.integrations.feishu_card_actions import action_payload

        _save(store, job, action_payload(job, store, report_loader()))
        payload = job["payload"]
    if payload["kind"] == "semantic_notice":
        # Safe recovery after persisting the decision but before persisting its reply.
        if payload.get("guided") or payload.get("dialogue_turn"):
            return _render_guidance(payload)
        return {"unavailable": True, "semantic_status": payload["status"]}, (
            payload["notice"] + ("\n\n" + round_notice(payload)
                                 if "conversation_round" in payload else "")
        )
    if payload["kind"] == "analysis_planning":
        raise QueryUnavailable("上次自然语言解析被中断，为避免重复调用，请重新提问。")
    if payload["kind"] == "analysis_question":
        # This durable marker also removes question text before any model request.
        _save(store, job, {"kind": "analysis_planning"})
        report = report_loader()
        key = snapshot_key(report)
        previous = store.latest_analysis_context(job)
        if previous and previous.get("snapshot_key") != key:
            previous = None
        if conversation_closed(previous):
            question = payload["question"].strip()
            if len(question) == 1 and question in "123456789":
                return _number_needs_words(job, store, previous, key)
            previous = None
        body = AnalysisQuestion(
            question=payload["question"], previous_plan=previous.get("plan") if previous else None
        )
        conversation = (
            SemanticContext.model_validate(previous["semantic_context"])
            if previous and previous.get("semantic_context")
            else None
        )
        if planner is not None and hasattr(planner, "interpret"):
            return _answer_guided(
                job, store, report, planner, payload, previous, key, conversation,
            )
        if planner is None or not planner.enabled:
            raise QueryUnavailable("自然语言模型暂不可用；发送“帮助”查看可用的固定日期查询示例。")
        resolution = asyncio.run(
            resolve_question(
                body,
                report,
                planner,
                date.fromisoformat(payload["today"]),
                conversation=conversation,
            )
        )
        if resolution.plan is None:
            _save(
                store,
                job,
                {
                    **next_round(previous),
                    "kind": "semantic_notice",
                    "snapshot_key": key,
                    "semantic_context": resolution.semantic.context.model_dump(mode="json"),
                    "status": resolution.semantic.status,
                    "notice": resolution.semantic.message,
                    "product_scope_note": resolution.semantic.context.product_scope_note,
                },
            )
            return {
                "unavailable": True,
                "semantic_status": resolution.semantic.status,
            }, resolution.semantic.message + "\n\n" + round_notice(job["payload"])
        plan, interpretation = resolution.plan, resolution.interpretation
        product_scope_note = "药品" in compact_question(body.question) or bool(
            previous and previous.get("product_scope_note")
        )
        if resolution.semantic is not None:
            product_scope_note = resolution.semantic.context.product_scope_note
        if product_scope_note and not any("未按药品类别筛选" in line for line in interpretation):
            interpretation.append(
                "沿用当前商品范围，未按药品类别筛选，可能包含器械、保健品等其他商品。"
            )
        payload = {
            **next_round(previous),
            "kind": "analysis",
            "plan": plan.model_dump(mode="json"),
            "interpretation": interpretation,
            "snapshot_key": key,
            "product_scope_note": product_scope_note,
        }
        if resolution.semantic is not None:
            payload["semantic_context"] = resolution.semantic.context.model_dump(mode="json")
        _save(store, job, payload)
    else:
        report = report_loader()
        key = snapshot_key(report)
        if payload.get("snapshot_key", key) != key:
            raise QueryUnavailable("数据快照已更新，请重新提问以使用新的数据范围。")
        payload = dict(payload, snapshot_key=key)
        if "conversation_round" not in payload:
            previous = store.latest_analysis_context(job)
            if previous and previous.get("snapshot_key") != key:
                previous = None
            payload.update(next_round(previous))
        _save(store, job, payload)
    result = execute_analysis(report, AnalysisPlan.model_validate(payload["plan"]))
    result = result.model_copy(update={"interpretation": payload["interpretation"]})
    return _store_result(result, payload, job)
