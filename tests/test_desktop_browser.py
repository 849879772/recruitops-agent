"""Real DOM fixtures, no server, credentials, persistent profile or database."""

import json
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "packages/desktop_browser/index.cjs"
URL = "https://ats.example/applications"
CONTEXT = {"operation_id": "offline-1", "page_url": URL, "application_ids": ["target-1"]}

# Load the pure schema without browser_bridge.__init__ importing DB transports.
SPEC = importlib.util.spec_from_file_location(
    "desktop_browser_fixture_schema", ROOT / "packages/browser_bridge/models.py"
)
SCHEMA = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCHEMA
SPEC.loader.exec_module(SCHEMA)
validate_bridge_payload = SCHEMA.validate_bridge_payload


def node_call(expression, value=None):
    completed = subprocess.run(
        [shutil.which("node") or "node", "-e",
         "const a=require(process.argv[1]); const fs=require('node:fs');"
         "const input=JSON.parse(fs.readFileSync(0,'utf8'));"
         f"console.log(JSON.stringify({expression}));", str(ADAPTER)],
        input=json.dumps(value), text=True, encoding="utf-8", capture_output=True,
        cwd=ROOT, timeout=20, check=True,
    )
    return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        # Playwright creates an ephemeral profile; never attach to existing Edge.
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(service_workers="block")
    context.route("**/*", lambda route: route.fulfill(
        status=200, content_type="text/html", body="<!doctype html><body></body>"
    ))
    page = context.new_page()
    page.goto(URL)
    yield page
    context.close()


def observe(page, html):
    page.set_content(html)
    script = node_call("a.buildObservationScript(input)", {
        "operation_id": CONTEXT["operation_id"], "page_url": URL,
    })
    raw = page.evaluate(script)
    normalized = node_call("a.normalizeObservation(input.raw,input.context)", {
        "raw": raw, "context": CONTEXT,
    })
    validate_bridge_payload(normalized["result"])
    assert normalized["operation_id"] == "offline-1"
    assert normalized["result"]["database_updated"] is False
    assert normalized["result"]["evidence_only"] is True
    return raw, normalized


@pytest.mark.parametrize("filename,code", [
    ("application-status-logged-out.html", "LOGIN_REQUIRED"),
    ("application-status-captcha.html", "CAPTCHA_REQUIRED"),
    ("application-status-overlay.html", "STATE_UNCLEAR"),
])
def test_gate_fixtures(page, filename, code):
    _, result = observe(page, (ROOT / "extension/fixtures" / filename).read_text(encoding="utf-8"))
    assert result["error_code"] == code
    assert result["status"] == "STATE_UNCLEAR"
    assert "entries" not in result["result"]


def test_blank_is_not_applied(page):
    _, result = observe(page, "<body></body>")
    assert result["status"] == "STATE_UNCLEAR"
    assert result["result"]["entries"] == []


def test_real_parser_multiple_cards_and_exact_titles(page):
    html = """<main><section class="cards">
      <article data-recruitops-application><h2>Platform Engineer</h2>
      <p>Current contact: alice@example.com</p>
      <span data-recruitops-application-status="written">Written test</span></article>
      <article data-recruitops-application><h2>Data Engineer</h2>
      <span data-recruitops-application-status="rejected">Rejected</span></article>
      </section></main>"""
    raw, result = observe(page, html)
    records = result["result"]["application_records"]
    assert [(r["title"], r["status"]) for r in records] == [
        ("Platform Engineer", "written"), ("Data Engineer", "rejected")]
    assert "rejected" not in records[0]["evidence"].lower()
    assert "alice@example.com" not in json.dumps(result)
    assert all("application_id" not in entry for entry in result["result"]["entries"])
    # Compare the exact same DOM with the original extension collector.
    page.add_script_tag(path=str(ROOT / "extension/src/application-records.js"))
    direct = page.evaluate("RecruitOpsApplicationRecords.extract(document).records")
    assert [(r["title"], r["status"]) for r in direct] == [(r["title"], r["status"]) for r in raw["data"]["applicationRecords"]]


