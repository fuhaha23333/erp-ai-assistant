"""Connection-only Feishu application bot. No ERP access or model calls."""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from app.core.environment import environment
from app.notifications.feishu import LOCAL_DIR, delivery_lock, write_state

DEFAULT_CONFIG = LOCAL_DIR / "feishu-app.json"
DEFAULT_STATE = LOCAL_DIR / "feishu-app"


class AppConfigError(ValueError):
    pass


class ReplyRejected(AppConfigError):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"飞书拒绝回复请求，错误码 {code}")


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    app_id: str = ""
    app_secret: SecretStr = SecretStr("")
    tenant_key: str = ""
    allowed_chat_ids: tuple[str, ...] = ()
    allowed_user_open_ids: tuple[str, ...] = ()
    user_access_mode: Literal["allowlist", "all_group_members"] = "allowlist"

    def require_credentials(self):
        if not re.fullmatch(r"cli_[A-Za-z0-9_]+", self.app_id):
            raise AppConfigError("请在本地配置填写正确的 app_id（以 cli_ 开头）")
        if not self.app_secret.get_secret_value().strip():
            raise AppConfigError("请在本地配置填写 app_secret")

    def require_authorized_groups(self):
        self.require_credentials()
        if not self.tenant_key or not self.allowed_chat_ids:
            raise AppConfigError("请先用 --discover 获取标识并配置租户与授权群")
        if self.user_access_mode == "allowlist" and not self.allowed_user_open_ids:
            raise AppConfigError("用户白名单模式需要配置 allowed_user_open_ids")
        if not all(value.startswith("oc_") for value in self.allowed_chat_ids):
            raise AppConfigError("allowed_chat_ids 应填写 oc_ 开头的群 ID")
        if not all(value.startswith("ou_") for value in self.allowed_user_open_ids):
            raise AppConfigError("allowed_user_open_ids 应填写 ou_ 开头的用户 open_id")


def load_config(path: Path | None = None) -> AppConfig:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path and path.is_file() else {}
        if not isinstance(data, dict):
            raise AppConfigError("应用配置应为 JSON 对象")
        values = environment()
        for key in ("app_id", "app_secret", "tenant_key", "allowed_chat_ids",
                    "allowed_user_open_ids", "user_access_mode"):
            name = "FEISHU_" + key.upper()
            if name not in values:
                continue
            value = values[name].strip()
            data[key] = (
                [part.strip() for part in value.split(",") if part.strip()]
                if key in {"allowed_chat_ids", "allowed_user_open_ids"} else value
            )
        config = AppConfig.model_validate(data)
    except (OSError, ValueError, ValidationError):
        # Pydantic errors may include input values; never print them for credential files.
        raise AppConfigError("无法读取应用配置，请检查 .env 或旧 JSON 的格式和权限") from None
    if (path is None or not path.is_file()) and (
        not config.app_id or not config.app_secret.get_secret_value().strip()
    ):
        raise AppConfigError(
            "飞书配置文件不存在，且环境变量中的应用凭据不完整。"
            "请填写根目录 .env 的 FEISHU_* 配置（旧 JSON 自定义路径使用 --config），"
            "或按 docs/feishu-app-setup.md 配置；启动网页不会自动启动飞书机器人。"
        )
    config.require_credentials()
    return config


def make_client(config: AppConfig):
    import lark_oapi as lark

    return (lark.Client.builder().app_id(config.app_id)
            .app_secret(config.app_secret.get_secret_value()).timeout(15)
            .log_level(lark.LogLevel.WARNING).build())


def probe_bot(client) -> str:
    from lark_oapi.core.enum import AccessTokenType, HttpMethod
    from lark_oapi.core.model import BaseRequest

    request = (BaseRequest.builder().http_method(HttpMethod.GET)
               .uri("/open-apis/bot/v3/info/").token_types({AccessTokenType.TENANT}).build())
    try:
        response = client.request(request)
        if type(response.code) is not int or response.code != 0:
            raise AppConfigError("应用验证失败，请核对 App ID、App Secret 和机器人能力")
        payload = json.loads(response.raw.content)
        bot_id = payload["bot"]["open_id"]
        if not isinstance(bot_id, str) or not bot_id.startswith("ou_"):
            raise ValueError("missing identity")
        return bot_id
    except AppConfigError:
        raise
    except Exception:
        raise AppConfigError("无法验证机器人身份，请检查网络、应用凭据及机器人能力") from None


def reply_text(client, message_id: str, text: str, request_id: str) -> str:
    return _reply(client, message_id, 'text', {'text': text}, request_id)


def reply_card(client, message_id: str, card: dict, request_id: str) -> str:
    return _reply(client, message_id, 'interactive', card, request_id)


