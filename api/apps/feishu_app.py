#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import os
import logging
import json
import re

from quart import request

from api.apps import app
from api.db.services.api_service import API4ConversationService
from api.db.services.dialog_service import DialogService, async_chat
from api.db.services.document_service import doc_upload_and_parse
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import TenantService
from api.integrations.feishu_citation_formatter import SOURCE_HEADING, format_answer_with_citations
from api.integrations.feishu_kb_acl import evaluate_kb_acl_for_user
from api.integrations.feishu_query_runner import ask_feishu_kb_question
from api.integrations.feishu_metrics import get_feishu_health_snapshot, get_feishu_metrics_snapshot
from api.utils.api_utils import (
    get_data_error_result,
    get_json_result,
    get_request_json,
    server_error_response,
    validate_request,
)
from common.constants import StatusEnum, LLMType
from common import settings
from common.misc_utils import get_uuid, thread_pool_exec


def _resolve_dialog_id(dialog_id):
    if dialog_id:
        return dialog_id

    env_dialog_id = os.environ.get("FEISHU_DEFAULT_DIALOG_ID", "").strip()
    if env_dialog_id:
        return env_dialog_id

    dialogs = DialogService.query(
        status=StatusEnum.VALID.value,
        order_by=DialogService.model.create_time,
        reverse=False,
    )
    if dialogs:
        return dialogs[0].id

    return ""


def _check_bootstrap_route_allowed(route_name):
    allowed_envs = {"debug", "dev", "development", "test", "testing", "local"}
    env_name = (
        os.environ.get("FEISHU_BOOTSTRAP_ENV")
        or os.environ.get("RAGFLOW_ENV")
        or os.environ.get("APP_ENV")
        or os.environ.get("ENV")
        or ""
    ).strip().lower()
    bootstrap_enabled = (
        os.environ.get("FEISHU_BOOTSTRAP_ENABLED")
        or os.environ.get("RAGFLOW_BOOTSTRAP_ENABLED")
        or ""
    ).strip().lower() in {"1", "true", "yes", "on"}

    if bootstrap_enabled or env_name in allowed_envs:
        return True, ""

    message = (
        f"{route_name} is debug-only/bootstrap-only and is disabled in the current environment"
    )
    return False, message


async def _do_chat(question, dialog_id):
    exists, dialog = DialogService.get_by_id(dialog_id)
    if not exists:
        return None, f"Dialog not found: {dialog_id}"

    messages = [{"role": "user", "content": question}]
    final_answer = ""
    final_reference = {}

    async for chunk in async_chat(dialog, messages, stream=False):
        final_answer = chunk.get("answer", "")
        final_reference = chunk.get("reference", {})

    references = []
    if isinstance(final_reference, dict):
        references = final_reference.get("chunks", []) or []
    elif isinstance(final_reference, list):
        references = final_reference

    return {
        "answer": final_answer,
        "references": references,
    }, ""


async def _bootstrap_kb(dialog_id):
    exists, dialog = DialogService.get_by_id(dialog_id)
    if not exists:
        return None, f"Dialog not found: {dialog_id}", 404

    tenant_id = dialog.tenant_id
    before_kb_ids = list(dialog.kb_ids or [])
    kb_list = KnowledgebaseService.query(
        tenant_id=tenant_id,
        status=StatusEnum.VALID.value,
        order_by=KnowledgebaseService.model.create_time,
        reverse=False,
    )
    if not kb_list:
        logging.info(
            "[feishu/bootstrap-kb] dialog_id=%s tenant_id=%s selected_kb_id=%s before_kb_ids=%s after_kb_ids=%s",
            dialog_id,
            tenant_id,
            "",
            before_kb_ids,
            before_kb_ids,
        )
        return None, "No knowledgebase found for current tenant", 404

    selected_kb_id = kb_list[0].id
    after_kb_ids = [selected_kb_id]
    updated = DialogService.update_by_id(dialog_id, {"kb_ids": after_kb_ids})
    if not updated:
        return None, f"Failed to update dialog: {dialog_id}", 500

    logging.info(
        "[feishu/bootstrap-kb] dialog_id=%s tenant_id=%s selected_kb_id=%s before_kb_ids=%s after_kb_ids=%s",
        dialog_id,
        tenant_id,
        selected_kb_id,
        before_kb_ids,
        after_kb_ids,
    )
    return {
        "dialog_id": dialog_id,
        "kb_ids": after_kb_ids,
        "kb_count": len(after_kb_ids),
    }, "", 0


def _invalid_field_message(field_name):
    return f"`{field_name}` is invalid"


def _normalize_non_empty_string(value, field_name, default=None, max_len=None):
    if value is None:
        value = default
    if not isinstance(value, str):
        return None, _invalid_field_message(field_name), 400

    normalized = value.strip()
    if not normalized:
        return None, f"`{field_name}` is required", 400
    if max_len and len(normalized.encode("utf-8")) > max_len:
        return None, f"`{field_name}` is too long", 400
    return normalized, "", 0


def _get_dialog_and_tenant(dialog_id):
    exists, dialog = DialogService.get_by_id(dialog_id)
    if not exists:
        return None, None, f"Dialog not found: {dialog_id}", 404

    tenant_ok, tenant = TenantService.get_by_id(dialog.tenant_id)
    if not tenant_ok:
        return dialog, None, f"Tenant not found: {dialog.tenant_id}", 404

    return dialog, tenant, "", 0


