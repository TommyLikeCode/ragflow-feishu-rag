from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from quart import Blueprint, request, send_from_directory

from api.apps import app
from api.db.services.api_service import API4ConversationService
from api.db.services.dialog_service import DialogService
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import TenantService
from api.integrations.feishu_kb_acl import get_acl_config, normalize_kb_policy, remove_kb_policy, upsert_kb_policy
from api.integrations.feishu_metrics import get_feishu_health_snapshot, get_feishu_metrics_snapshot
from api.utils.api_utils import get_data_error_result, get_json_result, get_request_json, server_error_response
from common.constants import StatusEnum, TaskStatus
from common import settings
from common.doc_store.doc_store_base import OrderByExpr
from rag.nlp.search import index_name

page_name = "admin_mvp"
manager = Blueprint(page_name, __name__)
STATIC_DIR = Path(__file__).resolve().parents[1] / "static" / "admin_mvp"


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _task_status_name(run: Any) -> str:
    code = _safe_int(run)
    if code == TaskStatus.RUNNING.value:
        return "running"
    if code == TaskStatus.CANCEL.value:
        return "cancelled"
    if code == TaskStatus.FAIL.value:
        return "failed"
    if code == TaskStatus.DONE.value:
        return "done"
    if code == TaskStatus.UNSTART.value:
        return "unstarted"
    return f"unknown({code})"


def _norm_user_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = value.split(",")
    else:
        items = []

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        user_id = str(item).strip()
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        out.append(user_id)
    return out


def _all_kb_snapshots() -> list[dict[str, Any]]:
    acl_cfg = get_acl_config()
    policies = acl_cfg.get("kb_policies") if isinstance(acl_cfg.get("kb_policies"), dict) else {}

    kbs = KnowledgebaseService.query(
        status=StatusEnum.VALID.value,
        order_by=KnowledgebaseService.model.update_time,
        reverse=True,
    )

    snapshots: list[dict[str, Any]] = []
    for kb in kbs:
        docs, doc_count = DocumentService.get_by_kb_id(
            kb.id,
            page_number=1,
            items_per_page=500,
            orderby="update_time",
            desc=True,
            keywords="",
            run_status=[],
            types=[],
            suffix=[],
        )
        chunk_count = sum(_safe_int(d.get("chunk_num")) for d in docs)

        policy = normalize_kb_policy(policies.get(kb.id) if isinstance(policies.get(kb.id), dict) else {})
        scope = policy.get("scope", "public")
        owner_user_id = policy.get("owner_user_id", "")
        department_id = policy.get("department_id", "")
        allowed_user_ids = policy.get("allowed_user_ids", [])
        denied_user_ids = policy.get("denied_user_ids", [])

        status = "normal"
        if doc_count == 0 or chunk_count == 0:
            status = "empty"
        elif chunk_count < 3:
            status = "low_chunk"

        snapshots.append(
            {
                "kb_id": kb.id,
                "name": kb.name,
                "tenant_id": kb.tenant_id,
                "description": kb.description or "",
                "doc_count": doc_count,
                "chunk_count": chunk_count,
                "scope": scope,
                "owner_user_id": owner_user_id,
                "department_id": department_id,
                "allowed_user_ids": allowed_user_ids,
                "denied_user_ids": denied_user_ids,
                "allowed_user_count": len(allowed_user_ids),
                "denied_user_count": len(denied_user_ids),
                "status": status,
                "update_time": kb.update_time,
                "update_date": kb.update_date,
            }
        )

    return snapshots


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return datetime.min
    for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]:
        try:
            return datetime.strptime(text[:19], fmt)
        except Exception:
            continue
    return datetime.min


def _paginate(rows: list[dict[str, Any]], page: int, page_size: int) -> tuple[list[dict[str, Any]], int]:
    p = max(1, _safe_int(page))
    ps = max(1, min(200, _safe_int(page_size) or 20))
    total = len(rows)
    start = (p - 1) * ps
    return rows[start:start + ps], total


