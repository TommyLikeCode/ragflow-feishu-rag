#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.apps import app
from api.db.services.dialog_service import DialogService
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _get_dialog(dialog_id: str):
    exists, dialog = DialogService.get_by_id(dialog_id)
    if not exists or not dialog:
        raise RuntimeError(f"Dialog not found: {dialog_id}")
    return dialog


def _kb_snapshot(kb_id: str) -> dict[str, Any]:
    exists, kb = KnowledgebaseService.get_by_id(kb_id)
    if not exists or not kb:
        return {
            "kb_id": kb_id,
            "exists": False,
            "doc_count": 0,
            "chunk_count": 0,
            "is_empty": True,
        }

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
    return {
        "kb_id": kb.id,
        "kb_name": kb.name,
        "exists": True,
        "doc_count": doc_count,
        "chunk_count": chunk_count,
        "is_empty": doc_count == 0 or chunk_count == 0,
    }


def _pick_retained_kbs(kb_snapshots: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
    current_kb_ids = [kb["kb_id"] for kb in kb_snapshots]
    empty_kb_ids = [kb["kb_id"] for kb in kb_snapshots if kb.get("is_empty")]
    retained_kb_ids = [kb_id for kb_id in current_kb_ids if kb_id not in empty_kb_ids]
    return current_kb_ids, empty_kb_ids, retained_kb_ids


async def _cleanup_dialog_kbs(dialog_id: str, apply: bool) -> dict[str, Any]:
    dialog = _get_dialog(dialog_id)
    kb_snapshots = [_kb_snapshot(kb_id) for kb_id in list(dialog.kb_ids or [])]
    current_kb_ids, empty_kb_ids, retained_kb_ids = _pick_retained_kbs(kb_snapshots)

    result = {
        "dialog_id": dialog_id,
        "current_kb_ids": current_kb_ids,
        "empty_kb_ids": empty_kb_ids,
        "retained_kb_ids": retained_kb_ids,
        "updated_kb_ids": retained_kb_ids,
        "dry_run": not apply,
        "kb_snapshots": kb_snapshots,
    }

    if apply:
        DialogService.update_by_id(dialog_id, {"kb_ids": retained_kb_ids})
        result["applied"] = True
    else:
        result["applied"] = False

    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description="Cleanup empty KB bindings from a Feishu default dialog.")
    parser.add_argument("--dialog-id", required=True, help="Target dialog_id")
    parser.add_argument("--apply", action="store_true", help="Apply the KB binding cleanup")
    args = parser.parse_args()

    async with app.app_context():
        result = await _cleanup_dialog_kbs(args.dialog_id.strip(), args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