def _bind_dialog_kbs(dialog_id, kb_ids):
    if not dialog_id:
        return None, "`dialog_id` is required", 400
    if not isinstance(kb_ids, list):
        return None, _invalid_field_message("kb_ids"), 400

    new_kb_ids = [kb_id for kb_id in kb_ids if kb_id]
    if not new_kb_ids:
        return None, "`kb_ids` must be a non-empty list", 400

    exists, dialog = DialogService.get_by_id(dialog_id)
    if not exists:
        return None, f"Dialog not found: {dialog_id}", 404

    old_kb_ids = list(dialog.kb_ids or [])
    merged_kb_ids = list(dict.fromkeys(old_kb_ids + new_kb_ids))

    logging.info(
        "[feishu/bind-dialog-kbs] dialog_id=%s old_kb_ids=%s new_kb_ids=%s merged_kb_ids=%s",
        dialog_id,
        old_kb_ids,
        new_kb_ids,
        merged_kb_ids,
    )

    updated = DialogService.update_by_id(dialog_id, {"kb_ids": merged_kb_ids})
    if not updated:
        return None, f"Failed to update dialog: {dialog_id}", 500

    return {
        "dialog_id": dialog_id,
        "tenant_id": dialog.tenant_id,
        "before_kb_ids": old_kb_ids,
        "after_kb_ids": merged_kb_ids,
    }, "", 0


async def _bootstrap_kb_create(dialog_id, name):
    if not dialog_id:
        return None, "`dialog_id` is required", 400
    if not isinstance(name, str):
        return None, "`name` is invalid", 400

    kb_name = name.strip()
    if not kb_name:
        return None, "`name` is required", 400

    exists, dialog = DialogService.get_by_id(dialog_id)
    if not exists:
        return None, f"Dialog not found: {dialog_id}", 404

    tenant_id = dialog.tenant_id
    tenant_ok, tenant = TenantService.get_by_id(tenant_id)
    if not tenant_ok:
        return None, f"Tenant not found: {tenant_id}", 404
    if not tenant.embd_id:
        return None, f"No embedding model configured for tenant: {tenant_id}", 400

    logging.info(
        "[feishu/bootstrap-kb-create] start dialog_id=%s tenant_id=%s knowledgebase_id=%s knowledgebase_name=%s",
        dialog_id,
        tenant_id,
        "",
        kb_name,
    )

    created, kb_payload = KnowledgebaseService.create_with_name(
        name=kb_name,
        tenant_id=tenant_id,
        parser_id="naive",
        description="Feishu local debug knowledgebase (MVP)",
    )
    if not created:
        return None, "Failed to prepare knowledgebase payload", 500

    kb_payload["embd_id"] = tenant.embd_id
    saved = KnowledgebaseService.save(**kb_payload)
    if not saved:
        return None, "Failed to create knowledgebase", 500

    knowledgebase_id = kb_payload["id"]
    knowledgebase_name = kb_payload["name"]
    logging.info(
        "[feishu/bootstrap-kb-create] created dialog_id=%s tenant_id=%s knowledgebase_id=%s knowledgebase_name=%s",
        dialog_id,
        tenant_id,
        knowledgebase_id,
        knowledgebase_name,
    )

    bind_result, bind_err, bind_code = _bind_dialog_kbs(dialog_id, [knowledgebase_id])
    if bind_err:
        return None, bind_err, bind_code

    logging.info(
        "[feishu/bootstrap-kb-create] bound dialog_id=%s tenant_id=%s knowledgebase_id=%s knowledgebase_name=%s",
        dialog_id,
        tenant_id,
        knowledgebase_id,
        knowledgebase_name,
    )
    return {
        "dialog_id": dialog_id,
        "tenant_id": tenant_id,
        "knowledgebase_id": knowledgebase_id,
        "knowledgebase_name": knowledgebase_name,
        "kb_ids": bind_result["after_kb_ids"],
    }, "", 0


class _BootstrapTextFile:
    def __init__(self, filename, content):
        self.filename = filename
        self._content = content.encode("utf-8")

    def read(self):
        return self._content


