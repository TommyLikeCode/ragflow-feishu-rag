from __future__ import annotations

import html
import re
from typing import Any

SOURCE_HEADING = "本回答基于以下资料生成："
UNKNOWN_DOC = "未知文档"
EMPTY_SNIPPET = "暂无片段摘要"


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean_text(value: Any, max_len: int | None = None) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"^[;；]+\s*", "", text)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"([。！？!?])\1+", r"\1", text)
    if max_len and len(text) > max_len:
        return text[: max(1, max_len - 1)].rstrip() + "…"
    return text


def _repair_sentence_boundaries(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s*[\r\n]+\s*", "；", text)
    # Break markdown headings into sentence boundaries so snippets can be extracted from inner sections.
    text = re.sub(r"\s*#{1,6}\s*", "；", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])(?=Feishu\s+Debug\s+Dialog\b)", "；", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])(?=RAGFlow\b)", "；", text)
    text = re.sub(r"(?<=[a-z])(?=[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)", "；", text)
    text = re.sub(r"[;；]\s*[;；]+", "；", text)
    return _clean_text(text)


def _split_sentences(text: str) -> list[str]:
    repaired = _repair_sentence_boundaries(text)
    if not repaired:
        return []
    parts = re.split(r"(?<=[。！？!?\.])\s+|[;；]\s*", repaired)
    return [p.strip(" 。！？!?.;；") for p in parts if p.strip(" 。！？!?.;；")]


def _strip_markdown_heading_prefix(text: str) -> str:
    if not text:
        return ""
    cleaned = text
    cleaned = re.sub(
        r"^\s*#{1,6}\s*.*?(?=(?:[A-Za-z\u4e00-\u9fff][^，。；;!?！？]{0,20}(?:是|支持)))",
        "",
        cleaned,
    )
    cleaned = re.sub(r"^\s*#{1,6}\s*[^#\s]{1,20}\s*", "", cleaned)
    cleaned = re.sub(r"^\s*[A-Za-z0-9_\-\u4e00-\u9fff]{1,20}\s*(平台简介|简介)\s*", "", cleaned)
    cleaned = re.sub(r"\s*#{1,6}\s*(核心能力|适用场景|测试问题建议)\s*", "；", cleaned)
    return _clean_text(cleaned)


def _answer_priority_tokens(answer_text: str) -> list[str]:
    tokens: list[str] = []
    normalized = _clean_text(answer_text).lower()
    keyword_map = [
        ("飞书", "飞书"),
        ("discord", "discord"),
        ("telegram", "telegram"),
        ("whatsapp", "whatsapp"),
    ]
    for token, probe in keyword_map:
        if probe in normalized:
            tokens.append(token)
    return tokens


def _is_noise_sentence(text: str) -> bool:
    sentence = _clean_text(text)
    if not sentence:
        return True
    noisy_tokens = [
        "这是一份用于测试",
        "上传、解析、检索",
        "文档用途",
        "企业知识库测试文档",
    ]
    return any(token in sentence for token in noisy_tokens)


def _snippet_match_score(snippet: str, answer_text: str) -> int:
    sentence = _clean_text(snippet)
    if not sentence:
        return -100
    tokens = _answer_priority_tokens(answer_text)
    s = sentence.lower()
    score = sum(1 for token in tokens if token.lower() in s)
    if "支持" in sentence:
        score += 1
    if _is_noise_sentence(sentence):
        score -= 2
    return score


def _best_sentence_by_answer_keywords(sentences: list[str], answer_text: str) -> str:
    tokens = _answer_priority_tokens(answer_text)
    if not sentences or not tokens:
        return ""

    best_sentence = ""
    best_score = -100
    for sentence in sentences:
        score = _snippet_match_score(sentence, answer_text)
        if score > best_score:
            best_score = score
            best_sentence = sentence

    if best_score <= 0:
        return ""
    return _strip_markdown_heading_prefix(best_sentence)


