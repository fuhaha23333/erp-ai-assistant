"""Explicit connection setup for a Feishu application bot."""

import argparse
import logging
import threading
from pathlib import Path

from app.core.environment import configured_path, environment, project_path
from app.integrations.feishu_app import (
    DEFAULT_STATE,
    AppConfigError,
    ConnectionBot,
    load_config,
    make_client,
    probe_bot,
    reply_card,
    reply_text,
)
from app.notifications.feishu import DeliveryError, delivery_lock


class SafeSdkLog(logging.Handler):
    """Only emit known status text, never SDK connection URLs, payloads or exceptions."""

    def emit(self, record):
        message = record.getMessage()
        if "disconnected to" in message:
            print("飞书长连接已断开，等待自动重连。", flush=True)
        elif "connected to" in message:
            print("飞书长连接已建立，可在后台保存长连接订阅。", flush=True)
        elif record.levelno >= logging.WARNING:
            print("飞书连接出现异常，请检查网络与应用配置。", flush=True)


def main(argv=None) -> int:
    values = environment()
    state_dir = configured_path("ERP_FEISHU_STATE_DIR", DEFAULT_STATE)
    parser = argparse.ArgumentParser(description="飞书应用机器人接入；默认只检查本地配置")
    parser.add_argument("--config", type=Path, help="显式兼容旧JSON；默认只读取.env")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="离线检查，不连接飞书")
    mode.add_argument("--probe", action="store_true", help="验证凭据及机器人身份，不发消息")
    mode.add_argument("--discover", action="store_true", help="连接并记录群／用户 ID，不回复")
    mode.add_argument("--listen", action="store_true", help="接收授权群消息并回复连接测试")
    mode.add_argument(
        "--sales", action="store_true", help="授权群自然语言数据分析，使用独立助手数据库"
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=project_path(values["ERP_REPORT_PATH"])
        if values.get("ERP_REPORT_PATH")
        else (DEFAULT_STATE.parent / "reports/platform-four-domain-20260922.json")
        if (DEFAULT_STATE.parent / "reports/platform-four-domain-20260922.json").is_file()
        else DEFAULT_STATE.parent / "reports/platform-operating-20260916.json",
    )
    args = parser.parse_args(argv)
    stop = threading.Event()
    worker = None
    try:
        config = load_config(args.config)
        print("本地应用凭据已填写（未输出凭据）。", flush=True)
        if not (args.probe or args.discover or args.listen or args.sales):
            try:
                config.require_authorized_groups()
                label = (
                    "授权群全员" if config.user_access_mode == "all_group_members" else "用户白名单"
                )
                print(f"租户与群授权配置已填写；当前为{label}模式，可执行 --probe 或 --listen。")
            except AppConfigError:
                print("尚未完成群／用户授权；下一步执行 --discover。")
            return 0

        import lark_oapi as lark
        from lark_oapi.core.log import logger

        logger.handlers = [SafeSdkLog()]
        logger.propagate = False
        client = make_client(config)
        bot_id = probe_bot(client)
        print("应用凭据验证通过，机器人身份已读取。", flush=True)
        if args.probe:
            return 0
        store = None
        if args.sales:
            from app.ai.model_client import ModelSettings, ModelUnavailable, load_model_settings
            from app.core.reports import load_report
            from app.integrations.feishu_jobs import SqlJobStore
            from app.integrations.feishu_sales import SalesBot
            from app.semantic.provider import SemanticPlanner

            config.require_authorized_groups()
            snapshot = load_report(args.report_path)
            if snapshot.operating is None or not snapshot.metadata.scope.all_buyers:
                raise AppConfigError("销售模式需要包含有效销售规则的全平台快照")
            store = SqlJobStore(config.app_id, config.tenant_key)
            store.check()
            interpreter = SemanticPlanner(ModelSettings(enabled=False))
            try:
                settings = load_model_settings()
                interpreter = SemanticPlanner(settings)
                if settings.enabled:
                    from app.analysis.operations import DOMAIN_LABELS, executable_domains

                    print(
                        "四域语义理解已启用；当前快照可执行："
                        + "、".join(DOMAIN_LABELS[d] for d in executable_domains(snapshot))
                        + "。",
                        flush=True,
                    )
                else:
                    print("模型未配置，使用固定指令查询。", flush=True)
            except ModelUnavailable as exc:
                print(str(exc), flush=True)
            bot = SalesBot(
                lambda: load_config(args.config),
                bot_id,
                store,
                lambda: load_report(args.report_path),
                analysis_planner=interpreter,
            )
            print("销售问数模式已就绪，任务保存在独立助手数据库。", flush=True)
        else:
            bot = ConnectionBot(config, bot_id, state_dir, discover=args.discover)

        def receive(data):
            result = bot.accept(data)
            if result == "discovered":
                print(f"已发现群和用户标识：{state_dir / 'discovery.json'}（未回复）", flush=True)
            elif result == "queued":
                print("授权消息已入队。", flush=True)
            elif result == "busy":
                print("已有查询处理中，等待提示已入队。", flush=True)

        def process():
            while not stop.wait(0.5):
                try:
                    options = (
                        {"card_sender": lambda mid, card, key: reply_card(client, mid, card, key)}
                        if args.sales
                        else {}
                    )
                    count = bot.process_pending(
                        lambda message_id, text, key: reply_text(client, message_id, text, key),
                        **options,
                    )
                    if count:
                        print("机器人回复已发送。", flush=True)
                except Exception:
                    print("机器人任务暂未完成，请检查助手库、本地配置和状态。", flush=True)

        handler_builder = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(receive)
        )
        if args.sales:
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )

            def card_action(data):
                try:
                    status = bot.accept_card_action(data)
                except Exception:
                    status = "failed"
                message = {
                    "queued": "已收到，分析完成后会发送新的结果卡。",
                    "duplicate": "这项操作已经提交，请查看最近的结果。",
                    "busy": "已有查询处理中，请等待结果。",
                }.get(status, "操作未受理，请重新 @机器人提问。")
                return P2CardActionTriggerResponse({"toast": {
                    "type": "info" if status in {"queued", "duplicate"} else "warning",
                    "content": message,
                }})

            handler_builder.register_p2_card_action_trigger(card_action)
        handler = handler_builder.build()
        # One connection process per app/local state directory; no competing consumers.
        with delivery_lock(state_dir / "connection.lock"):
            if store is not None:
                store.recover()
            if args.listen or args.sales:
                worker = threading.Thread(target=process, daemon=True)
                worker.start()
            print("正在建立长连接；按 Ctrl+C 停止。", flush=True)
            lark.ws.Client(
                config.app_id,
                config.app_secret.get_secret_value(),
                event_handler=handler,
                log_level=lark.LogLevel.INFO,
            ).start()
        return 0
    except (AppConfigError, DeliveryError) as exc:
        print(str(exc), flush=True)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("连接未完成，请检查网络、应用设置及本地配置；没有输出原始异常或凭据。")
        return 1
    finally:
        stop.set()
        if worker is not None:
            worker.join(timeout=20)


if __name__ == "__main__":
    raise SystemExit(main())
