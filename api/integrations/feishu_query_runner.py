from __future__ import annotations

import logging
import time
from typing import Any

from api.db.services.api_service import API4ConversationService
from api.db.services.conversation_service import async_iframe_completion
from api.db.services.dialog_service import DialogService
from api.integrations.feishu_answer_constraint import apply_answer_constraint
from api.integrations.feishu_kb_acl import filter_kb_ids_for_user
from api.integrations.query_rewrite import rewrite_for_retrieval


def _extract_iframe_data(chunk: Any) -> dict[str, Any] | None:
    if isinstance(chunk, dict):
        return chunk
    if not isinstance(chunk, str) or not chunk.startswith("data:"):
        return None

    import json

    try:
        payload = json.loads(chunk[len("data:"):].strip())
    except Exception:
        return None

    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _get_previous_user_query(session_id: str) -> str:
    if not session_id:
        return ""
    try:
        exists, conv = API4ConversationService.get_by_id(session_id)
        if not exists or not conv:
            return ""
        msgs = conv.message if isinstance(conv.message, list) else []
        for msg in reversed(msgs):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
    except Exception:
        logging.exception("[feishu/query-runner] load previous question failed session_id=%s", session_id)
    return ""


async def bootstrap_session(dialog_id: str, feishu_user_id: str) -> str:
    async for chunk in async_iframe_completion(
        dialog_id,
        "",
        session_id=None,
        stream=False,
        user_id=feishu_user_id,
    ):
        data = _extract_iframe_data(chunk)
        if data and data.get("session_id"):
            return str(data.get("session_id"))
    return ""


async def ask_feishu_kb_question(
    dialog_id: str,
    feishu_user_id: str,
    question: str,
    session_id: str = "",
    apply_acl: bool = True,
    apply_rewrite: bool = True,
) -> dict[str, Any]:
    exists, dialog = DialogService.get_by_id(dialog_id)
    if not exists or not dialog:
        return {
            "pipeline_name": "feishu_iframe_completion",
            "answer": f"Dialog not found: {dialog_id}",
            "reference": {"chunks": [], "doc_aggs": []},
            "session_id": session_id,
            "original_query": question,
            "used_original_query": question,
            "used_retrieval_query": question,
            "rewrite_applied": False,
            "strategy": "dialog_not_found",
        }

    if not session_id:
        session_id = await bootstrap_session(dialog_id, feishu_user_id)

    acl_report = filter_kb_ids_for_user(feishu_user_id, list(dialog.kb_ids or [])) if apply_acl else {
        "feishu_user_id": feishu_user_id,
        "internal_user_id": feishu_user_id,
        "department_id": "",
        "original_kb_ids": list(dialog.kb_ids or []),
        "filtered_kb_ids": list(dialog.kb_ids or []),
        "denied_kb_ids": [],
    }
    filtered_kb_ids = acl_report.get("filtered_kb_ids", [])

    if not filtered_kb_ids:
        return {
            "pipeline_name": "feishu_iframe_completion",
            "answer": "当前账号无可访问知识库，请联系管理员配置权限。",
            "reference": {"chunks": [], "doc_aggs": []},
            "session_id": session_id,
            "original_query": question,
            "used_original_query": question,
            "used_retrieval_query": question,
            "rewrite_applied": False,
            "strategy": "acl_empty",
            "acl": acl_report,
        }

    previous_query = _get_previous_user_query(session_id)
    rewrite_result = rewrite_for_retrieval(question, previous_query) if apply_rewrite else {
        "original_query": question,
        "rewritten_query": question,
        "rewrite_applied": False,
        "strategy": "disabled",
        "llm_attempted": False,
    }
    retrieval_query = rewrite_result.get("rewritten_query", question) or question

    logging.info(
        "[feishu/query-runner] dialog_id=%s feishu_user_id=%s internal_user_id=%s department_id=%s original_kb_ids=%s filtered_kb_ids=%s original_query=%r rewritten_query=%r rewrite_applied=%s strategy=%s",
        dialog_id,
        acl_report.get("feishu_user_id", ""),
        acl_report.get("internal_user_id", ""),
        acl_report.get("department_id", ""),
        acl_report.get("original_kb_ids", []),
        acl_report.get("filtered_kb_ids", []),
        rewrite_result.get("original_query", question),
        retrieval_query,
        rewrite_result.get("rewrite_applied", False),
        rewrite_result.get("strategy", ""),
    )

    async for chunk in async_iframe_completion(
        dialog_id,
        question,
        session_id=session_id,
        stream=False,
        user_id=feishu_user_id,
        kb_ids_override=filtered_kb_ids,
        retrieval_query=retrieval_query,
    ):
        data = _extract_iframe_data(chunk)
        if data:
            constrained = apply_answer_constraint(question, data.get("answer", ""), data.get("reference", {}))
            if constrained.get("applied"):
                logging.info(
                    "[feishu/answer-constraint] applied=%s intent=%s rule=%s original_answer=%r constrained_answer=%r",
                    constrained.get("applied", False),
                    constrained.get("intent", ""),
                    constrained.get("rule", ""),
                    data.get("answer", ""),
                    constrained.get("answer", ""),
                )
                data["answer"] = constrained.get("answer", data.get("answer", ""))
            data.setdefault("answer_constraint", constrained)
            data.setdefault("pipeline_name", "feishu_iframe_completion")
            data.setdefault("original_query", question)
            data.setdefault("used_original_query", question)
            data.setdefault("used_retrieval_query", retrieval_query)
            data.setdefault("rewrite_applied", bool(rewrite_result.get("rewrite_applied", False)))
            data.setdefault("strategy", rewrite_result.get("strategy", ""))
            data.setdefault("acl", acl_report)
            data.setdefault("session_id", session_id)
            return data

    return {
        "pipeline_name": "feishu_iframe_completion",
        "answer": "",
        "reference": {"chunks": [], "doc_aggs": []},
        "session_id": session_id,
        "original_query": question,
        "used_original_query": question,
        "used_retrieval_query": retrieval_query,
        "rewrite_applied": bool(rewrite_result.get("rewrite_applied", False)),
        "strategy": rewrite_result.get("strategy", ""),
        "acl": acl_report,
    }
