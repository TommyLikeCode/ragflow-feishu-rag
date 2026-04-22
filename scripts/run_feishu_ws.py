import asyncio
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("FEISHU_WS_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("feishu_ws_sidecar")
    try:
        from api.apps import app
        from api.integrations.feishu_ws_bridge import FeishuWSBridge
        from api.integrations.feishu_ws_client import build_client_from_env
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Failed to import Feishu WS sidecar dependencies. "
            "Make sure backend runtime dependencies are installed before startup."
        ) from exc

    async with app.app_context():
        client = await build_client_from_env(logger=logger)
        bridge = FeishuWSBridge(
            client=client,
            default_dialog_id=os.environ.get("FEISHU_DEFAULT_DIALOG_ID", "").strip(),
            logger=logger,
        )
        client.register_message_handler(bridge.handle_event)
        logger.info("Feishu WS sidecar initialized")
        await client.start()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
