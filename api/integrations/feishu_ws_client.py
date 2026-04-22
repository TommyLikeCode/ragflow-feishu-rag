import asyncio
import importlib
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


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
        except Exception as exc:
            raise RuntimeError(
                "Failed to initialize Feishu WS SDK client. "
                "Please verify the installed `lark-oapi` version matches this adapter."
            ) from exc

        start_callable = getattr(self._ws_client, "start", None)
        if start_callable is None:
            raise RuntimeError("Feishu WS SDK client does not expose a start() method.")

        result = start_callable()
        if asyncio.iscoroutine(result):
            await result
            return

        # Some SDK versions block inside start(); keep the sidecar alive otherwise.
        await self._stopped.wait()

    async def stop(self) -> None:
        close_callable = getattr(self._ws_client, "stop", None) or getattr(
            self._ws_client, "close", None
        )
        if close_callable is not None:
            result = close_callable()
            if asyncio.iscoroutine(result):
                await result
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
        request = self._build_send_text_request(sdk, receive_id, text, receive_id_type)
        return await self._execute_send_text(sdk, request)

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
            app_id=self.config.app_id,
            app_secret=self.config.app_secret,
            app_token=self.config.app_token,
            event_handler=event_handler,
            log_level=self.config.log_level,
        )

    def _build_event_handler(self, sdk):
        ws_module = getattr(sdk, "ws", None)
        handler_cls = getattr(ws_module, "EventDispatcherHandler", None)
        if handler_cls is None:
            # Older/newer SDKs may accept a plain callback object.
            return self._dispatch_event

        builder = handler_cls.builder("", "")
        builder = builder.register_all_event(self._dispatch_event)
        return builder.build()

    async def _dispatch_event(self, data: Any) -> None:
        if self._message_handler is None:
            return

        event_dict = self._normalize_event(data)
        await self._message_handler(event_dict)

    def _normalize_event(self, data: Any) -> dict[str, Any]:
        if isinstance(data, dict):
            return data
        if hasattr(data, "__dict__"):
            return dict(data.__dict__)
        return {"raw_event": data}

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
            '{"text": "%s"}' % text.replace("\\", "\\\\").replace('"', '\\"')
        ).build()

        return (
            request_cls.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )

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


async def build_client_from_env(
    logger: Optional[logging.Logger] = None,
) -> FeishuWSClient:
    return FeishuWSClient(FeishuWSConfig.from_env(), logger=logger)
