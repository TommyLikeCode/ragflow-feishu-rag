import importlib
import logging
from typing import Any, Optional


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
        parsed = self.parse_ws_event(event)
        should_handle, reason = self.should_handle_event(parsed)
        if not should_handle:
            self.logger.info("ignored Feishu WS event: %s", reason)
            return {"status": "ignored", "reason": reason, "event": parsed}

        return await self.handle_text_message(parsed)

    def parse_ws_event(self, event: dict[str, Any]) -> dict[str, Any]:
        helpers = self._helpers(optional=True)
        if helpers and hasattr(helpers, "_parse_feishu_event"):
            base = helpers._parse_feishu_event(event)
        else:
            base = self._fallback_parse_feishu_event(event)

        header = event.get("header") if isinstance(event, dict) else {}
        event_body = event.get("event") if isinstance(event, dict) else {}
        message = event_body.get("message") if isinstance(event_body, dict) else {}
        content = base.get("message_content") or {}

        text = ""
        if isinstance(content, dict):
            text = (content.get("text") or content.get("raw") or "").strip()
        elif isinstance(content, str):
            text = content.strip()

        return {
            "event_type": base.get("event_type", ""),
            "open_id": base.get("open_id", ""),
            "chat_type": base.get("chat_type", ""),
            "message_type": base.get("message_type", ""),
            "message_content": content,
            "text": text,
            "message_id": message.get("message_id", "") if isinstance(message, dict) else "",
            "header": header if isinstance(header, dict) else {},
            "raw_event": event,
        }

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
    ) -> dict[str, Any]:
        helpers = self._helpers(optional=True)
        if hasattr(helpers, "_ask_feishu_with_session"):
            return await helpers._ask_feishu_with_session(dialog_id, open_id, session_id, question)

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
        return await self.client.send_text(open_id, reply_text, receive_id_type="open_id")

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
        answer_data = await self.ask_ragflow(dialog_id, open_id, session_id, question)
        answer_text = answer_data.get("answer", "") if isinstance(answer_data, dict) else ""
        references = self.extract_references(answer_data if isinstance(answer_data, dict) else {})
        reply_text = self.format_reply(answer_text, references)
        send_result = await self.send_reply(open_id, reply_text)

        return {
            "status": "replied",
            "dialog_id": dialog_id,
            "session_id": session_id,
            "open_id": open_id,
            "reply_text": reply_text,
            "send_result": send_result,
        }

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
        if not references:
            return answer_text

        lines = ["References:"]
        for i, ref in enumerate(references[:3], start=1):
            if not isinstance(ref, dict):
                continue
            doc_name = ref.get("document_name") or ref.get("doc_name") or ref.get("name") or "Unknown document"
            content = ref.get("content") or ref.get("snippet") or ref.get("text") or ""
            content = " ".join(str(content).split())[:80]
            lines.append(f"{i}. {doc_name}" + (f": {content}" if content else ""))

        return f"{answer_text}\n\n" + "\n".join(lines)
