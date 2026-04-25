from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _state_path() -> Path:
    return Path(os.environ.get("FEISHU_KB_SELECTION_FILE", str(_repo_root() / "logs" / "feishu_kb_selection.json")))


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {"selections": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"selections": {}}
        if not isinstance(data.get("selections"), dict):
            data["selections"] = {}
        return data
    except Exception:
        logging.exception("[feishu/kb-selection] failed to load state path=%s", path)
        return {"selections": {}}


def _save_state(data: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _norm_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def build_selection_key(context: Any) -> str:
    ctx = context if isinstance(context, dict) else {}
    chat_id = str(ctx.get("chat_id") or "").strip()
    open_id = str(ctx.get("open_id") or "").strip()
    user_id = str(ctx.get("user_id") or "").strip()
    if chat_id and open_id:
        return f"chat_id:{chat_id}:open_id:{open_id}"
    if open_id:
        return f"open_id:{open_id}"
    if user_id:
        return f"user_id:{user_id}"
    return ""


def get_selection(selection_key: str) -> dict[str, Any]:
    key = str(selection_key or "").strip()
    if not key:
        return {}
    state = _load_state()
    raw = state.get("selections", {}).get(key)
    if not isinstance(raw, dict):
        return {}
    return {
        "selected_kb_ids": _norm_list(raw.get("selected_kb_ids")),
        "selected_kb_names": _norm_list(raw.get("selected_kb_names")),
        "updated_at": str(raw.get("updated_at") or ""),
    }


def set_selection(selection_key: str, kb_ids: list[str], kb_names: list[str]) -> dict[str, Any]:
    key = str(selection_key or "").strip()
    if not key:
        raise ValueError("selection_key is required")
    selected_kb_ids = _norm_list(kb_ids)
    selected_kb_names = _norm_list(kb_names)
    state = _load_state()
    item = {
        "selected_kb_ids": selected_kb_ids,
        "selected_kb_names": selected_kb_names,
        "updated_at": _now_iso(),
    }
    state.setdefault("selections", {})[key] = item
    _save_state(state)
    return item


def clear_selection(selection_key: str) -> bool:
    key = str(selection_key or "").strip()
    if not key:
        return False
    state = _load_state()
    selections = state.setdefault("selections", {})
    existed = key in selections
    selections.pop(key, None)
    _save_state(state)
    return existed


def list_selections() -> list[dict[str, Any]]:
    state = _load_state()
    rows: list[dict[str, Any]] = []
    for key, raw in state.get("selections", {}).items():
        if not isinstance(raw, dict):
            continue
        rows.append(
            {
                "selection_key": key,
                "selected_kb_ids": _norm_list(raw.get("selected_kb_ids")),
                "selected_kb_names": _norm_list(raw.get("selected_kb_names")),
                "updated_at": str(raw.get("updated_at") or ""),
            }
        )
    rows.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return rows


def filter_selected_kbs(acl_filtered_kb_ids: list[str], selection: dict[str, Any] | None) -> list[str]:
    allowed = _norm_list(acl_filtered_kb_ids)
    selected = _norm_list((selection or {}).get("selected_kb_ids"))
    if not selected:
        return allowed
    selected_set = set(selected)
    return [kb_id for kb_id in allowed if kb_id in selected_set]
