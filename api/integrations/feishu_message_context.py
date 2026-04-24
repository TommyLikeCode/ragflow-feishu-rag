from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class FeishuMessageContext:
    message_id: str = ""
    chat_id: str = ""
    chat_type: str = ""
    user_id: str = ""
    open_id: str = ""
    tenant_key: str = ""
    message_type: str = ""
    text: str = ""
    image_key: str = ""
    file_key: str = ""
    raw_content: str = ""
    event_time: str = ""
    trace_id: str = ""

    @classmethod
    def from_ws_event(cls, event: dict[str, Any]) -> "FeishuMessageContext":
        payload = event if isinstance(event, dict) else {}
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        event_body = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        message = event_body.get("message") if isinstance(event_body.get("message"), dict) else {}
        sender = event_body.get("sender") if isinstance(event_body.get("sender"), dict) else {}
        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}

        raw_content_obj = message.get("content", "")
        raw_content = raw_content_obj if isinstance(raw_content_obj, str) else json.dumps(raw_content_obj, ensure_ascii=False)

        content_obj: dict[str, Any] = {}
        if isinstance(raw_content_obj, str):
            try:
                parsed = json.loads(raw_content_obj)
                if isinstance(parsed, dict):
                    content_obj = parsed
            except Exception:
                content_obj = {"raw": raw_content_obj}
        elif isinstance(raw_content_obj, dict):
            content_obj = raw_content_obj
        else:
            content_obj = {"raw": raw_content_obj}

        text = ""
        if isinstance(content_obj.get("text"), str):
            text = content_obj.get("text", "").strip()
        elif isinstance(content_obj.get("raw"), str):
            text = content_obj.get("raw", "").strip()

        image_key = content_obj.get("image_key") if isinstance(content_obj.get("image_key"), str) else ""
        file_key = content_obj.get("file_key") if isinstance(content_obj.get("file_key"), str) else ""

        return cls(
            message_id=message.get("message_id", "") if isinstance(message.get("message_id"), str) else "",
            chat_id=message.get("chat_id", "") if isinstance(message.get("chat_id"), str) else "",
            chat_type=message.get("chat_type", "") if isinstance(message.get("chat_type"), str) else "",
            user_id=sender_id.get("user_id", "") if isinstance(sender_id.get("user_id"), str) else "",
            open_id=sender_id.get("open_id", "") if isinstance(sender_id.get("open_id"), str) else "",
            tenant_key=sender_id.get("tenant_key", "") if isinstance(sender_id.get("tenant_key"), str) else "",
            message_type=message.get("message_type", "") if isinstance(message.get("message_type"), str) else "",
            text=text,
            image_key=image_key.strip(),
            file_key=file_key.strip(),
            raw_content=raw_content,
            event_time=(
                message.get("create_time", "") if isinstance(message.get("create_time"), str)
                else header.get("create_time", "") if isinstance(header.get("create_time"), str)
                else ""
            ),
            trace_id=(
                header.get("event_id", "") if isinstance(header.get("event_id"), str)
                else payload.get("event_id", "") if isinstance(payload.get("event_id"), str)
                else ""
            ),
        )

    def is_text(self) -> bool:
        return self.message_type == "text"

    def is_image(self) -> bool:
        return self.message_type == "image"

    def is_file(self) -> bool:
        return self.message_type == "file"

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "user_id": self.user_id,
            "open_id": self.open_id,
            "tenant_key": self.tenant_key,
            "message_type": self.message_type,
            "text": self.text,
            "image_key": self.image_key,
            "file_key": self.file_key,
            "raw_content": self.raw_content,
            "event_time": self.event_time,
            "trace_id": self.trace_id,
        }

    def to_task_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        attachments: list[str] = []
        if self.image_key:
            attachments.append(self.image_key)
        if self.file_key:
            attachments.append(self.file_key)
        payload["attachments"] = attachments
        return payload
