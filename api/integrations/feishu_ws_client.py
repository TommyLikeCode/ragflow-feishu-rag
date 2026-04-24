import asyncio
import importlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from api.integrations.feishu_metrics import (
    record_send_text_failure,
    record_send_text_success,
    set_ws_connected,
)


MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class FeishuWSConfig:
    app_id: str
    app_secret: str
    app_token: str = ""
    encrypt_key: str = ""
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "FeishuWSConfig":
        app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        app_token = os.environ.get("FEISHU_APP_TOKEN", "").strip()
        encrypt_key = os.environ.get("FEISHU_APP_ENCRYPT_KEY", "").strip()

        missing = []
        if not app_id:
            missing.append("FEISHU_APP_ID")
        if not app_secret:
            missing.append("FEISHU_APP_SECRET")
        if not app_token:
            missing.append("FEISHU_APP_TOKEN")

        if missing:
            raise RuntimeError(
                "Missing required Feishu WS environment variables: "
                + ", ".join(missing)
            )

        return cls(
            app_id=app_id,
            app_secret=app_secret,
            app_token=app_token,
            encrypt_key=encrypt_key,
            log_level=os.environ.get("FEISHU_WS_LOG_LEVEL", "INFO").strip() or "INFO",
        )


class FeishuWSClient:
    def __init__(
        self,
        config: FeishuWSConfig,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)
        self._message_handler: Optional[MessageHandler] = None
        self._sdk_module = None
        self._ws_client = None
        self._stopped = asyncio.Event()

    def register_message_handler(self, handler: MessageHandler) -> None:
        self._message_handler = handler

    async def start(self) -> None:
        if self._message_handler is None:
            raise RuntimeError("FeishuWSClient requires a registered message handler before start().")

        sdk = self._load_sdk()
        self.logger.info("starting Feishu WS sidecar client")

        try:
            self._ws_client = self._build_ws_client(sdk)
            set_ws_connected(True)
            start_callable = getattr(self._ws_client, "start", None)
            if start_callable is None:
                raise RuntimeError("Feishu WS SDK client does not expose a start() method.")

            result = start_callable()
            if asyncio.iscoroutine(result):
                await result
                return

            # Some SDK versions block inside start(); keep the sidecar alive otherwise.
            await self._stopped.wait()
        except Exception as exc:
            set_ws_connected(False)
            raise RuntimeError(
                "Failed to initialize Feishu WS SDK client. "
                "Please verify the installed `lark-oapi` version matches this adapter."
            ) from exc

    async def stop(self) -> None:
        close_callable = getattr(self._ws_client, "stop", None) or getattr(
            self._ws_client, "close", None
        )
        if close_callable is not None:
            result = close_callable()
            if asyncio.iscoroutine(result):
                await result
        set_ws_connected(False)
        self._stopped.set()

    async def send_text(
        self,
        receive_id: str,
        text: str,
        receive_id_type: str = "open_id",
    ) -> Any:
        if not receive_id:
            raise RuntimeError("send_text() requires a non-empty receive_id.")
        if not text:
            raise RuntimeError("send_text() requires a non-empty text payload.")

        sdk = self._load_sdk()
        self.logger.info(
            "Feishu send_text request receive_id=%s receive_id_type=%s text_len=%s",
            receive_id,
            receive_id_type,
            len(text),
        )
        request = self._build_send_text_request(sdk, receive_id, text, receive_id_type)
        try:
            result = await self._execute_send_text(sdk, request)
        except Exception:
            self.logger.exception(
                "Feishu send_text exception receive_id=%s receive_id_type=%s",
                receive_id,
                receive_id_type,
            )
            raise

        if hasattr(result, "success") and callable(result.success):
            if result.success():
                record_send_text_success()
                self.logger.info(
                    "Feishu send_text success receive_id=%s receive_id_type=%s",
                    receive_id,
                    receive_id_type,
                )
            else:
                record_send_text_failure(getattr(result, "msg", None) or getattr(result, "code", None))
                self.logger.error(
                    "Feishu send_text failed receive_id=%s receive_id_type=%s code=%s msg=%s log_id=%s",
                    receive_id,
                    receive_id_type,
                    getattr(result, "code", None),
                    getattr(result, "msg", None),
                    result.get_log_id() if hasattr(result, "get_log_id") else None,
                )
        else:
            record_send_text_success()
        return result

    async def add_message_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> Any:
        if not message_id:
            raise RuntimeError("add_message_reaction() requires a non-empty message_id.")

        sdk = self._load_sdk()
        self.logger.info(
            "Feishu add_reaction request message_id=%s emoji_type=%s",
            message_id,
            emoji_type,
        )
        request = self._build_add_message_reaction_request(sdk, message_id, emoji_type)
        try:
            result = await self._execute_add_message_reaction(sdk, request)
        except Exception:
            self.logger.exception(
                "Feishu add_reaction exception message_id=%s emoji_type=%s",
                message_id,
                emoji_type,
            )
            raise

        if hasattr(result, "success") and callable(result.success):
            if result.success():
                self.logger.info(
                    "Feishu add_reaction success message_id=%s emoji_type=%s",
                    message_id,
                    emoji_type,
                )
            else:
                self.logger.error(
                    "Feishu add_reaction failed message_id=%s emoji_type=%s code=%s msg=%s log_id=%s",
                    message_id,
                    emoji_type,
                    getattr(result, "code", None),
                    getattr(result, "msg", None),
                    result.get_log_id() if hasattr(result, "get_log_id") else None,
                )
        return result

    def _load_sdk(self):
        if self._sdk_module is not None:
            return self._sdk_module

        try:
            self._sdk_module = importlib.import_module("lark_oapi")
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Feishu WS adapter requires the `lark-oapi` package. "
                "Install it before running `scripts/run_feishu_ws.py`."
            ) from exc
        return self._sdk_module

    def _build_ws_client(self, sdk):
        ws_module = getattr(sdk, "ws", None)
        if ws_module is None:
            raise RuntimeError("`lark_oapi.ws` is unavailable.")

        event_handler = self._build_event_handler(sdk)
        client_cls = getattr(ws_module, "Client", None)
        if client_cls is None:
            raise RuntimeError("`lark_oapi.ws.Client` is unavailable.")

        return client_cls(
            self.config.app_id,
            self.config.app_secret,
            event_handler=event_handler
        )

    def _build_event_handler(self, sdk):
        ws_module = getattr(sdk, "ws", None)
        handler_cls = getattr(sdk, "EventDispatcherHandler", None)
        if handler_cls is None:
            # Older/newer SDKs may accept a plain callback object.
            return self._dispatch_event

        builder = handler_cls.builder(self.config.encrypt_key, self.config.app_token)
        if hasattr(builder, "register_all_event"):
            builder = builder.register_all_event(self._dispatch_event)
        elif hasattr(builder, "register_p2_im_message_receive_v1"):
            # Newer SDKs remove register_all_event; register IM message event explicitly.
            # Need to wrap async _dispatch_event in a sync adapter for SDK callback
            builder = builder.register_p2_im_message_receive_v1(self._sync_event_adapter)
        else:
            raise RuntimeError(
                "No compatible event registration API found on Feishu EventDispatcherHandlerBuilder."
            )
        return builder.build()

    def _sync_event_adapter(self, feishu_event: Any) -> None:
        """Synchronous adapter to convert Feishu SDK event to async dispatch."""
        self.logger.info("Feishu callback invoked")
        try:
            event_dict = self._normalize_event(feishu_event)
        except Exception:
            self.logger.exception("Failed to normalize Feishu event")
            return
        try:
            # Schedule async handler as a task in current event loop
            loop = asyncio.get_event_loop()
            self.logger.info("Received Feishu event via adapter, scheduling async dispatch")
            task = loop.create_task(self._dispatch_event(event_dict))
            task.add_done_callback(self._log_dispatch_exception)
        except RuntimeError:
            # No event loop in current thread; try get_running_loop
            try:
                loop = asyncio.get_running_loop()
                self.logger.info("Received Feishu event via adapter (running loop), scheduling async dispatch")
                task = loop.create_task(self._dispatch_event(event_dict))
                task.add_done_callback(self._log_dispatch_exception)
            except RuntimeError:
                self.logger.error("No event loop available for async event handling")

    def _log_dispatch_exception(self, task: asyncio.Task) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            self.logger.exception("Feishu event dispatch task failed", exc_info=exc)

    async def _dispatch_event(self, data: Any) -> None:
        if self._message_handler is None:
            return

        event_dict = self._normalize_event(data)
        self.logger.info("Dispatching event to message handler")
        await self._message_handler(event_dict)

    def _normalize_event(self, data: Any) -> dict[str, Any]:
        normalized = self._normalize_value(data)
        if isinstance(normalized, dict):
            return normalized
        return {"raw_event": normalized}

    def _normalize_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self._normalize_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._normalize_value(v) for v in value]
        if hasattr(value, "__dict__"):
            return {
                k: self._normalize_value(v)
                for k, v in vars(value).items()
                if not k.startswith("_")
            }
        return value

    def _build_send_text_request(self, sdk, receive_id: str, text: str, receive_id_type: str):
        api_module = getattr(sdk, "api", None)
        if api_module is None:
            raise RuntimeError("`lark_oapi.api` is unavailable.")

        im_module = getattr(api_module, "im", None)
        v1_module = getattr(im_module, "v1", None) if im_module else None
        if v1_module is None:
            raise RuntimeError("`lark_oapi.api.im.v1` is unavailable.")

        request_cls = getattr(v1_module, "CreateMessageRequest", None)
        body_cls = getattr(v1_module, "CreateMessageRequestBody", None)
        if request_cls is None or body_cls is None:
            raise RuntimeError("Feishu send message request classes are unavailable.")

        body = body_cls.builder().receive_id(receive_id).msg_type("text").content(
            json.dumps({"text": text}, ensure_ascii=False)
        ).build()

        return (
            request_cls.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )

    def _build_add_message_reaction_request(self, sdk, message_id: str, emoji_type: str):
        api_module = getattr(sdk, "api", None)
        if api_module is None:
            raise RuntimeError("`lark_oapi.api` is unavailable.")

        im_module = getattr(api_module, "im", None)
        v1_module = getattr(im_module, "v1", None) if im_module else None
        if v1_module is None:
            raise RuntimeError("`lark_oapi.api.im.v1` is unavailable.")

        request_cls = getattr(v1_module, "CreateMessageReactionRequest", None)
        body_cls = getattr(v1_module, "CreateMessageReactionRequestBody", None)
        emoji_cls = getattr(v1_module, "Emoji", None)
        if request_cls is None or body_cls is None or emoji_cls is None:
            raise RuntimeError("Feishu message reaction request classes are unavailable.")

        emoji = emoji_cls.builder().emoji_type(emoji_type).build()
        body = body_cls.builder().reaction_type(emoji).build()
        return request_cls.builder().message_id(message_id).request_body(body).build()

    async def _execute_send_text(self, sdk, request):
        client_builder = getattr(sdk, "Client", None)
        if client_builder is None:
            raise RuntimeError("`lark_oapi.Client` is unavailable.")

        client = (
            client_builder.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .build()
        )
        create_message = client.im.v1.message.create
        result = create_message(request)
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def _execute_add_message_reaction(self, sdk, request):
        client_builder = getattr(sdk, "Client", None)
        if client_builder is None:
            raise RuntimeError("`lark_oapi.Client` is unavailable.")

        client = (
            client_builder.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .build()
        )
        create_reaction = client.im.v1.message_reaction.create
        result = create_reaction(request)
        if asyncio.iscoroutine(result):
            return await result
        return result


async def build_client_from_env(
    logger: Optional[logging.Logger] = None,
) -> FeishuWSClient:
    return FeishuWSClient(FeishuWSConfig.from_env(), logger=logger)
