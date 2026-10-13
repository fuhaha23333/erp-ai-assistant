"""Authorized group queries: finite interpretation, durable analysis, original-message reply."""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.ai.question_bounds import question_bounds, validate_grounding
from app.ai.sales_intent import (
    UnrecognizedQuestion,
    parse_local_intent,
    parse_model_decision,
)
from app.analysis.sales_insights import sales_summary, sales_trend
from app.analysis.sales_query import (
    QueryRequest,
    QueryUnavailable,
    product_ranking,
    render_ranking,
)
from app.integrations.feishu_app import (
    ReplyRejected,
    authorized_identity,
    extract_group_message,
)
from app.integrations.feishu_cards import information_card, ranking_card
from app.integrations.feishu_insight_cards import insight_card, insight_text

HELP = (
    "ERP 经营助手：支持销售概览、每日趋势和商品排行。请 @我 发送：\n"
    "2026-09-09 至 2026-09-15 销售概览\n"
    "2026-09-09 至 2026-09-15 每日销售趋势\n"
    "2026-09-09 至 2026-09-15 商品销售额排行 前10\n"
    "2026-09-09 至 2026-09-15 商品订单数排行 前10\n"
    "最近一周哪些商品卖得好？\n"
    "数据范围\n"
    "当前使用备份快照；相对日期按提问日期计算，缺少数据时会说明。\n"
    "暂不支持药品分类、数量排行、利润、条件筛选或连续追问。"
)