def _all_docs() -> list[dict[str, Any]]:
    kbs = KnowledgebaseService.query(status=StatusEnum.VALID.value)
    docs: list[dict[str, Any]] = []
    for kb in kbs:
        rows, _ = DocumentService.get_by_kb_id(
            kb.id,
            page_number=1,
            items_per_page=1000,
            orderby="update_time",
            desc=True,
            keywords="",
            run_status=[],
            types=[],
            suffix=[],
        )
        for row in rows:
            docs.append(
                {
                    "id": row.get("id", ""),
                    "kb_id": kb.id,
                    "kb_name": kb.name,
                    "name": row.get("name", ""),
                    "file_type": row.get("type", ""),
                    "suffix": row.get("suffix", ""),
                    "run": row.get("run", 0),
                    "status": _task_status_name(row.get("run")),
                    "chunk_num": _safe_int(row.get("chunk_num")),
                    "progress": _safe_float(row.get("progress")),
                    "progress_msg": row.get("progress_msg", "") or "",
                    "create_date": row.get("create_date", ""),
                    "update_date": row.get("update_date", ""),
                }
            )
    docs.sort(key=lambda x: str(x.get("update_date", "")), reverse=True)
    return docs


def _latest_eval_summary() -> dict[str, Any]:
    logs_dir = Path(__file__).resolve().parents[2] / "logs"
    if not logs_dir.exists():
        return {}
    candidates = sorted(
        [p for p in logs_dir.glob("eval_*.json") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return {}
    try:
        payload = json.loads(candidates[0].read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {
        "file": str(candidates[0]),
        "name": candidates[0].name,
        "answer_keyword_match_rate": payload.get("answer_keyword_match_rate", 0),
        "source_keyword_match_rate": payload.get("source_keyword_match_rate", 0),
        "hit_rate": payload.get("hit_rate", 0),
    }


def _eval_files(limit: int = 30) -> list[Path]:
    logs_dir = Path(__file__).resolve().parents[2] / "logs"
    if not logs_dir.exists():
        return []
    files = [
        p for p in logs_dir.glob("eval_*.json")
        if p.is_file()
    ]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def _safe_eval_file(name: str) -> Path | None:
    if not name:
        return None
    p = Path(name)
    if p.is_absolute():
        # Restrict absolute path reads to current logs directory.
        logs_dir = (Path(__file__).resolve().parents[2] / "logs").resolve()
        try:
            resolved = p.resolve()
            if logs_dir in resolved.parents and resolved.exists() and resolved.suffix == ".json":
                return resolved
        except Exception:
            return None
        return None

    candidate = (Path(__file__).resolve().parents[2] / "logs" / p.name).resolve()
    if candidate.exists() and candidate.suffix == ".json":
        return candidate
    return None


def _load_eval_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["_meta"] = {
        "name": path.name,
        "path": str(path),
        "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "size": path.stat().st_size,
    }
    return payload


def _extract_hit_kbs_from_reference(reference: Any) -> list[str]:
    refs = reference if isinstance(reference, list) else [reference] if isinstance(reference, dict) else []
    hit_kbs: list[str] = []
    for ref in refs:
        chunks = ref.get("chunks") if isinstance(ref, dict) and isinstance(ref.get("chunks"), list) else []
        for c in chunks:
            if not isinstance(c, dict):
                continue
            kb_id = c.get("dataset_id") or c.get("kb_id") or c.get("knowledgebase_id")
            if isinstance(kb_id, str) and kb_id and kb_id not in hit_kbs:
                hit_kbs.append(kb_id)
    return hit_kbs


def _build_session_records(limit: int = 200) -> list[dict[str, Any]]:
    rows = (
        API4ConversationService.model.select()
        .order_by(API4ConversationService.model.update_time.desc())
        .limit(limit)
        .dicts()
    )
    sessions: list[dict[str, Any]] = []
    for row in rows:
        messages = row.get("message") if isinstance(row.get("message"), list) else []
        question = ""
        answer = ""
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            if role == "user" and isinstance(content, str):
                question = content
            if role == "assistant" and isinstance(content, str):
                answer = content

        references = row.get("reference") if isinstance(row.get("reference"), list) else []
        hit_kbs = _extract_hit_kbs_from_reference(references)
        success = bool(answer.strip()) and "no relevant content" not in answer.lower()
        failure_reason = "" if success else (row.get("errors") or "empty_answer")
        session_id = f"conv::{row.get('id', '')}"

        sessions.append(
            {
                "id": session_id,
                "source": "conversation",
                "time": row.get("update_date") or row.get("create_date") or "",
                "external_user": row.get("exp_user_id") or row.get("user_id") or "",
                "internal_user": row.get("user_id") or "",
                "used_original_query": question,
                "used_retrieval_query": question,
                "hit_kbs": hit_kbs,
                "pipeline_name": "feishu_iframe_completion",
                "success": success,
                "failure_reason": failure_reason,
                "latency_seconds": round(_safe_float(row.get("duration", 0)), 4),
                "answer_summary": (answer[:160] + "...") if len(answer) > 160 else answer,
                "answer": answer,
                "context_seed_question": "",
                "retrieved_kb_ids": hit_kbs,
                "rewrite_applied": False,
                "source_summary": [c.get("docnm_kwd") or c.get("document_name") or "" for ref in references if isinstance(ref, dict) for c in (ref.get("chunks") or []) if isinstance(c, dict)][:5],
            }
        )

    # If eval records are available, include them for richer rewrite visibility.
    eval_files = _eval_files(3)
    for p in eval_files:
        payload = _load_eval_payload(p)
        items = payload.get("per_case_results") if isinstance(payload.get("per_case_results"), list) else []
        eval_time = payload.get("_meta", {}).get("mtime", "")
        for it in items[:100]:
            if not isinstance(it, dict):
                continue
            case_id = str(it.get("case_id") or "")
            sid = f"eval::{p.name}::{case_id}"
            answer = str(it.get("answer") or "")
            sessions.append(
                {
                    "id": sid,
                    "source": "eval",
                    "time": eval_time,
                    "external_user": os.environ.get("FEISHU_EVAL_USER_ID", "ou_test_citation"),
                    "internal_user": "",
                    "used_original_query": it.get("used_original_query") or it.get("question") or "",
                    "used_retrieval_query": it.get("used_retrieval_query") or it.get("question") or "",
                    "hit_kbs": it.get("retrieved_kb_ids") if isinstance(it.get("retrieved_kb_ids"), list) else [],
                    "pipeline_name": it.get("pipeline_name") or payload.get("pipeline_name") or "feishu_iframe_completion",
                    "success": bool(it.get("success", False)),
                    "failure_reason": it.get("failure_reason") or "",
                    "latency_seconds": round(_safe_float(it.get("latency_seconds", 0)), 4),
                    "answer_summary": (answer[:160] + "...") if len(answer) > 160 else answer,
                    "answer": answer,
                    "context_seed_question": it.get("context_seed_question") or "",
                    "retrieved_kb_ids": it.get("retrieved_kb_ids") if isinstance(it.get("retrieved_kb_ids"), list) else [],
                    "rewrite_applied": bool((it.get("used_retrieval_query") or "") != (it.get("used_original_query") or "")),
                    "source_summary": it.get("matched_source_keywords") if isinstance(it.get("matched_source_keywords"), list) else [],
                }
            )

    sessions.sort(key=lambda x: _to_datetime(x.get("time")), reverse=True)
    return sessions


def _recent_qa(limit: int = 20) -> list[dict[str, Any]]:
    rows = (
        API4ConversationService.model.select()
        .order_by(API4ConversationService.model.update_time.desc())
        .limit(limit)
        .dicts()
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        messages = row.get("message") if isinstance(row.get("message"), list) else []
        question = ""
        answer = ""
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            if role == "user" and isinstance(content, str):
                question = content
            if role == "assistant" and isinstance(content, str):
                answer = content

        refs = row.get("reference") if isinstance(row.get("reference"), list) else []
        hit_kbs: list[str] = []
        if refs:
            tail = refs[-1] if isinstance(refs[-1], dict) else {}
            chunks = tail.get("chunks") if isinstance(tail, dict) and isinstance(tail.get("chunks"), list) else []
            for c in chunks:
                if not isinstance(c, dict):
                    continue
                kb_id = c.get("dataset_id") or c.get("kb_id") or c.get("knowledgebase_id")
                if isinstance(kb_id, str) and kb_id and kb_id not in hit_kbs:
                    hit_kbs.append(kb_id)

        success = bool(answer.strip()) and "no relevant content" not in answer.lower()
        result.append(
            {
                "time": row.get("update_date") or row.get("create_date") or "",
                "user": row.get("user_id", ""),
                "question": question,
                "answer": answer,
                "hit_kbs": hit_kbs,
                "success": success,
                "latency_seconds": round(_safe_float(row.get("duration", 0)), 4),
            }
        )
    return result


async def admin_ui():
    return await send_from_directory(str(STATIC_DIR), "index.html")


async def admin_asset(path: str):
    return await send_from_directory(str(STATIC_DIR / "assets"), path)


async def admin_dashboard():
    try:
        metrics = get_feishu_metrics_snapshot()
        health = get_feishu_health_snapshot()
        kb_snapshots = _all_kb_snapshots()
        docs = _all_docs()

        empty_kbs = [k for k in kb_snapshots if k.get("status") == "empty"]
        low_chunk_kbs = [k for k in kb_snapshots if k.get("status") == "low_chunk"]
        failed_docs = [d for d in docs if d.get("status") in {"failed", "cancelled"}]

        asked_total = _safe_int(metrics.get("received_total", 0))
        success_total = _safe_int(metrics.get("processing_success_total", 0))
        hit_rate = (success_total / asked_total) if asked_total else 0.0

        eval_summary = _latest_eval_summary()
        data = {
            "cards": {
                "today_questions": asked_total,
                "success_answers": success_total,
                "hit_rate": round(hit_rate, 4),
                "avg_response_seconds": round(_safe_float(metrics.get("avg_processing_seconds", 0)), 4),
                "kb_count": len(kb_snapshots),
                "document_count": len(docs),
            },
            "system_status": {
                "ws_connected": bool(health.get("ws_connected", False)),
                "worker_running": bool(health.get("worker_running", False)),
                "queue_size": _safe_int(health.get("queue_size", 0)),
                "last_processed_at": health.get("last_processed_at", ""),
                "last_error": health.get("last_error", ""),
            },
            "performance": {
                "answer_keyword_match_rate": eval_summary.get("answer_keyword_match_rate", 0),
                "source_keyword_match_rate": eval_summary.get("source_keyword_match_rate", 0),
                "query_rewrite_enabled": str(os.environ.get("FEISHU_QUERY_REWRITE_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"},
                "eval_file": eval_summary.get("file", ""),
            },
            "kb_health": {
                "empty_kbs": empty_kbs[:20],
                "low_chunk_kbs": low_chunk_kbs[:20],
                "failed_docs": failed_docs[:20],
            },
            "recent_qa": _recent_qa(20),
        }
        return get_json_result(data=data)
    except Exception as e:
        return server_error_response(e)


async def admin_kbs():
    try:
        q = (request.args.get("q") or "").strip().lower()
        scope = (request.args.get("scope") or "").strip().lower()
        empty_only = (request.args.get("empty_only") or "").strip().lower() in {"1", "true", "yes", "on"}
        department = (request.args.get("department") or "").strip().lower()
        page = _safe_int(request.args.get("page") or 1)
        page_size = _safe_int(request.args.get("page_size") or 20)

        rows = _all_kb_snapshots()
        if q:
            rows = [r for r in rows if q in str(r.get("name", "")).lower() or q in str(r.get("kb_id", "")).lower()]
        if scope:
            rows = [r for r in rows if (r.get("scope") or "") == scope]
        if department:
            rows = [r for r in rows if department in str(r.get("department_id") or "").lower()]
        if empty_only:
            rows = [r for r in rows if r.get("status") == "empty"]

        items, total = _paginate(rows, page, page_size)
        return get_json_result(data={"total": total, "page": max(1, page), "page_size": max(1, min(200, page_size or 20)), "items": items})
    except Exception as e:
        return server_error_response(e)


async def admin_kb_detail(kb_id: str):
    try:
        exists, kb = KnowledgebaseService.get_by_id(kb_id)
        if not exists or not kb:
            return get_json_result(code=404, message=f"KB not found: {kb_id}", data=None)

        docs, doc_count = DocumentService.get_by_kb_id(
            kb.id,
            page_number=1,
            items_per_page=200,
            orderby="update_time",
            desc=True,
            keywords="",
            run_status=[],
            types=[],
            suffix=[],
        )
        chunk_count = sum(_safe_int(d.get("chunk_num")) for d in docs)

        acl_cfg = get_acl_config()
        policy = normalize_kb_policy(
            acl_cfg.get("kb_policies", {}).get(kb.id, {}) if isinstance(acl_cfg.get("kb_policies"), dict) else {}
        )

        return get_json_result(
            data={
                "kb_id": kb.id,
                "name": kb.name,
                "tenant_id": kb.tenant_id,
                "description": kb.description or "",
                "scope": policy.get("scope", "public"),
                "owner_user_id": policy.get("owner_user_id", ""),
                "department_id": policy.get("department_id", ""),
                "allowed_user_ids": policy.get("allowed_user_ids", []),
                "denied_user_ids": policy.get("denied_user_ids", []),
                "acl_preview": {
                    "scope": policy.get("scope", "public"),
                    "owner_user_id": policy.get("owner_user_id", ""),
                    "department_id": policy.get("department_id", ""),
                    "allowed_user_ids": policy.get("allowed_user_ids", []),
                    "denied_user_ids": policy.get("denied_user_ids", []),
                },
                "doc_count": doc_count,
                "chunk_count": chunk_count,
                "update_date": kb.update_date,
                "documents": [
                    {
                        "id": d.get("id", ""),
                        "name": d.get("name", ""),
                        "status": _task_status_name(d.get("run")),
                        "chunk_num": _safe_int(d.get("chunk_num")),
                        "update_date": d.get("update_date", ""),
                    }
                    for d in docs[:100]
                ],
            }
        )
    except Exception as e:
        return server_error_response(e)


async def admin_kb_create():
    try:
        req = await get_request_json()
        name = (req.get("name") or "").strip()
        tenant_id = (req.get("tenant_id") or "").strip()
        scope = (req.get("scope") or "public").strip().lower() or "public"
        owner_user_id = (req.get("owner_user_id") or "").strip()
        department_id = (req.get("department_id") or "").strip()
        allowed_user_ids = _norm_user_ids(req.get("allowed_user_ids"))
        denied_user_ids = _norm_user_ids(req.get("denied_user_ids"))
        description = (req.get("description") or "").strip()

        if not name:
            return get_data_error_result(message="`name` is required")

        if not tenant_id:
            dialogs = DialogService.query(status=StatusEnum.VALID.value, order_by=DialogService.model.create_time, reverse=False)
            if not dialogs:
                return get_json_result(code=400, message="No dialog available to infer tenant_id", data=None)
            tenant_id = dialogs[0].tenant_id

        tenant_ok, tenant = TenantService.get_by_id(tenant_id)
        if not tenant_ok:
            return get_json_result(code=404, message=f"Tenant not found: {tenant_id}", data=None)

        ok, payload = KnowledgebaseService.create_with_name(
            name=name,
            tenant_id=tenant_id,
            parser_id="naive",
            description=description,
            embd_id=tenant.embd_id,
        )
        if not ok:
            return get_json_result(code=400, message=str(payload), data=None)

        saved = KnowledgebaseService.save(**payload)
        if not saved:
            return get_json_result(code=500, message="Failed to create KB", data=None)

        upsert_kb_policy(
            payload["id"],
            scope=scope,
            owner_user_id=owner_user_id,
            department_id=department_id,
            allowed_user_ids=allowed_user_ids,
            denied_user_ids=denied_user_ids,
        )
        return get_json_result(data={"kb_id": payload["id"], "name": payload["name"]})
    except Exception as e:
        return server_error_response(e)


async def admin_kb_update(kb_id: str):
    try:
        exists, kb = KnowledgebaseService.get_by_id(kb_id)
        if not exists or not kb:
            return get_json_result(code=404, message=f"KB not found: {kb_id}", data=None)

        req = await get_request_json()
        updates: dict[str, Any] = {}
        if isinstance(req.get("name"), str) and req.get("name").strip():
            updates["name"] = req.get("name").strip()
        if isinstance(req.get("description"), str):
            updates["description"] = req.get("description").strip()

        if updates:
            KnowledgebaseService.update_by_id(kb_id, updates)

        scope = (req.get("scope") or "public").strip().lower() if isinstance(req.get("scope"), str) else "public"
        owner_user_id = (req.get("owner_user_id") or "").strip() if isinstance(req.get("owner_user_id"), str) else ""
        department_id = (req.get("department_id") or "").strip() if isinstance(req.get("department_id"), str) else ""
        allowed_user_ids = _norm_user_ids(req.get("allowed_user_ids"))
        denied_user_ids = _norm_user_ids(req.get("denied_user_ids"))
        upsert_kb_policy(
            kb_id,
            scope=scope,
            owner_user_id=owner_user_id,
            department_id=department_id,
            allowed_user_ids=allowed_user_ids,
            denied_user_ids=denied_user_ids,
        )

        return get_json_result(data={"kb_id": kb_id, "updated": True})
    except Exception as e:
        return server_error_response(e)


async def admin_kb_delete(kb_id: str):
    try:
        exists, kb = KnowledgebaseService.get_by_id(kb_id)
        if not exists or not kb:
            return get_json_result(code=404, message=f"KB not found: {kb_id}", data=None)
        KnowledgebaseService.update_by_id(kb_id, {"status": StatusEnum.INVALID.value})
        remove_kb_policy(kb_id)
        return get_json_result(data={"kb_id": kb_id, "deleted": True})
    except Exception as e:
        return server_error_response(e)


async def admin_documents():
    try:
        q = (request.args.get("q") or "").strip().lower()
        kb_id_filter = (request.args.get("kb_id") or "").strip()
        kb_name_filter = (request.args.get("kb_name") or "").strip().lower()
        status_filter = (request.args.get("status") or "").strip().lower()
        page = _safe_int(request.args.get("page") or 1)
        page_size = _safe_int(request.args.get("page_size") or 20)

        rows = _all_docs()
        if kb_id_filter:
            rows = [r for r in rows if r.get("kb_id") == kb_id_filter]
        if kb_name_filter:
            rows = [r for r in rows if kb_name_filter in str(r.get("kb_name", "")).lower()]
        if status_filter:
            rows = [r for r in rows if (r.get("status") or "") == status_filter]
        if q:
            rows = [r for r in rows if q in str(r.get("name", "")).lower()]

        items, total = _paginate(rows, page, page_size)
        return get_json_result(data={"total": total, "page": max(1, page), "page_size": max(1, min(200, page_size or 20)), "items": items})
    except Exception as e:
        return server_error_response(e)


async def admin_document_detail(doc_id: str):
    try:
        exists, doc = DocumentService.get_by_id(doc_id)
        if not exists or not doc:
            return get_json_result(code=404, message=f"Document not found: {doc_id}", data=None)

        kb_ok, kb = KnowledgebaseService.get_by_id(doc.kb_id)
        tenant_id = kb.tenant_id if kb_ok and kb else ""
        idx = index_name(tenant_id) if tenant_id else ""

        chunk_preview: list[dict[str, Any]] = []
        if tenant_id:
            try:
                result = settings.docStoreConn.search(
                    ["content_with_weight", "docnm_kwd", "doc_id"],
                    [],
                    {"doc_id": doc.id},
                    [],
                    OrderByExpr(),
                    0,
                    5,
                    idx,
                    [doc.kb_id],
                )
                fields = settings.docStoreConn.get_fields(result, ["content_with_weight", "docnm_kwd", "doc_id"])
                for _, v in fields.items():
                    chunk_preview.append(
                        {
                            "doc_id": v.get("doc_id", ""),
                            "doc_name": v.get("docnm_kwd", ""),
                            "content": str(v.get("content_with_weight", ""))[:300],
                        }
                    )
            except Exception:
                chunk_preview = []

        return get_json_result(
            data={
                "id": doc.id,
                "name": doc.name,
                "kb_id": doc.kb_id,
                "status": _task_status_name(doc.run),
                "run": doc.run,
                "chunk_num": _safe_int(doc.chunk_num),
                "progress": _safe_float(doc.progress),
                "progress_msg": doc.progress_msg or "",
                "last_error": doc.progress_msg or "",
                "type": doc.type,
                "suffix": doc.suffix,
                "create_date": doc.create_date,
                "update_date": doc.update_date,
                "chunk_preview": chunk_preview,
            }
        )
    except Exception as e:
        return server_error_response(e)


async def admin_document_reparse(doc_id: str):
    try:
        exists, doc = DocumentService.get_by_id(doc_id)
        if not exists or not doc:
            return get_json_result(code=404, message=f"Document not found: {doc_id}", data=None)

        kb_ok, kb = KnowledgebaseService.get_by_id(doc.kb_id)
        if not kb_ok or not kb:
            return get_json_result(code=404, message=f"KB not found: {doc.kb_id}", data=None)

        doc_dict = doc.to_dict()
        DocumentService.begin2parse(doc.id)
        DocumentService.run(kb.tenant_id, doc_dict, {})

        return get_json_result(data={"id": doc.id, "reparse_queued": True})
    except Exception as e:
        return server_error_response(e)


async def admin_document_delete(doc_id: str):
    try:
        exists, doc = DocumentService.get_by_id(doc_id)
        if not exists or not doc:
            return get_json_result(code=404, message=f"Document not found: {doc_id}", data=None)
        kb_ok, kb = KnowledgebaseService.get_by_id(doc.kb_id)
        if not kb_ok or not kb:
            return get_json_result(code=404, message=f"KB not found: {doc.kb_id}", data=None)

        DocumentService.remove_document(doc, kb.tenant_id)
        return get_json_result(data={"id": doc.id, "deleted": True})
    except Exception as e:
        return server_error_response(e)


async def admin_acl_debug():
    try:
        dialog_id = (request.args.get("dialog_id") or "").strip()
        open_id = (request.args.get("open_id") or "").strip()
        user_id = (request.args.get("user_id") or "").strip()
        feishu_user_id = open_id or user_id
        if not dialog_id:
            return get_data_error_result(message="`dialog_id` is required")
        if not feishu_user_id:
            return get_data_error_result(message="`open_id` or `user_id` is required")

        from api.apps.feishu import _debug_acl

        data, err, code = await _debug_acl(dialog_id, feishu_user_id)
        if err:
            return get_json_result(code=code, message=err, data=None)
        return get_json_result(data=data)
    except Exception as e:
        return server_error_response(e)


async def admin_sessions():
    try:
        q = (request.args.get("q") or "").strip().lower()
        success = (request.args.get("success") or "").strip().lower()
        source = (request.args.get("source") or "").strip().lower()
        page = _safe_int(request.args.get("page") or 1)
        page_size = _safe_int(request.args.get("page_size") or 20)

        rows = _build_session_records(limit=300)
        if q:
            rows = [
                r for r in rows
                if q in str(r.get("used_original_query") or "").lower()
                or q in str(r.get("used_retrieval_query") or "").lower()
                or q in str(r.get("answer") or "").lower()
                or q in str(r.get("external_user") or "").lower()
                or q in str(r.get("internal_user") or "").lower()
            ]
        if success in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            wanted = success in {"1", "true", "yes", "on"}
            rows = [r for r in rows if bool(r.get("success", False)) == wanted]
        if source in {"conversation", "eval"}:
            rows = [r for r in rows if r.get("source") == source]

        items, total = _paginate(rows, page, page_size)
        return get_json_result(data={"total": total, "page": max(1, page), "page_size": max(1, min(200, page_size or 20)), "items": items})
    except Exception as e:
        return server_error_response(e)


async def admin_session_detail(session_id: str):
    try:
        rows = _build_session_records(limit=500)
        hit = next((r for r in rows if r.get("id") == session_id), None)
        if not hit:
            return get_json_result(code=404, message=f"Session not found: {session_id}", data=None)
        return get_json_result(data=hit)
    except Exception as e:
        return server_error_response(e)


def _eval_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "pipeline_name": payload.get("pipeline_name", "feishu_iframe_completion"),
        "total_cases": _safe_int(payload.get("total_cases", 0)),
        "hit_count": _safe_int(payload.get("hit_count", 0)),
        "hit_rate": _safe_float(payload.get("hit_rate", 0)),
        "answer_keyword_match_rate": _safe_float(payload.get("answer_keyword_match_rate", 0)),
        "source_keyword_match_rate": _safe_float(payload.get("source_keyword_match_rate", 0)),
        "avg_latency_seconds": _safe_float(payload.get("avg_latency_seconds", 0)),
        "failure_reason_counts": payload.get("failure_reason_counts") if isinstance(payload.get("failure_reason_counts"), dict) else {},
    }


async def admin_evals():
    try:
        files = _eval_files(limit=50)
        items: list[dict[str, Any]] = []
        for p in files:
            payload = _load_eval_payload(p)
            item = {
                "name": payload.get("_meta", {}).get("name", p.name),
                "path": payload.get("_meta", {}).get("path", str(p)),
                "mtime": payload.get("_meta", {}).get("mtime", ""),
                "size": payload.get("_meta", {}).get("size", 0),
                **_eval_summary(payload),
            }
            items.append(item)
        return get_json_result(data={"total": len(items), "items": items})
    except Exception as e:
        return server_error_response(e)


async def admin_evals_latest():
    try:
        files = _eval_files(limit=1)
        if not files:
            return get_json_result(data={"latest": None})
        payload = _load_eval_payload(files[0])
        per_case = payload.get("per_case_results") if isinstance(payload.get("per_case_results"), list) else []
        return get_json_result(
            data={
                "latest": {
                    "meta": payload.get("_meta", {}),
                    "summary": _eval_summary(payload),
                    "per_case_results": per_case,
                }
            }
        )
    except Exception as e:
        return server_error_response(e)


async def admin_evals_compare():
    try:
        before_name = (request.args.get("before") or "").strip()
        after_name = (request.args.get("after") or "").strip()

        files = _eval_files(limit=5)
        if not after_name and files:
            after_name = files[0].name
        if not before_name and len(files) > 1:
            before_name = files[1].name

        before_path = _safe_eval_file(before_name) if before_name else None
        after_path = _safe_eval_file(after_name) if after_name else None
        if not before_path or not after_path:
            return get_json_result(code=400, message="`before` and `after` eval files are required", data=None)

        before = _load_eval_payload(before_path)
        after = _load_eval_payload(after_path)
        before_summary = _eval_summary(before)
        after_summary = _eval_summary(after)

        keys = [
            "total_cases",
            "hit_count",
            "hit_rate",
            "answer_keyword_match_rate",
            "source_keyword_match_rate",
            "avg_latency_seconds",
        ]
        diff = {k: round(_safe_float(after_summary.get(k, 0)) - _safe_float(before_summary.get(k, 0)), 6) for k in keys}

        return get_json_result(
            data={
                "before": {"meta": before.get("_meta", {}), "summary": before_summary},
                "after": {"meta": after.get("_meta", {}), "summary": after_summary},
                "diff": diff,
                "failure_reason_counts": {
                    "before": before_summary.get("failure_reason_counts", {}),
                    "after": after_summary.get("failure_reason_counts", {}),
                },
            }
        )
    except Exception as e:
        return server_error_response(e)


async def admin_document_upload():
    try:
        form = await request.form
        files = await request.files
        kb_id = (form.get("kb_id") or "").strip()
        if not kb_id:
            return get_data_error_result(message="`kb_id` is required")
        if "file" not in files:
            return get_data_error_result(message="No file selected")

        e, kb = KnowledgebaseService.get_by_id(kb_id)
        if not e or not kb:
            return get_json_result(code=404, message=f"KB not found: {kb_id}", data=None)

        file_objs = files.getlist("file")
        if not file_objs:
            return get_data_error_result(message="No file selected")

        user_id = (form.get("user_id") or "").strip() or kb.tenant_id
        err, uploaded = FileService.upload_document(kb, file_objs, user_id)

        queued = []
        kb_table_num_map: dict[str, int] = {}
        for doc, _ in uploaded:
            try:
                DocumentService.begin2parse(doc["id"])
                DocumentService.run(kb.tenant_id, doc, kb_table_num_map)
                queued.append({"id": doc.get("id", ""), "name": doc.get("name", "")})
            except Exception as parse_err:
                err.append(f"{doc.get('name', '')}: queue failed: {parse_err}")

        return get_json_result(
            data={
                "kb_id": kb_id,
                "queued_count": len(queued),
                "queued": queued,
                "errors": err,
            }
        )
    except Exception as e:
        return server_error_response(e)


async def admin_defaults():
    default_dialog_id = (os.environ.get("FEISHU_DEFAULT_DIALOG_ID") or "").strip()
    if not default_dialog_id:
        dialogs = DialogService.query(status=StatusEnum.VALID.value, order_by=DialogService.model.create_time, reverse=False)
        if dialogs:
            default_dialog_id = dialogs[0].id
    return get_json_result(
        data={
            "default_dialog_id": default_dialog_id,
            "query_rewrite_enabled": str(os.environ.get("FEISHU_QUERY_REWRITE_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"},
        }
    )


# Page routes
app.add_url_rule("/admin", view_func=admin_ui, methods=["GET"])
app.add_url_rule("/admin/assets/<path:path>", view_func=admin_asset, methods=["GET"])

# Admin APIs
app.add_url_rule("/api/admin/dashboard", view_func=admin_dashboard, methods=["GET"])
app.add_url_rule("/api/admin/defaults", view_func=admin_defaults, methods=["GET"])
app.add_url_rule("/api/admin/sessions", view_func=admin_sessions, methods=["GET"])
app.add_url_rule("/api/admin/sessions/<path:session_id>", view_func=admin_session_detail, methods=["GET"])
app.add_url_rule("/api/admin/evals", view_func=admin_evals, methods=["GET"])
app.add_url_rule("/api/admin/evals/latest", view_func=admin_evals_latest, methods=["GET"])
app.add_url_rule("/api/admin/evals/compare", view_func=admin_evals_compare, methods=["GET"])
app.add_url_rule("/api/admin/kbs", view_func=admin_kbs, methods=["GET"])
app.add_url_rule("/api/admin/kbs", view_func=admin_kb_create, methods=["POST"])
app.add_url_rule("/api/admin/kbs/<kb_id>", view_func=admin_kb_detail, methods=["GET"])
app.add_url_rule("/api/admin/kbs/<kb_id>", view_func=admin_kb_update, methods=["PUT"])
app.add_url_rule("/api/admin/kbs/<kb_id>", view_func=admin_kb_delete, methods=["DELETE"])
app.add_url_rule("/api/admin/documents", view_func=admin_documents, methods=["GET"])
app.add_url_rule("/api/admin/documents/upload", view_func=admin_document_upload, methods=["POST"])
app.add_url_rule("/api/admin/documents/<doc_id>", view_func=admin_document_detail, methods=["GET"])
app.add_url_rule("/api/admin/documents/<doc_id>/reparse", view_func=admin_document_reparse, methods=["POST"])
app.add_url_rule("/api/admin/documents/<doc_id>", view_func=admin_document_delete, methods=["DELETE"])
app.add_url_rule("/api/admin/acl-debug", view_func=admin_acl_debug, methods=["GET"])
