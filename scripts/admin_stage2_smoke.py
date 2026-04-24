#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _get_json(url: str, timeout: int = 10) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("response is not JSON object")
        return payload


def _expect_code_zero(payload: dict[str, Any]) -> Any:
    code = payload.get("code")
    if code != 0:
        raise RuntimeError(f"code != 0, code={code}, message={payload.get('message')}")
    return payload.get("data")


def run_smoke(base_url: str, dialog_id: str, open_id: str) -> list[CheckResult]:
    results: list[CheckResult] = []

    def check(name: str, path: str, validator) -> None:
        try:
            payload = _get_json(base_url.rstrip("/") + path)
            data = _expect_code_zero(payload)
            detail = validator(data)
            results.append(CheckResult(name=name, ok=True, detail=detail))
        except Exception as exc:
            results.append(CheckResult(name=name, ok=False, detail=str(exc)))

    check(
        "admin_dashboard",
        "/api/admin/dashboard",
        lambda d: f"cards={sorted((d or {}).get('cards', {}).keys())}",
    )

    check(
        "admin_kbs_paged",
        "/api/admin/kbs?page=1&page_size=5",
        lambda d: f"total={(d or {}).get('total', 0)},items={len((d or {}).get('items', []))}",
    )

    check(
        "admin_docs_paged",
        "/api/admin/documents?page=1&page_size=5",
        lambda d: f"total={(d or {}).get('total', 0)},items={len((d or {}).get('items', []))}",
    )

    check(
        "admin_sessions",
        "/api/admin/sessions?page=1&page_size=5",
        lambda d: f"total={(d or {}).get('total', 0)},items={len((d or {}).get('items', []))}",
    )

    check(
        "admin_evals_latest",
        "/api/admin/evals/latest",
        lambda d: "latest=present" if (d or {}).get("latest") else "latest=none",
    )

    check(
        "admin_evals_compare",
        "/api/admin/evals/compare",
        lambda d: f"diff_keys={sorted((d or {}).get('diff', {}).keys())}",
    )

    acl_qs = urllib.parse.urlencode({"dialog_id": dialog_id, "open_id": open_id})
    check(
        "admin_acl_debug",
        f"/api/admin/acl-debug?{acl_qs}",
        lambda d: f"orig={len((d or {}).get('original_kb_ids', []))},filtered={len((d or {}).get('filtered_kb_ids', []))}",
    )

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test for Admin Stage2 APIs")
    parser.add_argument("--base-url", default="http://127.0.0.1:9380", help="API base URL")
    parser.add_argument("--dialog-id", required=True, help="dialog_id for ACL debug")
    parser.add_argument("--open-id", default="ou_test_citation", help="open_id for ACL debug")
    args = parser.parse_args()

    results = run_smoke(args.base_url, args.dialog_id, args.open_id)
    failed = [r for r in results if not r.ok]

    print("=== Admin Stage2 Smoke ===")
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        print(f"[{status}] {r.name}: {r.detail}")

    print("---")
    print(f"total={len(results)} pass={len(results) - len(failed)} fail={len(failed)}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