async def _bootstrap_kb_text(dialog_id, text, filename):
    if not dialog_id:
        return None, "`dialog_id` is required", 400
    if not isinstance(text, str):
        return None, "`text` is invalid", 400
    if not isinstance(filename, str):
        return None, "`filename` is invalid", 400

    content = text.strip()
    if not content:
        return None, "`text` is required", 400

    safe_filename = filename.strip()
    if not safe_filename:
        return None, "`filename` is required", 400
    if "." not in safe_filename:
        safe_filename += ".txt"

    exists, dialog = DialogService.get_by_id(dialog_id)
    if not exists:
        return None, f"Dialog not found: {dialog_id}", 404
    if not dialog.kb_ids:
        return None, "Dialog has no bound knowledgebase. Please call bootstrap-kb or bootstrap-kb-create first.", 400

    temp_conversation_id = get_uuid()
    doc_ids = []
    text_preview = content[:50].replace("\n", " ")
    logging.info(
        "[feishu/bootstrap-kb-text] start dialog_id=%s filename=%s text_length=%s text_preview=%s conversation_id=%s",
        dialog_id,
        safe_filename,
        len(content),
        text_preview,
        temp_conversation_id,
    )

    try:
        API4ConversationService.save(
            id=temp_conversation_id,
            dialog_id=dialog_id,
            user_id="feishu-bootstrap",
            message=[],
            reference=[],
            source="dialog",
        )
        logging.info(
            "[feishu/bootstrap-kb-text] conversation-created dialog_id=%s filename=%s text_length=%s text_preview=%s conversation_id=%s",
            dialog_id,
            safe_filename,
            len(content),
            text_preview,
            temp_conversation_id,
        )
        file_obj = _BootstrapTextFile(safe_filename, content)
        logging.info(
            "[feishu/bootstrap-kb-text] parse-start dialog_id=%s filename=%s text_length=%s text_preview=%s conversation_id=%s",
            dialog_id,
            safe_filename,
            len(content),
            text_preview,
            temp_conversation_id,
        )
        try:
            doc_ids = await thread_pool_exec(
                doc_upload_and_parse,
                temp_conversation_id,
                [file_obj],
                dialog.tenant_id,
            )
            logging.info(
                "[feishu/bootstrap-kb-text] parse-finished dialog_id=%s filename=%s text_length=%s text_preview=%s conversation_id=%s",
                dialog_id,
                safe_filename,
                len(content),
                text_preview,
                temp_conversation_id,
            )
        except Exception as e:
            logging.exception(
                "[feishu/bootstrap-kb-text] parse-failed dialog_id=%s filename=%s text_length=%s text_preview=%s conversation_id=%s",
                dialog_id,
                safe_filename,
                len(content),
                text_preview,
                temp_conversation_id,
            )
            return None, f"Failed to upload or parse bootstrap text: {e}", 500
    finally:
        try:
            API4ConversationService.delete_by_id(temp_conversation_id)
            logging.info(
                "[feishu/bootstrap-kb-text] conversation-deleted dialog_id=%s filename=%s text_length=%s text_preview=%s conversation_id=%s",
                dialog_id,
                safe_filename,
                len(content),
                text_preview,
                temp_conversation_id,
            )
        except Exception:
            logging.exception(
                "[feishu/bootstrap-kb-text] conversation-delete-failed dialog_id=%s filename=%s text_length=%s text_preview=%s conversation_id=%s",
                dialog_id,
                safe_filename,
                len(content),
                text_preview,
                temp_conversation_id,
            )

    return {
        "dialog_id": dialog_id,
        "filename": safe_filename,
        "kb_ids": list(dialog.kb_ids or []),
        "doc_ids": doc_ids,
        "status": "success",
    }, "", 0


def _parse_feishu_event(payload):
    event = payload.get("event") if isinstance(payload, dict) else {}
    event = event if isinstance(event, dict) else {}
    header = payload.get("header") if isinstance(payload, dict) else {}
    header = header if isinstance(header, dict) else {}

    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}

    content = message.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            content = {"raw": message.get("content")}
    if not isinstance(content, dict):
        content = {"raw": content}

    return {
        "event_type": header.get("event_type") or payload.get("event_type") or event.get("type") or "",
        "open_id": sender_id.get("open_id") or "",
        "chat_type": message.get("chat_type") or "",
        "message_type": message.get("message_type") or "",
        "message_content": content,
    }


def _resolve_feishu_session(dialog_id, feishu_user_id):
    sessions = API4ConversationService.query(
        dialog_id=dialog_id,
        user_id=feishu_user_id,
        order_by=API4ConversationService.model.update_time,
        reverse=True,
    )
    if not sessions:
        return ""
    return sessions[0].id


def _format_feishu_reply(answer_text, references):
    formatted = format_answer_with_citations(answer_text or "", references if references is not None else [])
    reply_text = formatted.get("formatted_text", answer_text or "")
    display_answer = reply_text.split("\n\n本回答基于以下资料生成：", 1)[0]
    display_answer = reply_text.split(f"\n\n{SOURCE_HEADING}", 1)[0]
    return {
        "answer": display_answer,
        "references": references,
        "reply_text": reply_text,
        "normalized_sources": formatted.get("normalized_sources", []),
    }

    answer_text = answer_text or ""
    references = references if isinstance(references, list) else []

    def _normalize_snippet(value, max_chars=100):
        if value is None:
            return ""
        text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            return ""

        first_line = ""
        for line in text.split("\n"):
            line = line.strip()
            if line:
                first_line = line
                break
        text = first_line or text

        sentence_end = re.search(r"[。！？.!?]", text)
        if sentence_end:
            text = text[: sentence_end.end()]
        else:
            mixed_boundary = re.search(r"(?<=[\u4e00-\u9fff0-9a-z])([A-Z][A-Za-z0-9_\-]{2,})", text)
            if mixed_boundary:
                text = text[: mixed_boundary.start(1)].rstrip()

        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "..."
        elif text and not re.search(r"[。！？.!?]$", text):
            text = text + "..."
        return text

    citation_pattern = re.compile(r"\[ID:(\d+)\]")
    cited_indexes = []
    for match in citation_pattern.finditer(answer_text):
        idx = int(match.group(1))
        if idx not in cited_indexes:
            cited_indexes.append(idx)

    if cited_indexes:
        answer_text = citation_pattern.sub(lambda m: f"[来源{int(m.group(1)) + 1}]", answer_text)

    normalized_refs = []
    for i, ref in enumerate(references):
        if not isinstance(ref, dict):
            continue
        doc_name = (
            ref.get("document_name")
            or ref.get("doc_name")
            or ref.get("name")
            or ref.get("docnm_kwd")
            or f"来源片段 {i + 1}"
        )
        snippet = _normalize_snippet(
            ref.get("content") or ref.get("snippet") or ref.get("text") or ref.get("content_with_weight") or ""
        )
        normalized_refs.append({"doc_name": doc_name, "snippet": snippet})

    source_lines = []
    if cited_indexes:
        for idx in cited_indexes[:3]:
            if 0 <= idx < len(normalized_refs):
                source_lines.append((idx, normalized_refs[idx]))
            else:
                source_lines.append((idx, {"doc_name": f"来源片段 {idx + 1}", "snippet": ""}))
    else:
        for idx, item in enumerate(normalized_refs[:3]):
            source_lines.append((idx, item))

    lines = []
    if source_lines:
        lines.append("本回答基于以下资料生成：")
        for i, (_, item) in enumerate(source_lines, start=1):
            if item["snippet"]:
                lines.append(f"{i}. {item['doc_name']}：{item['snippet']}")
            else:
                lines.append(f"{i}. {item['doc_name']}")

    reply_text = answer_text
    if lines:
        reply_text = f"{answer_text}\n\n" + "\n".join(lines)

    return {
        "answer": answer_text,
        "references": references,
        "reply_text": reply_text,
    }


