from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

VALID_SCOPES = {"public", "department", "personal"}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _config_path() -> Path:
    return Path(os.environ.get("FEISHU_KB_ACL_FILE", str(_repo_root() / "conf" / "feishu_kb_acl.json")))


def _load_acl_config() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        logging.exception("[feishu/kb-acl] failed to load acl config path=%s", path)
        return {}


def get_acl_config() -> dict[str, Any]:
    cfg = _load_acl_config()
    if not isinstance(cfg.get("external_user_map"), dict):
        cfg["external_user_map"] = {}
    if not isinstance(cfg.get("internal_user_department_map"), dict):
        cfg["internal_user_department_map"] = {}
    if not isinstance(cfg.get("kb_policies"), dict):
        cfg["kb_policies"] = {}
    return cfg


def save_acl_config(config: dict[str, Any]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def upsert_kb_policy(
    kb_id: str,
    scope: str,
    owner_user_id: str = "",
    department_id: str = "",
    allowed_user_ids: Any = None,
    denied_user_ids: Any = None,
) -> dict[str, Any]:
    cfg = get_acl_config()
    policies = cfg.get("kb_policies") or {}
    policy = normalize_kb_policy(
        {
            "scope": scope,
            "owner_user_id": owner_user_id,
            "department_id": department_id,
            "allowed_user_ids": allowed_user_ids,
            "denied_user_ids": denied_user_ids,
        }
    )
    policies[kb_id] = policy
    cfg["kb_policies"] = policies
    save_acl_config(cfg)
    return policy


def remove_kb_policy(kb_id: str) -> None:
    cfg = get_acl_config()
    policies = cfg.get("kb_policies") or {}
    if kb_id in policies:
        del policies[kb_id]
        cfg["kb_policies"] = policies
        save_acl_config(cfg)


def _norm_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _norm_user_list(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = value.split(",")
    else:
        items = []

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        user_id = _norm_str(item)
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        out.append(user_id)
    return out


def normalize_kb_policy(policy: Any) -> dict[str, Any]:
    raw = policy if isinstance(policy, dict) else {}
    scope = _norm_str(raw.get("scope")).lower() or "public"
    if scope not in VALID_SCOPES:
        scope = "public"

    return {
        "scope": scope,
        "owner_user_id": _norm_str(raw.get("owner_user_id")),
        "department_id": _norm_str(raw.get("department_id")),
        "allowed_user_ids": _norm_user_list(raw.get("allowed_user_ids")),
        "denied_user_ids": _norm_user_list(raw.get("denied_user_ids")),
    }


def _policy_or_none(policies: dict[str, Any], kb_id: str) -> dict[str, Any] | None:
    raw = policies.get(kb_id)
    if not isinstance(raw, dict):
        return None
    return normalize_kb_policy(raw)


def resolve_feishu_user_context(feishu_user_id: str) -> dict[str, str]:
    cfg = _load_acl_config()
    ext_map = cfg.get("external_user_map") if isinstance(cfg.get("external_user_map"), dict) else {}
    dept_map = cfg.get("internal_user_department_map") if isinstance(cfg.get("internal_user_department_map"), dict) else {}

    internal_user_id = _norm_str(ext_map.get(feishu_user_id)) or feishu_user_id
    department_id = _norm_str(dept_map.get(internal_user_id))

    return {
        "feishu_user_id": feishu_user_id,
        "internal_user_id": internal_user_id,
        "department_id": department_id,
    }


def evaluate_kb_acl_for_user(feishu_user_id: str, kb_ids: list[str]) -> dict[str, Any]:
    cfg = _load_acl_config()
    policies = cfg.get("kb_policies") if isinstance(cfg.get("kb_policies"), dict) else {}

    user_ctx = resolve_feishu_user_context(feishu_user_id)
    internal_user_id = user_ctx["internal_user_id"]
    department_id = user_ctx["department_id"]

    original_kb_ids = [kb_id for kb_id in (kb_ids or []) if isinstance(kb_id, str) and kb_id]
    filtered_kb_ids: list[str] = []
    denied_kb_ids: list[str] = []
    per_kb_decisions: list[dict[str, Any]] = []

    for kb_id in original_kb_ids:
        policy = _policy_or_none(policies, kb_id)
        if policy is None:
            policy = {
                "scope": "no_policy",
                "owner_user_id": "",
                "department_id": "",
                "allowed_user_ids": [],
                "denied_user_ids": [],
            }
            allow = False
            reason = "no_policy"
            matched_rule = "no_policy"
        else:
            scope = policy["scope"]
            owner_user_id = policy["owner_user_id"]
            policy_department_id = policy["department_id"]
            allowed_user_ids = policy["allowed_user_ids"]
            denied_user_ids = policy["denied_user_ids"]

            if internal_user_id and internal_user_id in denied_user_ids:
                allow = False
                reason = "explicit_deny_user"
                matched_rule = "explicit_deny_user"
            elif internal_user_id and internal_user_id in allowed_user_ids:
                allow = True
                reason = "explicit_allow_user"
                matched_rule = "explicit_allow_user"
            elif scope == "public":
                allow = True
                reason = "public"
                matched_rule = "public"
            elif scope == "department":
                allow = bool(department_id) and policy_department_id == department_id
                reason = "department_match" if allow else "department_mismatch"
                matched_rule = reason
            elif scope == "personal":
                allow = bool(internal_user_id) and owner_user_id == internal_user_id
                reason = "owner_match" if allow else "owner_mismatch"
                matched_rule = reason
            else:
                allow = False
                reason = "no_policy"
                matched_rule = "no_policy"

        scope = policy["scope"]
        owner_user_id = policy["owner_user_id"]
        policy_department_id = policy["department_id"]
        allowed_user_ids = policy["allowed_user_ids"]
        denied_user_ids = policy["denied_user_ids"]

        decision = {
            "kb_id": kb_id,
            "scope": scope,
            "allow": allow,
            "reason": reason,
            "matched_rule": matched_rule,
            "owner_user_id": owner_user_id,
            "department_id": policy_department_id,
            "allowed_user_ids": allowed_user_ids,
            "denied_user_ids": denied_user_ids,
        }
        per_kb_decisions.append(decision)

        if allow:
            filtered_kb_ids.append(kb_id)
        else:
            denied_kb_ids.append(kb_id)

    return {
        **user_ctx,
        "external_user": user_ctx["feishu_user_id"],
        "internal_user": user_ctx["internal_user_id"],
        "original_kb_ids": original_kb_ids,
        "filtered_kb_ids": filtered_kb_ids,
        "denied_kb_ids": denied_kb_ids,
        "per_kb_decisions": per_kb_decisions,
    }


def filter_kb_ids_for_user(feishu_user_id: str, kb_ids: list[str]) -> dict[str, Any]:
    report = evaluate_kb_acl_for_user(feishu_user_id, kb_ids)
    return {
        "feishu_user_id": report["feishu_user_id"],
        "internal_user_id": report["internal_user_id"],
        "department_id": report["department_id"],
        "original_kb_ids": report["original_kb_ids"],
        "filtered_kb_ids": report["filtered_kb_ids"],
        "denied_kb_ids": report["denied_kb_ids"],
    }