def test_table_titles_and_status_labels(page):
    raw, result = observe(page, """<table><thead><tr><th>Position</th><th>Status</th><th>Date</th></tr></thead>
      <tbody><tr><td>Platform Engineer</td><td>在线测评</td><td>2026-09-18</td></tr>
      <tr><td>Data Engineer</td><td>笔试</td><td>2026-09-18</td></tr>
      <tr><td>Test Development Engineer</td><td>申请成功</td><td>2026-09-19</td></tr>
      <tr><td>Embodied Model Deployment Engineer</td><td>暂不匹配</td><td>2026-09-19</td></tr></tbody></table>""")
    records = result["result"]["application_records"]
    assert [(r["title"], r["status"]) for r in records] == [
        ("Platform Engineer", "applied"), ("Data Engineer", "written"),
        ("Test Development Engineer", "applied"), ("Embodied Model Deployment Engineer", "rejected")]
    assert [(r["status"], r["label"]) for r in records[-2:]] == [
        ("applied", "申请成功"), ("rejected", "暂不匹配")]

    unbound = node_call("a.normalizeObservation(input.raw,input.context)", {
        "raw": raw, "context": {**CONTEXT, "application_ids": []},
    })
    assert unbound["result"]["application_ids"] == []
    assert "application_id" not in unbound["result"]
    assert [(r["title"], r["status"]) for r in unbound["result"]["application_records"][-2:]] == [
        ("Test Development Engineer", "applied"), ("Embodied Model Deployment Engineer", "rejected")]
    with pytest.raises(subprocess.CalledProcessError):
        node_call("a.normalizeObservation(input.raw,input.context)", {
            "raw": raw, "context": {**CONTEXT, "application_ids": ["target-1", "target-1"]},
        })


def test_standalone_status_entry_accepts_terminal_rejection_labels(page):
    _, result = observe(page, """<article data-recruitops-application>
      <h2>Test Development Engineer</h2><span data-recruitops-application-status>未通过</span>
      </article>""")
    records = result["result"]["application_records"]
    assert [(record["title"], record["status"], record["label"]) for record in records] == [
        ("Test Development Engineer", "rejected", "未通过")]


@pytest.mark.parametrize("src", ["/child", "https://other.example/child"])
def test_frames_are_not_silently_scanned(page, src):
    raw, result = observe(page, f'<p>Applications are embedded below</p><iframe src="{src}"></iframe>')
    # Presence alone cannot establish that application evidence lives in a frame.
    assert result["error_code"] == "STATE_UNCLEAR"
    assert result["result"]["diagnostics"]["iframeCount"] == 1
    assert raw["data"]["applicationRecords"] == []
    script = node_call("a.buildObservationScript(input)", {"operation_id": "offline-1", "page_url": URL})
    child = page.frames[1]
    assert child.evaluate(script)["error"]["code"] == "FRAME_NOT_ALLOWED"


def test_script_has_no_host_capabilities_and_cannot_be_poisoned(page):
    page.evaluate("globalThis.RecruitOpsApplicationRecords={extract(){throw Error('poison')}}")
    script = node_call("a.buildObservationScript(input)", {"operation_id": "offline-1", "page_url": URL})
    for forbidden in ["require(", "chrome.", "fetch(", "process.", "dispatchControlEvents", "addListener"]:
        assert forbidden not in script
    assert page.evaluate(script)["ok"] is True
    assert page.evaluate(script)["ok"] is True


@pytest.mark.parametrize("params", [
    {"operation_id": "x", "page_url": "javascript:alert(1)"},
    {"operation_id": "x", "page_url": "https://secret:pw@ats.example/"},
    {"operation_id": "x", "page_url": URL, "script": "alert(1)"},
    {"operation_id": "x');alert(1);//", "page_url": URL},
])
def test_invalid_parameters(params):
    with pytest.raises(subprocess.CalledProcessError):
        node_call("a.buildObservationScript(input)", params)


def test_resource_packaging_and_tamper_detection(tmp_path):
    destination = tmp_path / "resources"
    assert node_call("(a.packageObservationResources(input),true)", str(destination))
    script = node_call("a.createObservationAdapter(input).buildObservationScript({operation_id:'x',page_url:'https://ats.example/'})", str(destination))
    assert "createSemanticObservation" in script
    with (destination / "actions.js").open("a", encoding="utf-8") as stream:
        stream.write("\n// altered resource")
    with pytest.raises(subprocess.CalledProcessError):
        node_call("a.createObservationAdapter(input)", str(destination))