def _extract_references_from_answer(answer_data):
    if not isinstance(answer_data, dict):
        return []

    ref = answer_data.get("reference", {})
    if isinstance(ref, list):
        return ref
    if isinstance(ref, dict):
        for key in ["chunks", "references", "chunk_list", "doc_aggs", "docs"]:
            value = ref.get(key)
            if isinstance(value, list):
                return value

    refs = answer_data.get("references")
    if isinstance(refs, list):
        return refs
    return []


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _should_force_new_session(payload, text):
    # Explicit payload control has highest priority in local debugging.
    if isinstance(payload, dict) and "force_new_session" in payload:
        return _to_bool(payload.get("force_new_session"))

    # Environment-level override.
    if _to_bool(os.environ.get("FEISHU_EVENTS_FORCE_NEW_SESSION", "")):
        return True

    # For strict KB-grounded prompts, prefer a fresh session to avoid history drift.
    normalized = (text or "").strip().lower()
    strict_kb_markers = [
        "只根据知识库",
        "根据知识库原文",
        "给出原句",
        "引用来源",
        "原文回答",
    ]
    return any(marker in normalized for marker in strict_kb_markers)


def _extract_iframe_data(chunk):
    if isinstance(chunk, dict):
        return chunk
    if not isinstance(chunk, str) or not chunk.startswith("data:"):
        return None

    try:
        payload = json.loads(chunk[len("data:"):].strip())
    except Exception:
        return None

    data = payload.get("data")
    return data if isinstance(data, dict) else None


async def _ask_feishu_with_session(dialog_id, feishu_user_id, session_id, question, selection_key=""):
    return await ask_feishu_kb_question(
        dialog_id=dialog_id,
        feishu_user_id=feishu_user_id,
        question=question,
        session_id=session_id,
        selection_key=selection_key,
        apply_acl=True,
        apply_rewrite=True,
    )


async def _debug_acl(dialog_id, feishu_user_id):
    exists, dialog = DialogService.get_by_id(dialog_id)
    if not exists:
        return None, f"Dialog not found: {dialog_id}", 404

    report = evaluate_kb_acl_for_user(feishu_user_id, list(dialog.kb_ids or []))
    logging.info(
        "[feishu/debug-acl] dialog_id=%s feishu_user_id=%s internal_user_id=%s department_id=%s original_kb_ids=%s filtered_kb_ids=%s",
        dialog_id,
        report.get("feishu_user_id", ""),
        report.get("internal_user_id", ""),
        report.get("department_id", ""),
        report.get("original_kb_ids", []),
        report.get("filtered_kb_ids", []),
    )

    return {
        "dialog_id": dialog_id,
        "external_user": report.get("feishu_user_id", ""),
        "internal_user": report.get("internal_user_id", ""),
        "department_id": report.get("department_id", ""),
        "original_kb_ids": report.get("original_kb_ids", []),
        "filtered_kb_ids": report.get("filtered_kb_ids", []),
        "denied_kb_ids": report.get("denied_kb_ids", []),
        "per_kb_decisions": report.get("per_kb_decisions", []),
    }, "", 0


@manager.route("/ping", methods=["GET"])  # noqa: F821
async def ping():
    return get_json_result(data={"ok": True, "message": "feishu app alive"})


@manager.route("/metrics", methods=["GET"])  # noqa: F821
async def metrics():
    return get_json_result(data=get_feishu_metrics_snapshot())


@manager.route("/health", methods=["GET"])  # noqa: F821
async def health():
    return get_json_result(data=get_feishu_health_snapshot())