class SalesBot:
    def __init__(
        self,
        config_loader,
        bot_id,
        store,
        report_loader,
        *,
        model_client=None,
        analysis_planner=None,
    ):
        self.config_loader = config_loader
        self.bot_id = bot_id
        self.store = store
        self.report_loader = report_loader
        self.model_client = model_client
        self.analysis_planner = analysis_planner
        self.config_loader().require_authorized_groups()

    def accept(self, data):
        config = self.config_loader()
        extracted = extract_group_message(data, config.app_id, self.bot_id)
        if extracted is None:
            return "ignored"
        identity, text = extracted
        if not authorized_identity(config, identity):
            return "ignored"
        if text.lower() in {"ping", "连接测试"}:
            payload = {"kind": "ping"}
        elif text in {"帮助", "help", "菜单", ""}:
            payload = {"kind": "help"}
        elif text in {"数据范围", "可用日期"}:
            payload = {"kind": "coverage"}
        elif text in {"清除追问上下文", "重新开始"} and self.analysis_planner is not None:
            payload = {"kind": "clear_context"}
        else:
            try:
                raw_time = getattr(data.event.message, "create_time", None)
                when = (
                    datetime.fromtimestamp(int(raw_time) / 1000, timezone.utc)
                    if raw_time is not None
                    else datetime.now(timezone.utc)
                )
                today = when.astimezone(ZoneInfo("Asia/Shanghai")).date()
                if self.analysis_planner is not None:
                    from app.integrations.feishu_analytics import question_payload

                    payload = question_payload(text, today)
                else:
                    intent = parse_local_intent(text, today)
                    payload = self._intent_payload(intent)
            except UnrecognizedQuestion as exc:
                try:
                    question_bounds(text, today)
                    payload = (
                        {"kind": "interpret", "question": text, "today": today.isoformat()}
                        if self.model_client is not None
                        else {"kind": "notice", "notice": str(exc)}
                    )
                except QueryUnavailable as invalid:
                    payload = {"kind": "notice", "notice": str(invalid)}
            except QueryUnavailable as exc:
                payload = {"kind": "notice", "notice": str(exc)}
            except (ValueError, OverflowError, OSError):
                payload = {"kind": "notice", "notice": "消息日期无效，请用明确日期重新提问。"}
        # No source/model calls here. Commit before acknowledging the SDK event.
        return self.store.enqueue(identity, payload)

    def accept_card_action(self, data):
        from app.integrations.feishu_card_actions import accept

        return accept(self, data)

    @staticmethod
    def _intent_payload(intent):
        return {
            "kind": {
                "product_ranking": "ranking",
                "sales_summary": "summary",
                "sales_trend": "trend",
            }[intent.tool],
            "request": intent.to_query().model_dump(mode="json"),
        }

    def _interpret(self, job):
        original = job["payload"]
        # Persist the attempt and scrub the question before making a network call.
        # A restart with this marker must never automatically repeat an uncertain call.
        if not self.store.replace_payload(job, {"kind": "interpreting"}):
            raise QueryUnavailable("任务已被重新领取，请重新提问。")
        job["payload"] = {"kind": "interpreting"}
        if self.model_client is None:
            raise QueryUnavailable("自然语言模型未启用，请使用帮助中的固定指令。")
        today = date.fromisoformat(original["today"])
        bounds = question_bounds(original["question"], today)
        candidate = self.model_client.interpret(original["question"], today)
        intent = parse_model_decision(candidate)
        validate_grounding(original["question"], bounds, intent)
        payload = self._intent_payload(intent)
        payload["interpreter"] = "model"
        if not self.store.replace_payload(job, payload):
            raise QueryUnavailable("任务已被重新领取，请重新提问。")
        job["payload"] = payload

    def _answer(self, job):
        if job["payload"]["kind"] in {
            "analysis_question",
            "analysis_planning",
            "analysis",
            "semantic_notice",
            "card_action",
        }:
            from app.integrations.feishu_analytics import answer_analysis

            return answer_analysis(job, self.store, self.report_loader, self.analysis_planner)
        if job["payload"]["kind"] == "clear_context":
            return {}, (
                "已清除当前群中你的追问上下文。再次 @我，说说你想了解什么；"
                "可以先说大致目标，我会帮你逐步补齐。"
            )
        if job["payload"]["kind"] == "interpreting":
            raise QueryUnavailable("上次自然语言解析被中断，为避免重复请求，请重新提问。")
        if job["payload"]["kind"] == "interpret":
            self._interpret(job)
        payload = job["payload"]
        kind = payload["kind"]
        if kind == "ping":
            return {}, "机器人连接正常。发送“帮助”查看当前支持的分析类型和提问示例。"
        if kind == "help":
            from app.integrations.feishu_guidance import help_text

            return {}, (
                help_text(self.report_loader()) if self.analysis_planner is not None else HELP
            )
        if kind == "notice":
            return {}, payload["notice"] + "\n发送“帮助”查看支持的提问方式。"
        report = self.report_loader()
        if kind == "coverage":
            if self.analysis_planner is not None:
                from app.integrations.feishu_guidance import coverage_text

                return {}, coverage_text(report)
            from datetime import timedelta

            from app.schemas.sales import AnalysisWindow

            if report.operating is None:
                raise QueryUnavailable("快照不含有效销售规则，请更新经营快照。")
            tz = ZoneInfo(report.operating.policy.business_timezone)
            if report.metadata.source_as_of.tzinfo is None:
                raise QueryUnavailable("快照截至时间缺少时区，暂不能查询。")
            cutoff = report.metadata.source_as_of.astimezone(tz)
            end = min(report.metadata.window.end, cutoff.date())
            if end <= report.metadata.window.start:
                raise QueryUnavailable("当前快照没有可查询的完整日期。")
            window = AnalysisWindow(start=report.metadata.window.start, end=end)
            return {}, (
                f"当前完整日期范围：{window.start} 至 {window.end - timedelta(days=1)}\n"
                f"备份截至：{cutoff:%Y-%m-%d %H:%M:%S}（{tz.key}）\n"
                "当前为历史备份，未覆盖的日期不能查询，也不会自动替换成其他日期。\n"
                f"口径：{'、'.join(report.operating.policy.included_statuses)}；"
                "按订单创建日期；不是支付成交额，未扣退款。"
            )
        request = QueryRequest.model_validate(payload["request"])
        if kind in {"summary", "trend"}:
            result = (sales_summary if kind == "summary" else sales_trend)(report, request)
            stored = result.model_dump(mode="json")
            stored["reply_card"] = insight_card(result, job["job_id"][:16])
            return stored, insight_text(result)
        result = product_ranking(report, request)
        stored = result.model_dump(mode="json")
        stored["reply_card"] = ranking_card(result, job["job_id"][:16])
        return stored, render_ranking(result, job["job_id"][:16])

    def process_pending(self, sender, *, card_sender=None):
        config = self.config_loader()
        job = self.store.claim()
        if job is not None:
            if not authorized_identity(config, job):
                self.store.mark(job["job_id"], "revoked")
            else:
                try:
                    result, text = self._answer(job)
                except QueryUnavailable as exc:
                    result, text = {"unavailable": True}, str(exc)
                except Exception:
                    result, text = {"failed": True}, "查询暂未完成，请稍后重试或联系管理员。"
                if "reply_card" not in result:
                    warning = bool(
                        result.get("unavailable")
                        or result.get("failed")
                        or job["payload"]["kind"] == "notice"
                    )
                    title = "查询提示" if warning else "ERP 经营助手"
                    result["reply_card"] = information_card(title, text, warning=warning)
                self.store.finish_analysis(job, result, text)
        # Configuration may be temporarily invalid while the operator saves it.
        # Read it before claiming delivery so unsent ready jobs remain recoverable.
        delivery_config = self.config_loader()
        outgoing = self.store.next_reply()
        if outgoing is None:
            return 0
        if not authorized_identity(delivery_config, outgoing):
            self.store.mark(outgoing["job_id"], "revoked")
            return 0
        try:
            card = outgoing.get("result", {}).get("reply_card")
            reply_to = outgoing["payload"].get("reply_to_message_id", outgoing["message_id"])
            if card is not None and card_sender is not None:
                receipt = card_sender(reply_to, card, outgoing["request_id"])
            else:
                receipt = sender(reply_to, outgoing["reply_text"], outgoing["request_id"])
        except ReplyRejected as exc:
            self.store.mark(outgoing["job_id"], "rejected", error_code=exc.code)
        except Exception:
            self.store.mark(outgoing["job_id"], "unknown")
        else:
            self.store.mark(outgoing["job_id"], "sent", **(
                {"reply_message_id": receipt} if isinstance(receipt, str) else {}
            ))
            return 1
        return 0
