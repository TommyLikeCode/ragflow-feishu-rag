#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.apps import app
from api.db.services.dialog_service import DialogService
from api.integrations.feishu_query_runner import ask_feishu_kb_question


def _load_eval_set(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, list):
        raise ValueError("Eval set file must be a JSON array")
    normalized: list[dict[str, Any]] = []
    for i, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Eval case at index {i - 1} must be object")
        question = (item.get("question") or "").strip()
        if not question:
            raise ValueError(f"Eval case at index {i - 1} missing question")
        normalized.append(
            {
                "case_id": item.get("case_id") or f"case_{i}",
                "question": question,
                "expected_answer_keywords": item.get("expected_answer_keywords") or [],
                "expected_kb_ids": item.get("expected_kb_ids") or [],
                "expected_source_keywords": item.get("expected_source_keywords") or [],
                "context_seed_question": (item.get("context_seed_question") or "").strip(),
            }
        )
    return normalized


def _extract_references(final_reference: Any) -> list[dict[str, Any]]:
    if isinstance(final_reference, dict):
        for key in ["chunks", "references", "chunk_list", "doc_aggs", "docs"]:
            value = final_reference.get(key)
            if isinstance(value, list):
                return [v for v in value if isinstance(v, dict)]
        return []
    if isinstance(final_reference, list):
        return [v for v in final_reference if isinstance(v, dict)]
    return []


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).lower()


def _match_keywords(text: str, keywords: list[Any]) -> list[str]:
    haystack = _normalize_text(text)
    matched: list[str] = []
    for kw in keywords:
        token = _normalize_text(kw)
        if token and token in haystack:
            matched.append(str(kw))
    return matched


