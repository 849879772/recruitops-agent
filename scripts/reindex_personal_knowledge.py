"""Rebuild only mismatched personal document indexes; no business-table writes."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.personal_knowledge import get_knowledge_service


def main():
    service = get_knowledge_service()
    result = {"rebuilt": 0, "unchanged": 0, "not_ready": 0, "failed": []}
    for doc in service.list_documents():
        if doc["status"] != "ready":
            result["not_ready"] += 1
            continue
        try:
            result["rebuilt" if service.reindex(doc["id"]) else "unchanged"] += 1
        except Exception:
            result["failed"].append(doc["id"])
    print(json.dumps(result))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