@manager.route("/chat", methods=["POST"])  # noqa: F821
@validate_request("user_id", "question")
async def chat():
    req = await get_request_json()
    user_id = req.get("user_id")
    question = (req.get("question") or "").strip()
    dialog_id = _resolve_dialog_id(req.get("dialog_id", ""))

    if not user_id:
        return get_data_error_result(message="`user_id` is required")
    if not question:
        return get_data_error_result(message="`question` is required")
    if not dialog_id:
        return get_data_error_result(
            message="`dialog_id` is required or set FEISHU_DEFAULT_DIALOG_ID in environment"
        )

    try:
        result, err = await _do_chat(question, dialog_id)
        if err:
            return get_data_error_result(message=err)

        return get_json_result(
            data={
                "question": question,
                "answer": result["answer"],
                "references": result["references"],
            }
        )
    except Exception as e:
        return server_error_response(e)


@manager.route("/events", methods=["POST"])  # noqa: F821
async def feishu_events():
    try:
        payload = await get_request_json()
        payload = payload if isinstance(payload, dict) else {}

        logging.info("[feishu/event-route] enter")

        challenge = payload.get("challenge")
        if challenge:
            logging.info("[feishu/event-route] challenge")
            return get_json_result(data={"challenge": challenge})

        parsed = _parse_feishu_event(payload)
        event_type = parsed["event_type"]
        open_id = parsed["open_id"]
        chat_type = parsed["chat_type"]
        message_type = parsed["message_type"]
        message_content = parsed["message_content"]

        if not event_type.startswith("im.message.receive"):
            logging.info("[feishu/event-route] ignored-non-message event_type=%s", event_type)
            return get_json_result(
                data={
                    "status": "ignored-non-message",
                    "event": parsed,
                }
            )

        if chat_type != "p2p":
            logging.info("[feishu/event-route] ignored-non-p2p chat_type=%s", chat_type)
            return get_json_result(
                data={
                    "status": "ignored-non-p2p",
                    "event": parsed,
                }
            )

        if message_type != "text":
            logging.info("[feishu/event-route] ignored-non-text message_type=%s", message_type)
            return get_json_result(
                data={
                    "status": "ignored-non-text",
                    "event": parsed,
                }
            )

        text = message_content.get("text") if isinstance(message_content, dict) else ""
        if not text:
            logging.info("[feishu/event-route] ignored-non-text message_type=%s", message_type)
            return get_json_result(
                data={
                    "status": "ignored-non-text",
                    "event": parsed,
                }
            )

        dialog_id = _resolve_dialog_id("")
        if not dialog_id:
            raise LookupError("No dialog_id available for feishu events")

        force_new_session = _should_force_new_session(payload, text)
        session_id = ""
        if not force_new_session:
            session_id = _resolve_feishu_session(dialog_id, open_id) if open_id else ""
        if not session_id:
            session_id = await _bootstrap_feishu_session(dialog_id, open_id)

        logging.info(
            "[feishu/event-route] resolved-session dialog_id=%s open_id=%s session_id=%s force_new_session=%s",
            dialog_id,
            open_id,
            session_id,
            force_new_session,
        )

        answer_data = await _ask_feishu_with_session(dialog_id, open_id, session_id, text)
        answer_text = answer_data.get("answer", "") if isinstance(answer_data, dict) else ""
        ref = answer_data.get("reference", {}) if isinstance(answer_data, dict) else {}
        references = _extract_references_from_answer(answer_data)

        ref_type = type(ref).__name__
        ref_keys = list(ref.keys())[:8] if isinstance(ref, dict) else []
        ref_chunks_len = len(ref.get("chunks") or []) if isinstance(ref, dict) and isinstance(ref.get("chunks"), list) else 0
        ref_doc_aggs_len = len(ref.get("doc_aggs") or []) if isinstance(ref, dict) and isinstance(ref.get("doc_aggs"), list) else 0
        logging.info(
            "[feishu/event-route] reference-shape ref_type=%s ref_keys=%s ref_chunks_len=%s ref_doc_aggs_len=%s extracted_references_len=%s",
            ref_type,
            ref_keys,
            ref_chunks_len,
            ref_doc_aggs_len,
            len(references),
        )

        session_id = answer_data.get("session_id") or session_id

        reply = _format_feishu_reply(answer_text=answer_text, references=references)
        logging.info("[feishu/event-route] ask-finished")
        return get_json_result(
            data={
                "status": "ok",
                "event": parsed,
                "dialog_id": dialog_id,
                "session_id": session_id,
                **reply,
            }
        )
    except Exception as e:
        logging.exception("[feishu/event-route] route-failed")
        return server_error_response(e)


@manager.route("/bootstrap-kb", methods=["POST"])  # noqa: F821
@validate_request("dialog_id")
async def bootstrap_kb():
    try:
        req = await get_request_json()
        dialog_id = (req.get("dialog_id") or "").strip()
        if not dialog_id:
            return get_data_error_result(message="`dialog_id` is required")

        data, err, code = await _bootstrap_kb(dialog_id)
        if err:
            return get_json_result(code=code, message=err)
        return get_json_result(data=data)
    except Exception as e:
        return server_error_response(e)


