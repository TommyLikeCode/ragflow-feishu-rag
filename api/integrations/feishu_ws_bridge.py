import importlib
import logging
import re
from typing import Any, Optional

from api.integrations.feishu_message_context import FeishuMessageContext
from api.integrations.feishu_metrics import record_ws_received
from api.integrations.feishu_kb_selection import build_selection_key
from api.integrations.feishu_task_queue import enqueue_feishu_message


class FeishuWSBridge:
    def __init__(
        self,
        client,
        default_dialog_id: str = "",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.client = client
        self.default_dialog_id = default_dialog_id
        self.logger = logger or logging.getLogger(__name__)
        self._feishu_helpers = None

    async def handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        context = FeishuMessageContext.from_ws_event(event)
        record_ws_received()
        self.logger.info(
            "Feishu WS received context=%s",
            context.to_log_dict(),
        )
        await enqueue_feishu_message(context)
        return {"status": "enqueued", "context": context.to_task_payload()}

    def parse_ws_event(self, event: dict[str, Any], context: Optional[FeishuMessageContext] = None) -> dict[str, Any]:
        context = context or FeishuMessageContext.from_ws_event(event)
        helpers = self._helpers(optional=True)
        if helpers and hasattr(helpers, "_parse_feishu_event"):
            base = helpers._parse_feishu_event(event)
        else:
            base = self._fallback_parse_feishu_event(event)

        header = event.get("header") if isinstance(event, dict) else {}
        event_body = event.get("event") if isinstance(event, dict) else {}
        message = event_body.get("message") if isinstance(event_body, dict) else {}
        raw_content = message.get("content") if isinstance(message, dict) else ""
        content = base.get("message_content") or {}

        text = context.text
        image_key = context.image_key
        file_key = context.file_key

        return {
            "event_type": base.get("event_type", ""),
            "open_id": base.get("open_id", ""),
            "chat_type": base.get("chat_type", ""),
            "message_type": base.get("message_type", ""),
            "message_content": content,
            "raw_content_preview": self._truncate_for_log(
                context.raw_content if context.raw_content else raw_content,
                200,
            ),
            "text": text,
            "image_key": image_key,
            "file_key": file_key,
            "message_id": context.message_id,
            "header": header if isinstance(header, dict) else {},
            "raw_event": event,
            "context": context.to_task_payload(),
        }

    def _truncate_for_log(self, value: Any, max_len: int) -> str:
        text = value if isinstance(value, str) else str(value)
        if len(text) <= max_len:
            return text
        return text[:max_len] + "...(truncated)"

    def should_handle_event(self, parsed: dict[str, Any]) -> tuple[bool, str]:
        event_type = parsed.get("event_type", "")
        if not event_type.startswith("im.message.receive"):
            return False, "non-message-event"
        if parsed.get("chat_type") != "p2p":
            return False, "non-p2p"
        if parsed.get("message_type") != "text":
            return False, "non-text"
        if not parsed.get("text"):
            return False, "empty-text"
        if not parsed.get("open_id"):
            return False, "missing-open-id"
        return True, ""

    def resolve_dialog_id(self, requested_dialog_id: str = "") -> str:
        helpers = self._helpers(optional=True)
        if helpers and hasattr(helpers, "_resolve_dialog_id"):
            return helpers._resolve_dialog_id(requested_dialog_id or self.default_dialog_id)
        return (requested_dialog_id or self.default_dialog_id or "").strip()

    async def resolve_or_create_session(
        self,
        dialog_id: str,
        open_id: str,
        force_new_session: bool = False,
    ) -> str:
        helpers = self._helpers()
        session_id = ""
        if not force_new_session:
            session_id = helpers._resolve_feishu_session(dialog_id, open_id)
        if session_id:
            return session_id
        return await helpers._bootstrap_feishu_session(dialog_id, open_id)

    async def ask_ragflow(
        self,
        dialog_id: str,
        open_id: str,
        session_id: str,
        question: str,
        selection_key: str = "",
    ) -> dict[str, Any]:
        helpers = self._helpers(optional=True)
        if hasattr(helpers, "_ask_feishu_with_session"):
            return await helpers._ask_feishu_with_session(dialog_id, open_id, session_id, question, selection_key=selection_key)
        from api.db.services.conversation_service import async_iframe_completion

        answer = None
        async for chunk in async_iframe_completion(
            dialog_id,
            question,
            session_id=session_id,
            stream=False,
            user_id=open_id,
        ):
            answer = chunk
            break
        return answer or {}

    def extract_references(self, answer_data: dict[str, Any]) -> list[dict[str, Any]]:
        helpers = self._helpers(optional=True)
        if helpers and hasattr(helpers, "_extract_references_from_answer"):
            return helpers._extract_references_from_answer(answer_data)
        return self._fallback_extract_references(answer_data)

    def format_reply(self, answer_text: str, references: list[dict[str, Any]]) -> str:
        helpers = self._helpers(optional=True)
        if helpers and hasattr(helpers, "_format_feishu_reply"):
            formatted = helpers._format_feishu_reply(answer_text, references)
            return formatted.get("reply_text", answer_text)
        return self._fallback_format_reply(answer_text, references)

    async def send_reply(self, open_id: str, reply_text: str) -> Any:
        return await self.client.send_text(open_id, f"[WS] {reply_text}", receive_id_type="open_id")

    async def handle_text_message(self, parsed: dict[str, Any]) -> dict[str, Any]:
        dialog_id = self.resolve_dialog_id()
        if not dialog_id:
            raise RuntimeError(
                "No dialog_id available for Feishu WS bridge. "
                "Set FEISHU_DEFAULT_DIALOG_ID or pass default_dialog_id explicitly."
            )

        open_id = parsed["open_id"]
        question = parsed["text"]
        raw_event = parsed.get("raw_event") or {}
        helpers = self._helpers(optional=True)
        if helpers and hasattr(helpers, "_should_force_new_session"):
            force_new_session = helpers._should_force_new_session(raw_event, question)
        else:
            force_new_session = False
        session_id = await self.resolve_or_create_session(dialog_id, open_id, force_new_session)
        selection_key = build_selection_key(parsed.get("raw_event") or parsed)
        answer_data = await self.ask_ragflow(dialog_id, open_id, session_id, question, selection_key=selection_key)
        answer_text = answer_data.get("answer", "") if isinstance(answer_data, dict) else ""
        references = self.extract_references(answer_data if isinstance(answer_data, dict) else {})
        reply_text = self.format_reply(answer_text, references)
        self.logger.info(
            "Feishu reply prepared message_id=%s reply_text_len=%s",
            parsed.get("message_id", ""),
            len(reply_text),
        )
        send_result = await self.send_reply(open_id, reply_text)

        return {
            "status": "replied",
            "dialog_id": dialog_id,
            "session_id": session_id,
            "open_id": open_id,
            "selection_key": selection_key,
            "reply_text": reply_text,
            "send_result": send_result,
        }

    async def _add_debug_reaction(self, parsed: dict[str, Any]) -> None:
        message_id = parsed.get("message_id", "")
        if not message_id:
            self.logger.warning("Feishu reaction skipped: missing message_id")
            return

        self.logger.info("Feishu reaction attempt message_id=%s emoji_type=%s", message_id, "THUMBSUP")
        try:
            await self.client.add_message_reaction(message_id, emoji_type="THUMBSUP")
        except Exception:
            self.logger.exception("Feishu reaction failed message_id=%s", message_id)

    def _helpers(self, optional: bool = False):
        if self._feishu_helpers is None:
            try:
                self._feishu_helpers = importlib.import_module("api.apps.feishu")
            except ModuleNotFoundError:
                if optional:
                    return None
                raise
        return self._feishu_helpers

    def _fallback_parse_feishu_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = payload.get("event") if isinstance(payload, dict) else {}
        event = event if isinstance(event, dict) else {}
        header = payload.get("header") if isinstance(payload, dict) else {}
        header = header if isinstance(header, dict) else {}

        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content")

        if isinstance(content, str):
            import json

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

    def _fallback_extract_references(self, answer_data: dict[str, Any]) -> list[dict[str, Any]]:
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

    def _fallback_format_reply(self, answer_text: str, references: list[dict[str, Any]]) -> str:
        answer_text = answer_text or ""
        references = references if isinstance(references, list) else []

        def _normalize_snippet(value: Any, max_chars: int = 100) -> str:
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
            content = _normalize_snippet(
                ref.get("content") or ref.get("snippet") or ref.get("text") or ref.get("content_with_weight") or ""
            )
            normalized_refs.append({"doc_name": doc_name, "snippet": content})

        source_lines = []
        if cited_indexes:
            for idx in cited_indexes[:3]:
                if 0 <= idx < len(normalized_refs):
                    source_lines.append(normalized_refs[idx])
                else:
                    source_lines.append({"doc_name": f"来源片段 {idx + 1}", "snippet": ""})
        else:
            source_lines = normalized_refs[:3]

        if not source_lines:
            return answer_text

        lines = ["本回答基于以下资料生成："]
        for i, item in enumerate(source_lines, start=1):
            lines.append(f"{i}. {item['doc_name']}" + (f"：{item['snippet']}" if item["snippet"] else ""))

        return f"{answer_text}\n\n" + "\n".join(lines)