def _reply(client, message_id: str, msg_type: str, content: dict, request_id: str) -> str:
    from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

    request = (ReplyMessageRequest.builder().message_id(message_id)
               .request_body(ReplyMessageRequestBody.builder().msg_type(msg_type)
                             .content(json.dumps(content, ensure_ascii=False))
                             .uuid(request_id).build()).build())
    response = client.im.v1.message.reply(request)
    if type(response.code) is int and response.code != 0:
        raise ReplyRejected(response.code)
    if type(response.code) is not int:
        raise AppConfigError("回复未获成功确认，请核实群内消息和发消息权限")
    if not getattr(getattr(response, "data", None), "message_id", None):
        raise AppConfigError("回复缺少消息回执，请在群内核实")
    return response.data.message_id


def extract_group_message(data, app_id: str, bot_id: str):
    """Validate SDK event identity and strip only this bot's actual mentions."""
    try:
        header, sender, message = data.header, data.event.sender, data.event.message
        if (header.app_id != app_id
                or header.event_type != "im.message.receive_v1"
                or sender.sender_type != "user" or sender.sender_id.open_id == bot_id
                or message.chat_type != "group" or message.message_type != "text"):
            return None
        mentions = [item for item in (message.mentions or [])
                    if item.id and item.id.open_id == bot_id]
        if not mentions:
            return None
        item = {"app_id": header.app_id, "tenant_key": header.tenant_key,
                "chat_id": message.chat_id, "user_open_id": sender.sender_id.open_id,
                "message_id": message.message_id, "event_id": header.event_id}
        if not all(isinstance(value, str) and 0 < len(value) <= 200 for value in item.values()):
            return None
        if sender.tenant_key != header.tenant_key:
            return None
        text = json.loads(message.content)["text"]
        if not isinstance(text, str) or len(text) > 8000:
            return None
        for mention in mentions:
            if mention.key:
                text = text.replace(mention.key, "")
        return item, text.strip()
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def authorized_identity(config: AppConfig, item: dict) -> bool:
    user = item.get("user_open_id")
    return (item.get("app_id") == config.app_id
            and item.get("tenant_key") == config.tenant_key
            and item.get("chat_id") in config.allowed_chat_ids
            and isinstance(user, str) and user.startswith("ou_") and len(user) > 3
            and (config.user_access_mode == "all_group_members"
                 or user in config.allowed_user_open_ids))


class ConnectionBot:
    def __init__(self, config: AppConfig, bot_id: str, state_dir: Path = DEFAULT_STATE,
                 *, discover: bool = False):
        self.config = config
        self.bot_id = bot_id
        self.state_dir = state_dir
        self.discover = discover
        if not discover:
            config.require_authorized_groups()

    def authorized(self, item: dict) -> bool:
        return authorized_identity(self.config, item)

    def accept(self, data) -> str:
        extracted = extract_group_message(data, self.config.app_id, self.bot_id)
        if extracted is None:
            return "ignored"
        item, text = extracted
        if not self.discover and not self.authorized(item):
            return "ignored"

        if self.discover:
            path = self.state_dir / "discovery.json"
            with delivery_lock(self.state_dir / "discovery.lock"):
                rows = json.loads(path.read_bytes()) if path.exists() else []
                identity = {key: item[key] for key in
                            ("app_id", "tenant_key", "chat_id", "user_open_id")}
                if identity not in rows:
                    write_state(path, (rows + [identity])[-20:])
            return "discovered"

        key = hashlib.sha256(f"{item['app_id']}|{item['tenant_key']}|"
                             f"{item['message_id']}".encode()).hexdigest()
        path = self.state_dir / "inbox" / f"{key}.json"
        with delivery_lock(path.with_suffix(".lock")):
            if path.exists():
                return "duplicate"
            item.update(status="queued", command="ping" if text.strip().lower() in
                        {"连接测试", "ping"} else "help", request_id=key[:32],
                        received_at=datetime.now(timezone.utc).isoformat())
            write_state(path, item)
        return "queued"

    def process_pending(self, sender) -> int:
        if self.discover:
            return 0
        count = 0
        for path in sorted((self.state_dir / "inbox").glob("*.json")):
            with delivery_lock(path.with_suffix(".lock")):
                item = json.loads(path.read_bytes())
                if item.get("status") != "queued":
                    continue
                if not self.authorized(item):
                    write_state(path, {**item, "status": "revoked"})
                    continue
                text = ("机器人连接正常：已收到群内 @消息，并成功调用原消息回复接口。\n"
                        "当前处于连接测试阶段，尚未启用销售问数。") \
                    if item["command"] == "ping" else (
                        "ERP 经营助手已接入。请 @我 发送“连接测试”验证收发链路。\n"
                        "当前仅支持连接测试；商品排行与自然语言问数将在下一阶段接入。")
                write_state(path, {**item, "status": "sending"})
                try:
                    sender(item["message_id"], text, item["request_id"])
                except Exception:
                    write_state(path, {**item, "status": "unknown"})
                    continue
                write_state(path, {**item, "status": "sent"})
                count += 1
        return count