@manager.route("/bootstrap-kb-create", methods=["POST"])  # noqa: F821
@validate_request("dialog_id")
async def bootstrap_kb_create():
    try:
        allowed, message = _check_bootstrap_route_allowed("bootstrap-kb-create")
        if not allowed:
            logging.warning(
                "[feishu/bootstrap-route] blocked route=%s reason=%s",
                "bootstrap-kb-create",
                message,
            )
            return get_json_result(code=403, message=message, data=None)

        req = await get_request_json()
        dialog_id = req.get("dialog_id")
        logging.info(
            "[feishu/bootstrap-route] enter route=%s dialog_id=%s name=%s",
            "bootstrap-kb-create",
            dialog_id,
            req.get("name"),
        )

        data, err, code = await _bootstrap_kb_create(dialog_id, req.get("name"))
        if err:
            logging.warning(
                "[feishu/bootstrap-route] helper-failed route=%s dialog_id=%s code=%s message=%s",
                "bootstrap-kb-create",
                dialog_id,
                code,
                err,
            )
            return get_json_result(code=code, message=err, data=None)
        return get_json_result(code=0, message="success", data=data)
    except Exception as e:
        return server_error_response(e)


@manager.route("/bootstrap-kb-text", methods=["POST"])  # noqa: F821
@validate_request("dialog_id")
async def bootstrap_kb_text():
    try:
        allowed, message = _check_bootstrap_route_allowed("bootstrap-kb-text")
        if not allowed:
            logging.warning(
                "[feishu/bootstrap-route] blocked route=%s reason=%s",
                "bootstrap-kb-text",
                message,
            )
            return get_json_result(code=403, message=message, data=None)

        req = await get_request_json()
        dialog_id = req.get("dialog_id")
        text = req.get("text")
        filename = req.get("filename")
        logging.info(
            "[feishu/bootstrap-route] enter route=%s dialog_id=%s filename=%s text_length=%s",
            "bootstrap-kb-text",
            dialog_id,
            filename,
            len(text) if isinstance(text, str) else None,
        )

        data, err, code = await _bootstrap_kb_text(
            dialog_id,
            text,
            filename,
        )
        if err:
            logging.warning(
                "[feishu/bootstrap-route] helper-failed route=%s dialog_id=%s code=%s message=%s",
                "bootstrap-kb-text",
                dialog_id,
                code,
                err,
            )
            return get_json_result(code=code, message=err, data=None)
        return get_json_result(code=0, message="success", data=data)
    except Exception as e:
        return server_error_response(e)


# Compatibility aliases for callers that access /api/v1/feishu/* directly.
app.add_url_rule("/api/v1/feishu/ping", view_func=ping, methods=["GET"])
app.add_url_rule("/api/v1/feishu/chat", view_func=chat, methods=["POST"])
app.add_url_rule("/api/v1/feishu/bootstrap-kb", view_func=bootstrap_kb, methods=["POST"])
app.add_url_rule("/v1/feishu/bootstrap-kb", view_func=bootstrap_kb, methods=["POST"])
app.add_url_rule("/api/v1/feishu/bootstrap-kb-create", view_func=bootstrap_kb_create, methods=["POST"])
app.add_url_rule("/v1/feishu/bootstrap-kb-create", view_func=bootstrap_kb_create, methods=["POST"])
app.add_url_rule("/api/v1/feishu/bootstrap-kb-text", view_func=bootstrap_kb_text, methods=["POST"])
app.add_url_rule("/v1/feishu/bootstrap-kb-text", view_func=bootstrap_kb_text, methods=["POST"])
app.add_url_rule("/api/v1/feishu/events", view_func=feishu_events, methods=["POST"])
app.add_url_rule("/v1/feishu/events", view_func=feishu_events, methods=["POST"])


@manager.route("/dialogs", methods=["GET"])  # noqa: F821
async def list_dialogs_local():
    """Return up to 20 dialogs from DB for local debugging.

    Response format: { code, message, data: { total, items } }
    Each item includes id, name, tenant_id, kb_ids, status
    """
    try:
        dialogs = DialogService.query(
            status=StatusEnum.VALID.value,
            order_by=DialogService.model.create_time,
            reverse=True,
        )
        dialogs = [d.to_dict() for d in dialogs]
        items = []
        for d in dialogs[:20]:
            items.append({
                "id": d.get("id"),
                "name": d.get("name"),
                "tenant_id": d.get("tenant_id"),
                "kb_ids": d.get("kb_ids"),
                "status": d.get("status"),
            })

        logging.info("[feishu/dialogs] returned_count=%s", len(items))
        data = {"total": len(items), "items": items}
        return get_json_result(data=data)
    except Exception as e:
        return server_error_response(e)


@manager.route("/debug_acl", methods=["GET"])  # noqa: F821
async def debug_acl():
    try:
        dialog_id = (request.args.get("dialog_id") or "").strip()
        open_id = (request.args.get("open_id") or "").strip()
        user_id = (request.args.get("user_id") or "").strip()
        feishu_user_id = open_id or user_id

        if not dialog_id:
            return get_data_error_result(message="`dialog_id` is required")
        if not feishu_user_id:
            return get_data_error_result(message="`open_id` or `user_id` is required")

        data, err, code = await _debug_acl(dialog_id, feishu_user_id)
        if err:
            return get_json_result(code=code, message=err, data=None)
        return get_json_result(data=data)
    except Exception as e:
        return server_error_response(e)


app.add_url_rule("/api/v1/feishu/debug_acl", view_func=debug_acl, methods=["GET"])
app.add_url_rule("/v1/feishu/debug_acl", view_func=debug_acl, methods=["GET"])


def _is_usable_llm_id(tenant_id, llm_id):
    if not llm_id:
        return False
    try:
        return bool(TenantLLMService.get_api_key(tenant_id=tenant_id, model_name=llm_id))
    except Exception:
        return False


