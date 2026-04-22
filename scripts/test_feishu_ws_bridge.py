import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class MockFeishuClient:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []

    async def send_text(
        self,
        receive_id: str,
        text: str,
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        payload = {
            "mock": True,
            "receive_id": receive_id,
            "receive_id_type": receive_id_type,
            "text": text,
        }
        self.sent_messages.append(payload)
        return payload


def build_sample_payload(open_id: str, text: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "event_id": "mock-event-1",
            "event_type": "im.message.receive_v1",
            "tenant_key": "mock-tenant",
        },
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": open_id,
                }
            },
            "message": {
                "message_id": "mock-message-1",
                "chat_id": "mock-chat-id",
                "chat_type": "p2p",
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


async def main() -> None:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("FEISHU_WS_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        from api.integrations.feishu_ws_bridge import FeishuWSBridge
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Failed to import bridge test dependencies. "
            f"Missing module: {exc.name}. "
            "Install the minimal project dependencies before running this script."
        ) from exc

    open_id = os.environ.get("FEISHU_TEST_OPEN_ID", "ou_mock_user")
    question = os.environ.get("FEISHU_TEST_QUESTION", "请用知识库回答：这个项目是做什么的？")
    dialog_id = os.environ.get("FEISHU_DEFAULT_DIALOG_ID", "").strip()
    mock_mode = (os.environ.get("FEISHU_TEST_MOCK_MODE", "1").strip().lower() not in {"0", "false", "no", "off"})
    payload = build_sample_payload(open_id, question)
    client = MockFeishuClient()
    bridge = FeishuWSBridge(client=client, default_dialog_id=dialog_id or "mock-dialog-id")

    parsed = bridge.parse_ws_event(payload)
    should_handle, reason = bridge.should_handle_event(parsed)

    print("=== Sample Payload ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("=== Parsed Event ===")
    print(json.dumps(parsed, ensure_ascii=False, indent=2, default=str))
    print("=== Should Handle ===")
    print(json.dumps({"should_handle": should_handle, "reason": reason}, ensure_ascii=False, indent=2))

    if not should_handle:
        raise RuntimeError(f"Bridge rejected the sample payload: {reason}")

    if mock_mode:
        async def fake_resolve_or_create_session(dialog_id: str, open_id: str, force_new_session: bool = False) -> str:
            return "mock-session-id"

        async def fake_ask_ragflow(dialog_id: str, open_id: str, session_id: str, question: str) -> dict[str, Any]:
            return {
                "answer": f"[mock-answer] {question}",
                "reference": {
                    "chunks": [
                        {
                            "document_name": "mock-kb.txt",
                            "content": "This is a mocked knowledge-base snippet used for local bridge validation.",
                        }
                    ]
                },
            }

        bridge.resolve_or_create_session = fake_resolve_or_create_session  # type: ignore[method-assign]
        bridge.ask_ragflow = fake_ask_ragflow  # type: ignore[method-assign]

        result = await bridge.handle_event(payload)
    else:
        try:
            from api.apps import app
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Full-chain bridge test requires backend app dependencies. "
                f"Missing module: {exc.name}. "
                "Install the project runtime environment or keep FEISHU_TEST_MOCK_MODE=1."
            ) from exc

        async with app.app_context():
            result = await bridge.handle_event(payload)

    print("=== Bridge Result ===")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print("=== Mock Send Payload ===")
    if client.sent_messages:
        print(json.dumps(client.sent_messages[-1], ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps({"warning": "no mock send payload captured"}, ensure_ascii=False, indent=2))


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
