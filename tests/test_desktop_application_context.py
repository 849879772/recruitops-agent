"""Offline tests for desktop application-record frame aggregation."""

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "packages/desktop_filler/index.cjs"


def node_call(expression, value):
    result = subprocess.run(
        [shutil.which("node") or "node", "-e",
         "const a=require(process.argv[1]);const fs=require('node:fs');"
         "const input=JSON.parse(fs.readFileSync(0,'utf8'));"
         f"console.log(JSON.stringify({expression}));", str(ADAPTER)],
        input=json.dumps(value), text=True, encoding="utf-8", capture_output=True,
        cwd=ROOT, timeout=20, check=True,
    )
    return json.loads(result.stdout)


def test_application_context_merges_and_deduplicates_authorized_frames():
    top_url = "https://ats.example/applications"
    child_url = "https://forms.example/jobs"
    samples = [
        {"frameId": 0, "frameUrl": top_url, "result": {"company": "Acme", "url": top_url,
         "titles": ["Platform Engineer"], "records": [{"title": "Platform Engineer", "date": "", "sourceStatus": ""}]}},
        {"frameId": 9, "frameUrl": child_url, "result": {"company": "Wrong", "url": child_url,
         "titles": [" platform   engineer ", "Data Engineer"], "records": [
             {"title": " platform   engineer ", "date": "2026-09-21", "sourceStatus": "已投递"},
             {"title": "Data Engineer", "date": "2026-09-22", "sourceStatus": "笔试"}] }},
        {"frameId": 10, "frameUrl": "https://evil.example/jobs", "result": {"company": "Evil", "url": "https://evil.example/jobs",
         "titles": ["Injected Role"], "records": [{"title": "Injected Role", "date": "", "sourceStatus": ""}]}},
    ]
    merged = node_call("a.mergeApplicationContexts(input.samples,input.url,input.allowed)", {
        "samples": samples, "url": top_url, "allowed": ["https://forms.example"]})
    assert merged == {"company": "Acme", "titles": ["Platform Engineer", "Data Engineer"],
        "records": [{"title": "Platform Engineer", "date": "2026-09-21", "sourceStatus": "已投递"},
                    {"title": "Data Engineer", "date": "2026-09-22", "sourceStatus": "笔试"}], "url": top_url}


def test_application_context_script_checks_frame_visibility_and_binding():
    script = node_call("a.buildApplicationContextScript(a.loadDesktopFiller(),input.context)", {
        "context": {"instanceId": "instance-1", "tabId": "1", "frameId": "2:3",
                    "documentId": "4:https://forms.example/jobs", "profileVersion": "1",
                    "href": "https://forms.example/jobs", "allowSubframe": True}})
    assert "filler_frame_not_visible" in script
    assert "RECRUIT_GET_PAGE_CONTEXT" in script
    assert "type:'RESUME_FILL'" not in script


def test_application_context_merge_rejects_ungranted_cross_origin_frame():
    page_url = "https://ats.example/applications"
    frame_url = "https://forms.example/jobs"
    sample = {"frameId": 2, "frameUrl": frame_url, "result": {"company": "", "url": frame_url,
              "titles": ["Data Engineer"], "records": [{"title": "Data Engineer", "date": "", "sourceStatus": ""}]}}
    merged = node_call("a.mergeApplicationContexts(input.samples,input.url)", {"samples": [sample], "url": page_url})
    assert merged["titles"] == []