def _pick_llm_from_tenant_defaults(tenant):
    # Candidate 1: tenant default llm_id
    if _is_usable_llm_id(tenant.id, tenant.llm_id):
        return tenant.llm_id, "tenant_default"

    # Candidate 2: tenant_llm chat models
    tenant_chat_llms = TenantLLMService.query(
        tenant_id=tenant.id,
        model_type=LLMType.CHAT.value,
        status=StatusEnum.VALID.value,
    )
    for llm in tenant_chat_llms:
        candidates = [
            f"{llm.llm_name}@{llm.llm_factory}" if llm.llm_factory else llm.llm_name,
            llm.llm_name,
        ]
        for candidate in candidates:
            if _is_usable_llm_id(tenant.id, candidate):
                return candidate, "tenant_default"

    return None, "error"


async def _bootstrap_dialog(name=None):
    dialogs = DialogService.query(
        status=StatusEnum.VALID.value,
        order_by=DialogService.model.create_time,
        reverse=False,
    )
    found_existing = bool(dialogs)
    created = False
    existing_dialog_id = dialogs[0].id if dialogs else ""
    reusable_dialog = None
    reused_existing_llm = False

    for d in dialogs:
        if _is_usable_llm_id(d.tenant_id, d.llm_id):
            reusable_dialog = d
            reused_existing_llm = True
            break

    if reusable_dialog:
        d = reusable_dialog
        payload = {
            "dialog_id": d.id,
            "created": False,
            "name": d.name,
            "tenant_id": d.tenant_id,
            "kb_ids": d.kb_ids,
            "llm_id": d.llm_id,
            "source": "existing_dialog",
        }
        logging.info(
            "[feishu/bootstrap-dialog] found_existing_dialog=%s existing_dialog_id=%s reused_existing_llm_id=%s selected_llm_id=%s source=%s created_success=%s created_new_dialog=%s dialog_id=%s tenant_id=%s kb_ids=%s",
            found_existing,
            existing_dialog_id,
            reused_existing_llm,
            d.llm_id,
            payload["source"],
            True,
            created,
            payload["dialog_id"],
            payload["tenant_id"],
            payload["kb_ids"],
        )
        return payload, ""

    tenants = TenantService.query(
        status=StatusEnum.VALID.value,
        order_by=TenantService.model.create_time,
        reverse=False,
    )
    if not tenants:
        logging.info(
            "[feishu/bootstrap-dialog] found_existing_dialog=%s existing_dialog_id=%s reused_existing_llm_id=%s selected_llm_id=%s source=%s created_success=%s created_new_dialog=%s dialog_id=%s tenant_id=%s kb_ids=%s",
            found_existing,
            existing_dialog_id,
            reused_existing_llm,
            "",
            "error",
            False,
            False,
            "",
            "",
            [],
        )
        return None, "No valid tenant found. bootstrap-dialog is for local development only."

    tenant = tenants[0]
    kb_list = KnowledgebaseService.query(
        tenant_id=tenant.id,
        status=StatusEnum.VALID.value,
        order_by=KnowledgebaseService.model.create_time,
        reverse=False,
    )
    kb_ids = [kb_list[0].id] if kb_list else []

    llm_id, llm_source = _pick_llm_from_tenant_defaults(tenant)
    if not llm_id:
        logging.info(
            "[feishu/bootstrap-dialog] found_existing_dialog=%s existing_dialog_id=%s reused_existing_llm_id=%s selected_llm_id=%s source=%s created_success=%s created_new_dialog=%s dialog_id=%s tenant_id=%s kb_ids=%s",
            False,
            "",
            False,
            "",
            "error",
            False,
            False,
            "",
            tenant.id,
            kb_ids,
        )
        return None, "No usable llm_id found for bootstrap dialog"

    dialog_name = (name or "Feishu Debug Dialog").strip() or "Feishu Debug Dialog"
    dialog = {
        "id": get_uuid(),
        "tenant_id": tenant.id,
        "name": dialog_name,
        "description": "Feishu local debug dialog (MVP)",
        "icon": "",
        "kb_ids": kb_ids,
        "llm_id": llm_id,
        "rerank_id": "",
        "status": StatusEnum.VALID.value,
    }

    # If there are existing dialogs but none has usable llm_id, reuse non-llm fields from first one.
    if dialogs:
        template = dialogs[0]
        if template.prompt_config:
            dialog["prompt_config"] = template.prompt_config
        if template.do_refer is not None:
            dialog["do_refer"] = template.do_refer
        if template.top_k is not None:
            dialog["top_k"] = template.top_k
        if template.rerank_id is not None:
            dialog["rerank_id"] = template.rerank_id

    if not DialogService.save(**dialog):
        logging.info(
            "[feishu/bootstrap-dialog] found_existing_dialog=%s existing_dialog_id=%s reused_existing_llm_id=%s selected_llm_id=%s source=%s created_success=%s created_new_dialog=%s dialog_id=%s tenant_id=%s kb_ids=%s",
            found_existing,
            existing_dialog_id,
            reused_existing_llm,
            llm_id,
            llm_source,
            False,
            False,
            "",
            tenant.id,
            kb_ids,
        )
        return None, "Failed to create bootstrap dialog."

    created = True
    payload = {
        "dialog_id": dialog["id"],
        "created": created,
        "name": dialog["name"],
        "tenant_id": dialog["tenant_id"],
        "kb_ids": dialog["kb_ids"],
        "llm_id": dialog["llm_id"],
        "source": llm_source,
    }
    logging.info(
        "[feishu/bootstrap-dialog] found_existing_dialog=%s existing_dialog_id=%s reused_existing_llm_id=%s selected_llm_id=%s source=%s created_success=%s created_new_dialog=%s dialog_id=%s tenant_id=%s kb_ids=%s",
        found_existing,
        existing_dialog_id,
        reused_existing_llm,
        llm_id,
        llm_source,
        True,
        created,
        payload["dialog_id"],
        payload["tenant_id"],
        payload["kb_ids"],
    )
    return payload, ""


