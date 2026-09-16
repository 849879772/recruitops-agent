"""Anonymous, bounded public-source snapshot; never imports or scores jobs."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import requests

ENDPOINT = "https://offerbiu.com/api/recruitment/postings"
GROUPS = ("internet-tech", "manufacturing-equipment", "auto-transport-equipment")


def capture(output: Path, *, max_pages=150, delay=0.6, session=None):
    output.mkdir(parents=True, exist_ok=True)
    client = session or requests.Session()
    client.trust_env = False
    params = [("seasonYear", "2027"), ("recruitType", "秋招"),
              *[("industryGroup", group) for group in GROUPS], ("size", "9")]
    result = {"source": "offerbiu", "source_url": ENDPOINT, "read_only": True,
              "authenticated": False, "captured_at": datetime.now(timezone.utc).isoformat(),
              "filters": {"seasonYear": 2027, "recruitType": "秋招", "industryGroups": list(GROUPS)},
              "pages": [], "items": [], "complete": False, "stop_reason": None}
    seen = set()
    baseline = None
    try:
        for page in range(max_pages):
            # Use a fresh cookie jar on every public request, including after server Set-Cookie.
            client.cookies.clear()
            response = client.get(ENDPOINT, params=[*params, ("page", str(page))],
                                  timeout=20, allow_redirects=False)
            if response.status_code != 200:
                result["stop_reason"] = f"http_{response.status_code}"
                break
            payload = response.json()
            data = payload.get("data") or {}
            if payload.get("success") is not True or not isinstance(data.get("items"), list):
                result["stop_reason"] = "invalid_or_restricted_response"
                break
            if data.get("previewLimited") is not False:
                result["stop_reason"] = "preview_or_access_limit"
                break
            meta = {key: data.get(key) for key in ("page", "size", "totalItems", "totalPages", "previewLimited")}
            if meta["page"] != page or not isinstance(meta["totalPages"], int):
                result["stop_reason"] = "pagination_mismatch"
                break
            if baseline is None:
                baseline = meta
            if (meta["totalItems"], meta["totalPages"]) != (baseline["totalItems"], baseline["totalPages"]):
                result["stop_reason"] = "source_changed_during_capture"
                break
            rows = data["items"]
            if any(not row.get("id") or row["id"] in seen for row in rows):
                result["stop_reason"] = "duplicate_or_missing_record_id"
                break
            if len({row["id"] for row in rows}) != len(rows):
                result["stop_reason"] = "duplicate_page_rows"
                break
            if any(row.get("recruitType") != "秋招" or 2027 not in row.get("targetYears", [])
                   or not set(row.get("industryGroupCodes", [])).intersection(GROUPS) for row in rows):
                result["stop_reason"] = "source_filter_mismatch"
                break
            (output / f"page-{page:04d}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            seen.update(row["id"] for row in rows)
            result["pages"].append(meta)
            result["items"].extend(rows)
            print(json.dumps({"page": page, "pages": meta["totalPages"], "records": len(seen)}), flush=True)
            if page + 1 >= meta["totalPages"]:
                result["complete"] = len(seen) == meta["totalItems"]
                result["stop_reason"] = "complete" if result["complete"] else "total_mismatch"
                break
            if not rows:
                result["stop_reason"] = "unexpected_empty_page"
                break
            time.sleep(delay)
        else:
            result["stop_reason"] = "page_budget_exhausted"
    except (requests.RequestException, ValueError, TypeError) as exc:
        result["stop_reason"] = type(exc).__name__
    finally:
        if session is None:
            client.close()
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        (output / "snapshot.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-pages", type=int, default=150)
    args = parser.parse_args()
    result = capture(args.output_dir, max_pages=max(1, min(150, args.max_pages)))
    print(json.dumps({k: v for k, v in result.items() if k not in {"items", "pages"}}, ensure_ascii=True))