def test_normalizer_rejects_operation_url_and_untrusted_fields(page):
    raw, _ = observe(page, "<p>Offline application page</p>")
    for key, value in [("operation_id", "another"), ("page_url", "https://other.example/applications")]:
        with pytest.raises(subprocess.CalledProcessError):
            node_call("a.normalizeObservation(input.raw,input.context)", {"raw": raw, "context": {**CONTEXT, key: value}})
    raw["data"]["page"]["cookies"] = "not accepted"
    with pytest.raises(subprocess.CalledProcessError):
        node_call("a.normalizeObservation(input.raw,input.context)", {"raw": raw, "context": CONTEXT})


@pytest.mark.parametrize("mode,expected_attempts", [("blank", 22), ("late", 22), ("login", 1), ("stable", 6)])
def test_migrated_slow_page_retry_uses_bounded_attempts(page, mode, expected_attempts):
    source = (ROOT / "extension/src/background.js").read_text(encoding="utf-8")
    scoring = source[source.index("  function usableSemanticObservation("):source.index("  async function requestSemanticObservation(")]
    retry = source[source.index("  async function waitForSemanticObservation("):source.index("  function bridgeId(")]
    actual = page.evaluate("""async ({scoring, retry, mode}) => {
      const harness = `(async () => {
        let calls = 0, delays = 0;
        const protocol = {pauseReasons: {LOGIN_REQUIRED:'login_required', CAPTCHA_REQUIRED:'captcha_required'}};
        const mode = ${JSON.stringify(mode)};
        const delay = async ms => { if(ms !== 1200) throw Error('delay changed'); delays++; };
        const requestSemanticObservation = async () => {
          calls++;
          if(mode === 'login') return {ok:false, pause:{reason:'login_required'}};
          const ready = mode === 'stable' || (mode === 'late' && calls >= 20);
          return {ok:true, data:{page:{text:ready ? 'Offline fixture application evidence with enough visible text' : ''},
            semanticNodes:[], applicationRecords:ready ? [{title:'Platform Engineer'}] : [], entries:[]}};
        };
        const aggregateSemanticObservations = items => items[0].response;
        ${scoring}
        ${retry}
        const response = await waitForSemanticObservation(1, 'offline', 'https://ats.example', {});
        return {calls, delays, response};
      })()`;
      return await eval(harness);
    }""", {"scoring": scoring, "retry": retry, "mode": mode})
    assert actual["calls"] == expected_attempts
    assert actual["delays"] == expected_attempts - 1
    if mode == "late":
        assert actual["response"]["data"]["applicationRecords"][0]["title"] == "Platform Engineer"


def capture(page, html):
    page.set_content(html)
    context = {"operation_id": "capture-1", "page_url": URL}
    script = node_call("a.buildManualCaptureScript(input)", context)
    raw = page.evaluate(script)
    return raw, node_call("a.normalizeManualCapture(input.raw,input.context)", {
        "raw": raw, "context": context,
    })


def test_manual_capture_is_unbound_draft_not_submission(page):
    _, result = capture(page, """<title>Platform Engineer - Example Careers</title>
      <main><h1>Platform Engineer</h1><p>Build distributed systems.</p>
      <input value="fixture-private-value"><textarea>fixture-resume</textarea>
      <a href="/jobs?token=fixture-secret">Job link</a></main>""")
    assert result["status"] == "SUCCEEDED"
    data = result["result"]
    assert data["kind"] == "manual_capture"
    assert data["requires_user_confirmation"] is True
    assert data["database_updated"] is False
    assert data["evidence_only"] is True
    assert data["draft"]["title"] == "Platform Engineer - Example Careers"
    assert data["draft"]["url"] == URL
    assert "Build distributed systems." in data["draft"]["page_text"]
    for forbidden in ["application_id", "job_id", "company", "status"]:
        assert forbidden not in data["draft"]
        assert forbidden not in data
    for secret in ["fixture-private-value", "fixture-resume", "fixture-secret"]:
        assert secret not in json.dumps(result)


def test_manual_capture_preserves_per_job_records(page):
    _, result = capture(page, """<article data-recruitops-application>
      <h2>Platform Engineer</h2><span data-recruitops-application-status="written">Written test</span>
      </article><article data-recruitops-application><h2>Data Engineer</h2>
      <span data-recruitops-application-status="rejected">Rejected</span></article>""")
    records = result["result"]["application_records"]
    assert [(r["title"], r["status"]) for r in records] == [
        ("Platform Engineer", "written"), ("Data Engineer", "rejected")]
    assert "rejected" not in records[0]["evidence"].lower()
    assert all("application_id" not in r for r in records)