def _choose_relevant_snippet(snippet: str, answer: str, max_snippet_len: int) -> str:
    repaired = _repair_sentence_boundaries(snippet)
    answer_text = _clean_text(answer).lower()
    sentences = _split_sentences(repaired)

    keyword_matched = _best_sentence_by_answer_keywords(sentences, answer)
    if keyword_matched:
        return _clean_text(keyword_matched, max_snippet_len)

    priorities: list[str] = []
    if "ragflow" in answer_text:
        priorities.append("ragflow")
    if "feishu debug dialog" in answer_text:
        priorities.append("feishu debug dialog")

    for token in priorities:
        for sentence in sentences:
            if token in sentence.lower():
                return _clean_text(sentence, max_snippet_len)

    if len(sentences) > 1:
        joined = "；".join(sentences)
        return _clean_text(_strip_markdown_heading_prefix(joined), max_snippet_len)
    return _clean_text(_strip_markdown_heading_prefix(repaired), max_snippet_len)


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _score_of(ref: dict[str, Any]) -> float | None:
    for key in ["score", "similarity", "vector_similarity", "sim", "rank_score", "relevance_score"]:
        score = _to_float(ref.get(key))
        if score is not None:
            return score
    return None


def _doc_name_of(ref: dict[str, Any], doc_names: dict[str, str]) -> str:
    doc_id = _clean_text(ref.get("doc_id") or ref.get("document_id"))
    raw = (
        ref.get("document_name")
        or ref.get("doc_name")
        or ref.get("docnm_kwd")
        or ref.get("docnm")
        or ref.get("name")
        or doc_names.get(doc_id)
        or UNKNOWN_DOC
    )
    return _clean_text(raw) or UNKNOWN_DOC


def _snippet_of(ref: dict[str, Any], max_snippet_len: int, answer: str = "") -> str:
    raw = (
        ref.get("content")
        or ref.get("snippet")
        or ref.get("text")
        or ref.get("content_with_weight")
        or ref.get("content_ltks")
        or ref.get("summary")
        or ""
    )
    snippet = _choose_relevant_snippet(str(raw), answer, max_snippet_len)
    return snippet or EMPTY_SNIPPET


def _kb_id_of(ref: dict[str, Any]) -> str:
    return _clean_text(ref.get("dataset_id") or ref.get("kb_id") or ref.get("knowledgebase_id"))


def _chunk_id_of(ref: dict[str, Any]) -> str:
    return _clean_text(ref.get("chunk_id") or ref.get("id") or ref.get("doc_id"))


def _extract_reference_payload(answer_or_response: Any) -> tuple[str, Any]:
    if not isinstance(answer_or_response, dict):
        return "", answer_or_response
    answer = _clean_text(answer_or_response.get("answer"))
    ref = answer_or_response.get("reference", {})
    if ref:
        return answer, ref
    refs = answer_or_response.get("references")
    return answer, refs


