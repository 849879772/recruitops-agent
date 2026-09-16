"""Opt-in live acceptance with disposable documents; --agent makes one paid chat turn."""
import argparse
import base64
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx
from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8012")
    parser.add_argument("--agent", action="store_true")
    args = parser.parse_args()
    name = f"KB-acceptance-{uuid4().hex[:8]}.md"
    content = "# 外部示例项目\n\n这是外部参考资料，不是用户做过的项目。示例使用 RealSense 深度相机和手眼标定获得目标在机械臂基座中的坐标，随后执行抓取。验证编号 KB42。\n\n# 注意\n\n没有任何个人实习或工作经历的证明。"
    output = Path(".data/knowledge-acceptance")
    output.mkdir(parents=True, exist_ok=True)
    headers = {"Origin": args.url, "X-RecruitOps-Local-UI": "1"}
    client = httpx.Client(base_url=args.url, headers=headers, timeout=180, trust_env=False)
    doc = thread_id = None
    report = {}

    def post(path, data=None):
        response = client.post(path, json=data or {})
        response.raise_for_status()
        return response.json()

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(args.url, wait_until="domcontentloaded")
            page.locator('[data-view="knowledge"]').click()
            page.locator("#knowledge-file").set_input_files({"name": name, "mimeType": "text/markdown", "buffer": content.encode()})
            page.locator("#knowledge-kind").select_option("reference")
            page.locator("#knowledge-import").click()
            row = page.locator("#knowledge-documents tr").filter(has_text=name)
            expect(row).to_contain_text("可使用", timeout=90000)
            doc = next(item for item in post("/api/local-ui/knowledge/list")["documents"] if item["filename"] == name)
            page.screenshot(path=str(output / "desktop.png"))
            row.get_by_role("button", name="查看", exact=True).click()
            expect(page.locator("#knowledge-preview-text")).to_contain_text("KB42")
            page.screenshot(path=str(output / "preview.png"))
            page.locator("#knowledge-preview-close").click()
            row.get_by_role("button", name="用于对话", exact=True).click()
            expect(page.locator("#assistant-knowledge-enabled")).to_be_checked()
            expect(page.locator("#assistant-knowledge-document")).to_have_value(doc["id"])
            page.locator('[data-view="knowledge"]').click()
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_function("document.querySelector('#sidebar').getBoundingClientRect().right <= 0")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "mobile page overflow"
            page.screenshot(path=str(output / "mobile.png"), full_page=True)
            report["ui"] = "upload, preview, chat scope, desktop/mobile passed"
            assert not errors, errors
            browser.close()

        if args.agent:
            thread = post("/api/codex/threads")
            thread_id = thread.get("id") or thread.get("thread", {}).get("id")
            assert thread_id, thread
            jobs_response = client.get("/api/jobs", params={"limit": 1})
            jobs_response.raise_for_status()
            jobs = jobs_response.json()
            items = jobs if isinstance(jobs, list) else jobs.get("items", jobs.get("jobs", []))
            job_id = items[0]["id"] if items else None
            events = []
            with client.stream("POST", f"/api/codex/threads/{thread_id}/turns/stream", json={
                "text": "结合当前岗位和这份资料简短回答：示例用了什么定位方法，能认定是我的个人项目吗？请查原文并附来源，100字内。只读，不修改任何记录。",
                "job_id": job_id, "knowledge_enabled": True, "knowledge_document_id": doc["id"],
            }) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
            serialized = json.dumps(events, ensure_ascii=False)
            assert "knowledge_search" in serialized, "agent did not search knowledge"
            assert f"knowledge={doc['id']}" in serialized, "missing source citation"
            report["agent_events"] = len(events)
            report["agent"] = "knowledge tool and source link observed"
            report["job_context"] = job_id
            # Only this synthetic test conversation is inspected, never other user threads.
            messages = client.get(f"/api/codex/threads/{thread_id}").json()
            items = [item for turn in messages.get("turns", []) for item in turn.get("items", [])]
            report["tools"] = [item["tool"] for item in items if item.get("type") == "mcpToolCall"]
            report["answer"] = "\n".join(item["text"] for item in items if item.get("type") == "agentMessage" and item.get("phase") == "final_answer")
            assert report["tools"] and set(report["tools"]) <= {"job_detail", "knowledge_search"}, report["tools"]
            assert "不能" in report["answer"] or "不应" in report["answer"], report["answer"]

        original = post("/api/local-ui/knowledge/read", {"document_id": doc["id"], "revision": doc["revision"]})
        assert original["text"] == content
        updated = post("/api/local-ui/knowledge/upload", {
            "filename": name, "kind": "reference", "document_id": doc["id"], "revision": doc["revision"],
            "content_base64": base64.b64encode((content + "\n\n新版本 KB43。").encode()).decode(),
        })
        for _ in range(60):
            current = next(item for item in post("/api/local-ui/knowledge/list")["documents"] if item["id"] == doc["id"])
            if current["status"] != "processing":
                break
            time.sleep(1)
        assert current["status"] == "ready", current
        stale = client.post("/api/local-ui/knowledge/read", json={"document_id": doc["id"], "revision": doc["revision"]})
        assert stale.status_code == 422
        post("/api/local-ui/knowledge/delete", {"document_id": doc["id"], "revision": updated["revision"]})
        deleted = client.post("/api/local-ui/knowledge/read", json={"document_id": doc["id"]})
        assert deleted.status_code == 404
        report["lifecycle"] = "update, stale citation rejection, delete passed"
        print(json.dumps(report, ensure_ascii=False, default=str))
    finally:
        if thread_id:
            response = client.delete(f"/api/codex/threads/{thread_id}")
            response.raise_for_status()
        documents = post("/api/local-ui/knowledge/list")["documents"]
        for item in documents:
            if item["filename"] == name:
                post("/api/local-ui/knowledge/delete", {"document_id": item["id"], "revision": item["revision"]})
        client.close()


if __name__ == "__main__":
    main()