@pytest.mark.parametrize("filename,code", [
    ("application-status-logged-out.html", "LOGIN_REQUIRED"),
    ("application-status-captcha.html", "CAPTCHA_REQUIRED"),
    ("application-status-overlay.html", "STATE_UNCLEAR"),
])
def test_manual_capture_gates_remain_user_controlled(page, filename, code):
    _, result = capture(page, (ROOT / "extension/fixtures" / filename).read_text(encoding="utf-8"))
    assert result["error_code"] == code
    assert result["result"]["requires_user_action"] is True
    assert "draft" not in result["result"]


@pytest.mark.parametrize("src", ["/child", "https://other.example/child"])
def test_capture_iframe_limit_is_explicit(page, src):
    _, result = capture(page, f'<p>Embedded job details</p><iframe src="{src}"></iframe>')
    assert result["status"] == "STATE_UNCLEAR"
    assert result["error_code"] == "FRAME_EVIDENCE_UNAVAILABLE"
    assert result["result"]["diagnostics"]["frameScope"] == "top_only"
    script = node_call("a.buildManualCaptureScript(input)", {
        "operation_id": "capture-1", "page_url": URL,
    })
    assert page.frames[1].evaluate(script)["error"]["code"] == "FRAME_NOT_ALLOWED"


def test_capture_blank_does_not_claim_success(page):
    _, result = capture(page, "<body></body>")
    assert result["status"] == "STATE_UNCLEAR"


def test_capture_and_review_event_ids_do_not_collide(page):
    raw, capture_result = capture(page, "<p>Offline job description</p>")
    review = node_call("a.normalizeObservation(input.raw,input.context)", {
        "raw": raw, "context": {**CONTEXT, "operation_id": "capture-1"},
    })
    assert review["event_id"] != capture_result["event_id"]
    repeated = node_call("a.normalizeManualCapture(input.raw,input.context)", {
        "raw": raw, "context": {"operation_id": "capture-1", "page_url": URL},
    })
    assert repeated["event_id"] == capture_result["event_id"]


@pytest.mark.parametrize("url", ["https://ats.example/other", URL + "#/other"])
def test_script_rejects_same_origin_navigation_before_collection(page, url):
    page.goto(url)
    page.set_content("<p>Wrong route</p>")
    script = node_call("a.buildObservationScript(input)", {
        "operation_id": "offline-1", "page_url": URL,
    })
    assert page.evaluate(script)["error"]["code"] == "SOURCE_NOT_ALLOWED"


def test_capture_rejects_untrusted_binding_and_oversized_pause(page):
    raw, _ = capture(page, "<p>Offline job description</p>")
    with pytest.raises(subprocess.CalledProcessError):
        node_call("a.normalizeManualCapture(input.raw,input.context)", {"raw": raw, "context": CONTEXT})
    raw.update(ok=False, type="extension.pause_state", pause={"reason": "login_required", "text": "x" * 262144})
    with pytest.raises(subprocess.CalledProcessError):
        node_call("a.normalizeManualCapture(input.raw,input.context)", {
            "raw": raw, "context": {"operation_id": "capture-1", "page_url": URL},
        })


def test_native_filler_capability_is_limited_and_disabled_by_default():
    capability = node_call("a.getFormFillCapability()")
    assert capability["supported"] is True
    assert capability["code"] == "LIMITED_NATIVE_V1"
    assert capability["enabled_by_default"] is False
    assert capability["preview"] is True
    assert capability["fill"] is True
    assert capability["file_selection"] == "user_only"
    assert capability["final_submission"] == "user_only"
    for method in ["buildFormPreviewScript", "buildFormFillScript"]:
        with pytest.raises(subprocess.CalledProcessError):
            node_call(f"a.{method}()")


def test_packaged_adapter_exposes_capture_and_fill_capability(tmp_path):
    resources = tmp_path / "packaged resources"
    node_call("(a.packageObservationResources(input),true)", str(resources))
    assert node_call("a.createObservationAdapter(input).getFormFillCapability().supported", str(resources)) is True
    script = node_call(
        "a.createObservationAdapter(input).buildManualCaptureScript({operation_id:'packaged',page_url:'https://ats.example/'})",
        str(resources),
    )
    assert "createSemanticObservation" in script
    for forbidden in ["chrome.", "dispatchControlEvents", "fetch(", "require(", ".click(", "new Event("]:
        assert forbidden not in script
