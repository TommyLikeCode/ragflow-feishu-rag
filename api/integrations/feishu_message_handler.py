from __future__ import annotations

import logging
from typing import Any

from api.db.services.dialog_service import DialogService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.integrations.feishu_kb_acl import filter_kb_ids_for_user
from api.integrations.feishu_kb_selection import (
    build_selection_key,
    clear_selection,
    get_selection,
    set_selection,
)
from api.integrations.feishu_message_context import FeishuMessageContext


def _kb_display_name(kb_id: str) -> str:
    exists, kb = KnowledgebaseService.get_by_id(kb_id)
    if not exists or not kb:
        return kb_id
    return kb.name or kb_id


def _dialog_kbs(dialog_id: str) -> list[dict[str, str]]:
    exists, dialog = DialogService.get_by_id(dialog_id)
    if not exists or not dialog:
        return []
    rows: list[dict[str, str]] = []
    for kb_id in list(dialog.kb_ids or []):
        if not kb_id:
            continue
        rows.append({"kb_id": kb_id, "name": _kb_display_name(kb_id)})
    return rows


def _accessible_kbs(dialog_id: str, feishu_user_id: str) -> tuple[list[dict[str, str]], list[str]]:
    all_kbs = _dialog_kbs(dialog_id)
    report = filter_kb_ids_for_user(feishu_user_id, [x["kb_id"] for x in all_kbs])
    allowed_ids = list(report.get("filtered_kb_ids", []) or [])
    allowed_set = set(allowed_ids)
    return [x for x in all_kbs if x["kb_id"] in allowed_set], allowed_ids


def _format_kb_list(title: str, rows: list[dict[str, str]]) -> str:
    if not rows:
        return f"{title}\n暂无可访问知识库。"
    lines = [title]
    lines.extend(f"{i}. {row['name']} ({row['kb_id']})" for i, row in enumerate(rows, start=1))
    return "\n".join(lines)


def _find_kb_candidates(query: str, rows: list[dict[str, str]]) -> list[dict[str, str]]:
    q = (query or "").strip().lower()
    if not q:
        return []
    exact = [row for row in rows if row["kb_id"].lower() == q or row["name"].lower() == q]
    if exact:
        return exact
    return [row for row in rows if q in row["kb_id"].lower() or q in row["name"].lower()]


async def _handle_kb_command(ctx: FeishuMessageContext, bridge, logger: logging.Logger) -> dict[str, Any] | None:
    text = (ctx.text or "").strip()
    if not text.startswith("/"):
        return None

    command, _, arg = text.partition(" ")
    arg = arg.strip()
    supported = {"/知识库", "/当前知识库", "/切换知识库", "/使用全部知识库"}
    if command not in supported:
        return None

    dialog_id = bridge.resolve_dialog_id()
    if not dialog_id:
        reply_text = "当前未配置默认 Dialog，无法读取知识库。"
        await bridge.send_reply(ctx.open_id or ctx.user_id, reply_text)
        return {"status": "replied", "reason": "missing-dialog", "reply_text": reply_text}

    selection_key = build_selection_key(ctx.to_task_payload())
    feishu_user_id = ctx.open_id or ctx.user_id
    if not selection_key or not feishu_user_id:
        reply_text = "无法识别当前飞书用户，暂时不能切换知识库。"
        await bridge.send_reply(ctx.open_id or ctx.user_id, reply_text)
        return {"status": "replied", "reason": "missing-user", "reply_text": reply_text}

    accessible, allowed_ids = _accessible_kbs(dialog_id, feishu_user_id)
    all_kbs = _dialog_kbs(dialog_id)

    if command == "/知识库":
        reply_text = (
            _format_kb_list("你可访问的知识库：", accessible)
            + "\n\n发送 /切换知识库 知识库名称 可切换当前问答范围。\n发送 /使用全部知识库 可恢复全部可访问知识库。"
        )
    elif command == "/当前知识库":
        selection = get_selection(selection_key)
        final_ids = [kb_id for kb_id in allowed_ids if kb_id in set(selection.get("selected_kb_ids", []))]
        if not selection.get("selected_kb_ids"):
            reply_text = "当前使用：全部可访问知识库"
        elif not final_ids:
            reply_text = "当前选择的知识库不可访问或为空，请使用 /知识库 查看可访问知识库。"
        else:
            name_by_id = {row["kb_id"]: row["name"] for row in all_kbs}
            rows = [{"kb_id": kb_id, "name": name_by_id.get(kb_id, kb_id)} for kb_id in final_ids]
            reply_text = _format_kb_list("当前使用知识库：", rows)
    elif command == "/使用全部知识库":
        clear_selection(selection_key)
        reply_text = "已恢复为全部可访问知识库。"
    else:
        if not arg:
            reply_text = "请发送 /切换知识库 知识库名称或KB_ID。"
        else:
            candidates = _find_kb_candidates(arg, all_kbs)
            allowed_set = set(allowed_ids)
            if not candidates:
                reply_text = "未找到该知识库"
            elif len(candidates) > 1:
                reply_text = _format_kb_list("找到多个匹配的知识库，请使用更完整的名称或 KB_ID：", candidates)
            elif candidates[0]["kb_id"] not in allowed_set:
                reply_text = "你无权访问该知识库"
            else:
                row = candidates[0]
                set_selection(selection_key, [row["kb_id"]], [row["name"]])
                reply_text = f"已切换到知识库：{row['name']}"

    logger.info(
        "Feishu KB command handled message_id=%s command=%s selection_key=%s",
        ctx.message_id,
        command,
        selection_key,
    )
    await bridge.send_reply(ctx.open_id or ctx.user_id, reply_text)
    return {
        "status": "replied",
        "reason": "kb-command",
        "command": command,
        "dialog_id": dialog_id,
        "selection_key": selection_key,
        "reply_text": reply_text,
    }


def _context_to_legacy_parsed(ctx: FeishuMessageContext) -> dict[str, Any]:
    return {
        "message_id": ctx.message_id,
        "open_id": ctx.open_id or ctx.user_id,
        "user_id": ctx.user_id,
        "chat_id": ctx.chat_id,
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
        command_result = await _handle_kb_command(ctx, bridge, logger)
        if command_result is not None:
            return command_result
        return await bridge.handle_text_message(parsed)
    except Exception:
        logger.exception(
            "Feishu worker failed message_id=%s message_type=%s",
            ctx.message_id,
            ctx.message_type,
        )
        raise