def _flatten_references(reference_payload: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
    doc_names: dict[str, str] = {}
    refs: list[dict[str, Any]] = []

    def add_doc_agg(item: Any) -> None:
        if not isinstance(item, dict):
            return
        doc_id = _clean_text(item.get("doc_id") or item.get("document_id"))
        doc_name = _clean_text(item.get("doc_name") or item.get("document_name") or item.get("docnm_kwd") or item.get("name"))
        if doc_id and doc_name:
            doc_names[doc_id] = doc_name

    def add_ref(item: Any) -> None:
        if isinstance(item, dict):
            refs.append(item)

    if isinstance(reference_payload, dict):
        for item in _as_list(reference_payload.get("doc_aggs")):
            add_doc_agg(item)
        for key in ["chunks", "references", "chunk_list", "docs"]:
            value = reference_payload.get(key)
            if isinstance(value, list):
                for item in value:
                    add_ref(item)
                break
    elif isinstance(reference_payload, list):
        for item in reference_payload:
            if isinstance(item, dict) and isinstance(item.get("chunks"), list):
                for agg in _as_list(item.get("doc_aggs")):
                    add_doc_agg(agg)
                for chunk in item.get("chunks", []):
                    add_ref(chunk)
            else:
                add_ref(item)

    return refs, doc_names


def _citation_indexes(answer: str) -> list[int]:
    indexes: list[int] = []
    for match in re.finditer(r"\[ID:(\d+)\]", answer or ""):
        idx = int(match.group(1))
        if idx not in indexes:
            indexes.append(idx)
    return indexes


def normalize_references(answer_or_response: Any, max_snippet_len: int = 100) -> list[dict[str, Any]]:
    answer, payload = _extract_reference_payload(answer_or_response)
    refs, doc_names = _flatten_references(payload)
    if not refs:
        return []

    candidates: list[dict[str, Any]] = []
    for idx, ref in enumerate(refs):
        if not isinstance(ref, dict):
            continue
        score = _score_of(ref)
        snippet = _snippet_of(ref, max_snippet_len, answer)
        candidates.append(
            {
                "source_index": idx,
                "source_indexes": [idx],
                "source_label": "",
                "doc_name": _doc_name_of(ref, doc_names),
                "snippet": snippet,
                "score": score,
                "chunk_id": _chunk_id_of(ref),
                "kb_id": _kb_id_of(ref),
                "_snippet_match": _snippet_match_score(snippet, answer),
                "_snippet_len": len(_clean_text(snippet)),
                "_doc_key": _clean_text(ref.get("doc_id") or ref.get("document_id")) or _doc_name_of(ref, doc_names),
                "_order": idx,
            }
        )

    cited = _citation_indexes(answer)
    cited_rank = {idx: rank for rank, idx in enumerate(cited)}
    if cited:
        candidates.sort(
            key=lambda x: (
                0 if x["source_index"] in cited_rank else 1,
                cited_rank.get(x["source_index"], 10**6),
                -x.get("_snippet_match", -100),
                x.get("_snippet_len", 10**6),
                x["_order"],
            )
        )
    else:
        candidates.sort(
            key=lambda x: (
                -x.get("_snippet_match", -100),
                x.get("_snippet_len", 10**6),
                -(x["score"] if x["score"] is not None else -1.0),
                x["_order"],
            )
        )

    deduped: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for item in candidates:
        snippet_key = _clean_text(item.get("snippet", "")).lower()
        key = snippet_key if snippet_key and snippet_key != EMPTY_SNIPPET.lower() else (item["_doc_key"] or item["doc_name"])
        if key in seen:
            seen[key]["source_indexes"].append(item["source_index"])
            if seen[key]["snippet"] == EMPTY_SNIPPET and item["snippet"] != EMPTY_SNIPPET:
                seen[key]["snippet"] = item["snippet"]
            continue
        seen[key] = item
        deduped.append(item)

    for display_idx, item in enumerate(deduped, start=1):
        item["source_label"] = f"来源{display_idx}"
        item.pop("_snippet_match", None)
        item.pop("_snippet_len", None)
        item.pop("_doc_key", None)
        item.pop("_order", None)
    return deduped


def replace_id_citations(answer_text: str, normalized_sources: list[dict[str, Any]]) -> str:
    if not answer_text:
        return ""
    by_index: dict[int, str] = {}
    for source in normalized_sources or []:
        label = source.get("source_label") or ""
        for idx in source.get("source_indexes") or [source.get("source_index")]:
            if isinstance(idx, int) and label:
                by_index[idx] = label

    def repl(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        label = by_index.get(idx) or by_index.get(idx - 1)
        if not label and len(normalized_sources or []) == 1:
            label = normalized_sources[0].get("source_label")
        return f"[{label or f'来源{idx + 1}'}]"

    return re.sub(r"\[ID:(\d+)\]", repl, answer_text)


def format_answer_with_citations(
    answer: str,
    references: Any,
    max_sources: int = 3,
    max_snippet_len: int = 100,
    max_total_len: int = 3500,
) -> dict[str, Any]:
    normalized_sources = normalize_references({"answer": answer, "reference": references}, max_snippet_len=max_snippet_len)
    answer_text = replace_id_citations(answer or "", normalized_sources)
    visible_sources = normalized_sources[: max(0, max_sources)]
    if not visible_sources:
        return {"formatted_text": answer_text, "normalized_sources": normalized_sources}

    lines = [SOURCE_HEADING]
    for i, source in enumerate(visible_sources, start=1):
        doc_name = source.get("doc_name") or UNKNOWN_DOC
        snippet = source.get("snippet") or EMPTY_SNIPPET
        label = source.get("source_label") or f"来源{i}"
        lines.append(f"{i}. {doc_name}：{snippet}（{label}）")

    formatted = f"{answer_text}\n\n" + "\n".join(lines)
    if max_total_len and len(formatted) > max_total_len:
        formatted = formatted[: max(1, max_total_len - 1)].rstrip() + "…"
    return {"formatted_text": formatted, "normalized_sources": normalized_sources}