@manager.route("/bootstrap-dialog", methods=["POST"])  # noqa: F821
async def bootstrap_dialog():
    try:
        req = {}
        try:
            maybe_req = await get_request_json()
            if isinstance(maybe_req, dict):
                req = maybe_req
        except Exception:
            # Allow empty body for local bootstrap convenience.
            req = {}
        data, err = await _bootstrap_dialog(req.get("name"))
        if err:
            if err == "No usable llm_id found for bootstrap dialog":
                return get_json_result(code=500, message=err)
            return get_data_error_result(message=err)

        if not data.get("kb_ids"):
            data["note"] = "No knowledgebase bound. Chat may work but retrieval grounding may be limited."
        return get_json_result(data=data)
    except Exception as e:
        return server_error_response(e)


@manager.route("/bootstrap-llm", methods=["POST"])  # noqa: F821
@validate_request("api_key", "model_name")
async def bootstrap_llm():
    """Local debug endpoint to bootstrap a usable chat llm_id for Feishu flow."""
    try:
        req = await get_request_json()
        api_key = (req.get("api_key") or "").strip()
        model_name = (req.get("model_name") or "").strip()
        base_url = (req.get("base_url") or "https://dashscope.aliyuncs.com/compatible-mode/v1").strip()

        if not api_key:
            return get_data_error_result(message="`api_key` is required")
        if not model_name:
            return get_data_error_result(message="`model_name` is required")

        llm_factory = ""
        for factory in (settings.FACTORY_LLM_INFOS or []):
            if factory.get("name") == "Tongyi-Qianwen":
                llm_factory = factory["name"]
                break
        if not llm_factory:
            return get_data_error_result(message="Tongyi-Qianwen factory not found in current config")

        tenants = TenantService.query(
            status=StatusEnum.VALID.value,
            order_by=TenantService.model.create_time,
            reverse=False,
        )
        if not tenants:
            return get_data_error_result(message="No valid tenant found")
        tenant = tenants[0]

        llm_filters = [
            TenantLLMService.model.tenant_id == tenant.id,
            TenantLLMService.model.llm_factory == llm_factory,
            TenantLLMService.model.llm_name == model_name,
        ]
        llm_payload = {
            "model_type": LLMType.CHAT.value,
            "api_key": api_key,
            "api_base": base_url,
            "status": StatusEnum.VALID.value,
        }

        updated_rows = TenantLLMService.filter_update(llm_filters, llm_payload)
        upsert_action = "update"
        if not updated_rows:
            TenantLLMService.save(
                tenant_id=tenant.id,
                llm_factory=llm_factory,
                model_type=LLMType.CHAT.value,
                llm_name=model_name,
                api_key=api_key,
                api_base=base_url,
                max_tokens=8192,
                status=StatusEnum.VALID.value,
            )
            upsert_action = "create"

        llm_id = f"{model_name}@{llm_factory}"
        TenantService.filter_update(
            [TenantService.model.id == tenant.id],
            {"llm_id": llm_id},
        )

        dialogs = DialogService.query(tenant_id=tenant.id, status=StatusEnum.VALID.value)
        updated_dialog_count = 0
        for dialog in dialogs:
            if dialog.llm_id == llm_id:
                continue
            DialogService.filter_update([DialogService.model.id == dialog.id], {"llm_id": llm_id})
            updated_dialog_count += 1

        logging.info(
            "[feishu/bootstrap-llm] tenant_id=%s llm_factory=%s llm_name=%s target_llm_id=%s tenant_llm_action=%s updated_dialog_count=%s",
            tenant.id,
            llm_factory,
            model_name,
            llm_id,
            upsert_action,
            updated_dialog_count,
        )

        return get_json_result(
            data={
                "tenant_id": tenant.id,
                "llm_factory": llm_factory,
                "llm_name": model_name,
                "target_llm_id": llm_id,
                "updated_dialog_count": updated_dialog_count,
            }
        )
    except Exception as e:
        return server_error_response(e)


# compatibility alias
app.add_url_rule("/api/v1/feishu/dialogs", view_func=list_dialogs_local, methods=["GET"])
app.add_url_rule("/api/v1/feishu/metrics", view_func=metrics, methods=["GET"])
app.add_url_rule("/api/v1/feishu/health", view_func=health, methods=["GET"])
app.add_url_rule("/api/v1/feishu/bootstrap-dialog", view_func=bootstrap_dialog, methods=["POST"])
app.add_url_rule("/api/v1/feishu/bootstrap-llm", view_func=bootstrap_llm, methods=["POST"])
app.add_url_rule("/v1/feishu/bootstrap-llm", view_func=bootstrap_llm, methods=["POST"])
