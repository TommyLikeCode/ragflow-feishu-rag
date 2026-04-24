#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.apps import app
from api.db.services.dialog_service import DialogService, async_ask
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from common import settings
from rag.nlp.search import index_name


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _truncate(text: Any, max_len: int = 160) -> str:
    s = text if isinstance(text, str) else str(text)
    return s if len(s) <= max_len else s[:max_len] + "...(truncated)"


async def _run_retrieval_probe(question: str, kb_ids: list[str], tenant_id: str) -> dict[str, Any]:
    probe: dict[str, Any] = {
        "question": question,
        "error": "",
        "final_received": False,
        "retrieval_result_count": 0,
        "chunk_count": 0,
        "doc_aggs_count": 0,
        "top_chunk_preview": "",
    }

    try:
        final_payload: dict[str, Any] = {}
        async for item in async_ask(
            question=question,
            kb_ids=kb_ids,
            tenant_id=tenant_id,
            search_config={},
        ):
            if isinstance(item, dict) and item.get("final"):
                final_payload = item
                break

        probe["final_received"] = bool(final_payload)
        ref = final_payload.get("reference", {}) if isinstance(final_payload, dict) else {}
        chunks = ref.get("chunks", []) if isinstance(ref, dict) else []
        doc_aggs = ref.get("doc_aggs", []) if isinstance(ref, dict) else []

        if isinstance(chunks, list):
            probe["chunk_count"] = len(chunks)
            probe["retrieval_result_count"] = len(chunks)
            if chunks:
                first_chunk = chunks[0]
                if isinstance(first_chunk, dict):
                    preview = first_chunk.get("content") or first_chunk.get("snippet") or first_chunk.get("text") or ""
                    probe["top_chunk_preview"] = _truncate(preview)
                else:
                    probe["top_chunk_preview"] = _truncate(first_chunk)

        if isinstance(doc_aggs, list):
            probe["doc_aggs_count"] = len(doc_aggs)
    except Exception as exc:
        probe["error"] = str(exc)

    return probe


async def _collect_diagnostics(dialog_id: str, question: str | None) -> dict[str, Any]:
    exists, dialog = DialogService.get_by_id(dialog_id)
    if not exists or not dialog:
        return {
            "ok": False,
            "dialog_id": dialog_id,
            "error": f"Dialog not found: {dialog_id}",
        }

    kb_ids = list(dialog.kb_ids or [])
    tenant_id = dialog.tenant_id
    idx_name = index_name(tenant_id)

    kb_rows = list(KnowledgebaseService.get_by_ids(kb_ids)) if kb_ids else []
    kb_info: list[dict[str, Any]] = []

    for kb in kb_rows:
        docs, doc_count = DocumentService.get_by_kb_id(
            kb.id,
            page_number=1,
            items_per_page=500,
            orderby="create_time",
            desc=True,
            keywords="",
            run_status=[],
            types=[],
            suffix=[],
        )

        chunk_count = sum(_safe_int(d.get("chunk_num")) for d in docs)
        recent_docs = []
        for d in docs[:5]:
            recent_docs.append(
                {
                    "doc_id": d.get("id", ""),
                    "name": d.get("name", ""),
                    "run": d.get("run"),
                    "progress": d.get("progress"),
                    "chunk_num": d.get("chunk_num"),
                    "progress_msg": _truncate(d.get("progress_msg", ""), 200),
                }
            )

        not_ready_docs = []
        for d in docs:
            run_state = _safe_int(d.get("run"))
            chunk_num = _safe_int(d.get("chunk_num"))
            if run_state in {0, 1, 4} or (run_state == 3 and chunk_num == 0):
                not_ready_docs.append(d.get("name") or d.get("id") or "unknown")
        parsed_done = len(not_ready_docs) == 0
        parsed_msg = "" if parsed_done else f"not_ready_docs={not_ready_docs[:5]}"
        try:
            index_exists = bool(settings.docStoreConn.index_exist(idx_name, kb.id))
        except Exception as exc:
            index_exists = False
            parsed_msg = f"{parsed_msg}; index_exist_error={exc}" if parsed_msg else f"index_exist_error={exc}"

        kb_info.append(
            {
                "kb_id": kb.id,
                "kb_name": kb.name,
                "tenant_id": kb.tenant_id,
                "doc_count": doc_count,
                "chunk_count": chunk_count,
                "index_name": idx_name,
                "index_exists": index_exists,
                "is_parsed_done": bool(parsed_done),
                "parsed_status_message": parsed_msg or "",
                "recent_docs": recent_docs,
            }
        )

    retrieval_probe = None
    if question:
        retrieval_probe = await _run_retrieval_probe(question=question, kb_ids=kb_ids, tenant_id=tenant_id)

    return {
        "ok": True,
        "dialog_id": dialog_id,
        "tenant_id": tenant_id,
        "dialog_kb_ids": kb_ids,
        "kb_diagnostics": kb_info,
        "retrieval_probe": retrieval_probe,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose Feishu dialog knowledgebase retrieval state.")
    parser.add_argument("--dialog-id", required=True, help="Target dialog_id")
    parser.add_argument("--question", default="", help="Optional retrieval probe question")
    args = parser.parse_args()

    async with app.app_context():
        result = await _collect_diagnostics(args.dialog_id.strip(), (args.question or "").strip() or None)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
