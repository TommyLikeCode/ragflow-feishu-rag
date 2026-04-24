from __future__ import annotations

import os
import re
from typing import Any

_GREETING_PATTERNS = [
    r"^(你好|您好|嗨|hi|hello)[,，。!！\s]*",
    r"^(请问|麻烦|帮我|请你|能否|能不能)[,，。!！\s]*",
]

_FOLLOWUP_HINTS = ["它", "这个", "那个", "上面说的", "前面说的", "刚才", "上述", "继续", "然后呢"]


def _to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _rule_rewrite(query: str, previous_query: str = "") -> tuple[str, str]:
    q = _normalize_spaces(query)

    for pattern in _GREETING_PATTERNS:
        q = re.sub(pattern, "", q, flags=re.IGNORECASE)
    q = _normalize_spaces(q)

    prev = _normalize_spaces(previous_query)
    is_followup = any(token in q for token in _FOLLOWUP_HINTS)
    is_short = len(q) <= int(os.environ.get("FEISHU_QUERY_REWRITE_SHORT_LEN", "8"))

    if (is_followup or is_short) and prev:
        rewritten = f"{prev}。补充问题：{q}"
        return _normalize_spaces(rewritten), "rule_followup_completion"

    return q, "rule_normalize"


def rewrite_for_retrieval(original_query: str, previous_query: str = "") -> dict[str, Any]:
    enabled = _to_bool(os.environ.get("FEISHU_QUERY_REWRITE_ENABLED", "1"), default=True)
    use_llm = _to_bool(os.environ.get("FEISHU_QUERY_REWRITE_USE_LLM", "0"), default=False)

    original = _normalize_spaces(original_query)
    if not enabled:
        return {
            "original_query": original,
            "rewritten_query": original,
            "rewrite_applied": False,
            "strategy": "disabled",
            "llm_attempted": False,
        }

    rewritten, strategy = _rule_rewrite(original, previous_query)

    # MVP: keep LLM rewrite optional but disabled by default. Hook reserved for future extension.
    llm_attempted = False
    if use_llm:
        llm_attempted = True

    rewrite_applied = rewritten != original
    return {
        "original_query": original,
        "rewritten_query": rewritten,
        "rewrite_applied": rewrite_applied,
        "strategy": strategy,
        "llm_attempted": llm_attempted,
    }