def _collect_source_text(references: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for ref in references:
        doc_name = ref.get("document_name") or ref.get("doc_name") or ref.get("name") or ref.get("docnm_kwd") or ""
        content = ref.get("content") or ref.get("snippet") or ref.get("text") or ref.get("content_with_weight") or ""
        if doc_name:
            chunks.append(str(doc_name))
        if content:
            chunks.append(str(content))
    return "\n".join(chunks)


def _collect_retrieved_kb_ids(references: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for ref in references:
        kb_id = ref.get("dataset_id") or ref.get("kb_id") or ref.get("knowledgebase_id")
        if isinstance(kb_id, str) and kb_id and kb_id not in ids:
            ids.append(kb_id)
    return ids


async def _ask_once(dialog_id: str, feishu_user_id: str, question: str) -> dict[str, Any]:
    return await ask_feishu_kb_question(
        dialog_id=dialog_id,
        feishu_user_id=feishu_user_id,
        question=question,
        session_id="",
        apply_acl=True,
        apply_rewrite=True,
    )


def _all_expected_matched(expected: list[Any], matched: list[str]) -> bool:
    expected_norm = [_normalize_text(v) for v in expected if _normalize_text(v)]
    matched_norm = {_normalize_text(v) for v in matched if _normalize_text(v)}
    if not expected_norm:
        return True
    return all(v in matched_norm for v in expected_norm)


async def _run_eval(dialog_id: str, eval_cases: list[dict[str, Any]]) -> dict[str, Any]:
    exists, dialog = DialogService.get_by_id(dialog_id)
    if not exists or not dialog:
        raise RuntimeError(f"Dialog not found: {dialog_id}")

    feishu_user_id = os.environ.get("FEISHU_EVAL_USER_ID", "ou_test_citation").strip() or "ou_test_citation"

    per_case_results: list[dict[str, Any]] = []
    total_latency = 0.0
    hit_count = 0
    answer_keyword_match_count = 0
    source_keyword_match_count = 0
    failure_reason_counts: dict[str, int] = {}
    pipeline_name = "feishu_iframe_completion"

    for case in eval_cases:
        question = case["question"]
        expected_answer_keywords = case["expected_answer_keywords"]
        expected_source_keywords = case["expected_source_keywords"]
        expected_kb_ids = case["expected_kb_ids"]
        context_seed_question = case.get("context_seed_question", "")

        started_at = time.perf_counter()
        failure_reason = ""
        answer = ""
        references: list[dict[str, Any]] = []
        ask_result: dict[str, Any] = {}
        seeded_session_id = ""

        try:
            if context_seed_question:
                seed_result = await _ask_once(dialog_id, feishu_user_id, context_seed_question)
                seeded_session_id = seed_result.get("session_id", "") if isinstance(seed_result, dict) else ""

            ask_result = await ask_feishu_kb_question(
                dialog_id=dialog_id,
                feishu_user_id=feishu_user_id,
                question=question,
                session_id=seeded_session_id,
                apply_acl=True,
                apply_rewrite=True,
            )
            answer = ask_result.get("answer", "") if isinstance(ask_result, dict) else ""
            references = _extract_references(ask_result.get("reference", {})) if isinstance(ask_result, dict) else []
            pipeline_name = ask_result.get("pipeline_name", pipeline_name) if isinstance(ask_result, dict) else pipeline_name
        except Exception as exc:
            failure_reason = f"ask_failed: {exc}"

        latency_seconds = time.perf_counter() - started_at
        total_latency += latency_seconds

        source_text = _collect_source_text(references)
        matched_answer_keywords = _match_keywords(answer, expected_answer_keywords)
        matched_source_keywords = _match_keywords(source_text, expected_source_keywords)
        retrieved_kb_ids = _collect_retrieved_kb_ids(references)

        answer_ok = _all_expected_matched(expected_answer_keywords, matched_answer_keywords)
        source_ok = _all_expected_matched(expected_source_keywords, matched_source_keywords)
        kb_ok = True
        if expected_kb_ids:
            kb_ok = all(kb in set(retrieved_kb_ids) for kb in expected_kb_ids)

        if answer_ok:
            answer_keyword_match_count += 1
        if source_ok:
            source_keyword_match_count += 1

        success = bool(answer.strip()) and answer_ok and source_ok and kb_ok and not failure_reason
        if not success and not failure_reason:
            reasons = []
            if not answer.strip():
                reasons.append("empty_answer")
            if not answer_ok:
                reasons.append("answer_keywords_not_fully_matched")
            if not source_ok:
                reasons.append("source_keywords_not_fully_matched")
            if not kb_ok:
                reasons.append("expected_kb_ids_not_fully_matched")
            failure_reason = ",".join(reasons)

        if success:
            hit_count += 1
        else:
            reason_key = failure_reason or "unknown"
            failure_reason_counts[reason_key] = failure_reason_counts.get(reason_key, 0) + 1

        per_case_results.append(
            {
                "case_id": case.get("case_id"),
                "question": question,
                "answer": answer,
                "pipeline_name": pipeline_name,
                "context_seed_question": context_seed_question,
                "used_original_query": ask_result.get("used_original_query", question) if isinstance(ask_result, dict) else question,
                "used_retrieval_query": ask_result.get("used_retrieval_query", question) if isinstance(ask_result, dict) else question,
                "seeded_session_id": seeded_session_id,
                "latency_seconds": round(latency_seconds, 4),
                "matched_answer_keywords": matched_answer_keywords,
                "matched_source_keywords": matched_source_keywords,
                "expected_kb_ids": expected_kb_ids,
                "retrieved_kb_ids": retrieved_kb_ids,
                "success": success,
                "failure_reason": failure_reason,
            }
        )

    total_cases = len(eval_cases)
    hit_rate = (hit_count / total_cases) if total_cases else 0.0
    answer_keyword_match_rate = (answer_keyword_match_count / total_cases) if total_cases else 0.0
    source_keyword_match_rate = (source_keyword_match_count / total_cases) if total_cases else 0.0
    avg_latency_seconds = (total_latency / total_cases) if total_cases else 0.0

    return {
        "dialog_id": dialog_id,
        "pipeline_name": pipeline_name,
        "total_cases": total_cases,
        "hit_count": hit_count,
        "hit_rate": round(hit_rate, 4),
        "answer_keyword_match_count": answer_keyword_match_count,
        "answer_keyword_match_rate": round(answer_keyword_match_rate, 4),
        "source_keyword_match_count": source_keyword_match_count,
        "source_keyword_match_rate": round(source_keyword_match_rate, 4),
        "avg_latency_seconds": round(avg_latency_seconds, 4),
        "failure_reason_counts": failure_reason_counts,
        "per_case_results": per_case_results,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Offline minimal evaluator for Feishu RAG answers")
    parser.add_argument(
        "--dialog-id",
        default=os.environ.get("FEISHU_DEFAULT_DIALOG_ID", "").strip(),
        help="Target dialog_id, fallback to FEISHU_DEFAULT_DIALOG_ID",
    )
    parser.add_argument(
        "--eval-set",
        default=str(PROJECT_ROOT / "conf" / "feishu_eval_set.json"),
        help="Path to evaluation set json",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional output json path",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="Run only selected case_id. Repeat this arg for multiple cases.",
    )
    args = parser.parse_args()

    dialog_id = (args.dialog_id or "").strip()
    if not dialog_id:
        raise RuntimeError("dialog_id is required, pass --dialog-id or set FEISHU_DEFAULT_DIALOG_ID")

    eval_set_path = Path(args.eval_set).resolve()
    eval_cases = _load_eval_set(eval_set_path)
    if args.case_id:
        selected = {str(cid).strip() for cid in args.case_id if str(cid).strip()}
        eval_cases = [c for c in eval_cases if str(c.get("case_id", "")).strip() in selected]
        if not eval_cases:
            raise RuntimeError(f"No eval cases selected by --case-id: {sorted(selected)}")

    async with app.app_context():
        result = await _run_eval(dialog_id, eval_cases)

    output_text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output_text, encoding="utf-8")

    print(output_text)


if __name__ == "__main__":
    asyncio.run(main())
