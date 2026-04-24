from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from api.integrations.feishu_message_context import FeishuMessageContext
from api.integrations.feishu_metrics import (
    record_message_enqueued,
    record_worker_processing_failure,
    record_worker_processing_start,
    record_worker_processing_success,
    set_worker_running,
)


_feishu_queue: asyncio.Queue[FeishuMessageContext] = asyncio.Queue()
_worker_task: Optional[asyncio.Task] = None
_worker_handler: Optional[Callable[[FeishuMessageContext], Awaitable[Any]]] = None
_worker_logger = logging.getLogger(__name__)


async def enqueue_feishu_message(ctx: FeishuMessageContext) -> None:
    await _feishu_queue.put(ctx)
    record_message_enqueued(_feishu_queue.qsize())
    _worker_logger.info(
        "Feishu message enqueued message_id=%s message_type=%s queue_size=%s",
        ctx.message_id,
        ctx.message_type,
        _feishu_queue.qsize(),
    )


def start_feishu_worker(
    handler: Callable[[FeishuMessageContext], Awaitable[Any]],
    logger: Optional[logging.Logger] = None,
) -> asyncio.Task:
    global _worker_task, _worker_handler, _worker_logger

    if logger is not None:
        _worker_logger = logger
    _worker_handler = handler

    if _worker_task is not None and not _worker_task.done():
        return _worker_task

    _worker_task = asyncio.create_task(_feishu_worker_loop())
    set_worker_running(True)
    return _worker_task


async def _feishu_worker_loop() -> None:
    if _worker_handler is None:
        raise RuntimeError("Feishu worker handler is not configured.")

    _worker_logger.info("Feishu worker started")
    try:
        while True:
            ctx = await _feishu_queue.get()
            start_ts = asyncio.get_running_loop().time()
            try:
                record_worker_processing_start(_feishu_queue.qsize())
                _worker_logger.info(
                    "Feishu worker dequeued message_id=%s message_type=%s queue_size=%s",
                    ctx.message_id,
                    ctx.message_type,
                    _feishu_queue.qsize(),
                )
                await _worker_handler(ctx)
                record_worker_processing_success(asyncio.get_running_loop().time() - start_ts)
            except Exception as exc:
                record_worker_processing_failure(exc)
                _worker_logger.exception(
                    "Feishu worker failed message_id=%s message_type=%s",
                    ctx.message_id,
                    ctx.message_type,
                )
            finally:
                _feishu_queue.task_done()
    finally:
        set_worker_running(False)