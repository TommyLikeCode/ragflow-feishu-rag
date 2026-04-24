from __future__ import annotations

import logging
from typing import Any

from api.integrations.feishu_message_context import FeishuMessageContext


def _context_to_legacy_parsed(ctx: FeishuMessageContext) -> dict[str, Any]:
    return {
        "message_id": ctx.message_id,
        "open_id": ctx.open_id or ctx.user_id,
        "chat_type": ctx.chat_type,
        "message_type": ctx.message_type,
        "text": ctx.text,
        "image_key": ctx.image_key,
        "file_key": ctx.file_key,
        "raw_event": ctx.to_task_payload(),
    }


async def handle_feishu_message(ctx: FeishuMessageContext, bridge, logger: logging.Logger | None = None) -> dict[str, Any]:
    logger = logger or getattr(bridge, "logger", logging.getLogger(__name__))

    if not ctx.is_text():
        logger.info(
            "Feishu worker ignored message reason=non-text context=%s",
            ctx.to_log_dict(),
        )
        return {"status": "ignored", "reason": "non-text", "context": ctx.to_task_payload()}

    parsed = _context_to_legacy_parsed(ctx)
    logger.info(
        "enter handle_text_message message_id=%s message_type=%s",
        ctx.message_id,
        ctx.message_type,
    )

    try:
        await bridge._add_debug_reaction(parsed)
        return await bridge.handle_text_message(parsed)
    except Exception:
        logger.exception(
            "Feishu worker failed message_id=%s message_type=%s",
            ctx.message_id,
            ctx.message_type,
        )
        raise