"""Edge screenshot fallback with local DOM and mocked Chrome/API transports."""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright


SRC = Path(__file__).parents[1] / "extension" / "src"
URL = "https://ats.example/applications"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        yield instance
        instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 900, "height": 600}, service_workers="block")
    context.route("**/*", lambda route: route.fulfill(body="<html><body></body></html>", content_type="text/html"))
    page = context.new_page()
    page.goto(URL)
    page.evaluate("""() => {
      globalThis.__shots = []; globalThis.__requests = []; globalThis.__captureFail = false;
      globalThis.importScripts = () => {};
      const tab = {id: 7, windowId: 1, url: location.href, status: 'complete'};
      globalThis.__tab = tab;
      const listener = {addListener() {}, removeListener() {}};
      globalThis.chrome = {
        runtime: {id: 'fixture', onInstalled: listener, onStartup: listener, onMessage: listener},
        permissions: {contains: async () => true}, windows: {update: async () => {}},
        tabs: {get: async () => tab, query: async () => [tab], update: async () => tab,
          create: async () => tab, remove: async () => {}, onUpdated: listener, onRemoved: listener,
          captureVisibleTab: async () => {
            if (__captureFail) throw new Error('fixture failure with private data');
            __shots.push({y: scrollY, time: Date.now(), masked: document.querySelectorAll('[data-recruitops-privacy-mask]').length,
              inputHidden: document.querySelector('input') ? getComputedStyle(document.querySelector('input')).visibility : null});
            return 'data:image/jpeg;base64,/9j/2Q==';
          }},
        scripting: {executeScript: async (request) => {
          if (request.files) return [];
          return [{frameId: 0, result: await request.func(...(request.args || []))}];
        }},
        storage: {local: {get: async () => ({}), set: async () => {}}}
      };
      globalThis.fetch = async (url, options) => {
        __requests.push(JSON.parse(options.body));
        return {ok: true, json: async () => ({text: '软件工程师 当前状态: 笔试中', confidence: .98,
          model: 'deepseek-flash', image_sha256: 'a'.repeat(64), usage: {total_tokens: 33}})};
      };
    }""")
    for name in ("protocol.js", "allowlist.js", "config.js", "actions.js"):
        page.add_script_tag(path=str(SRC / name))
    source = (SRC / "background.js").read_text(encoding="utf-8").replace("  void connectBrowserBridge();", "")
    prefix, suffix = source.rsplit("})();", 1)
    page.add_script_tag(content=prefix + """
      globalThis.__vision = {canRequestVision, aggregateSemanticObservations, visionFrameCaptureState,
        captureVisiblePageVision, captureVisiblePageVisionSerial, executeCommandAuthorizedAction,
        setQueue(promise) { visionQueue = promise; },
        setObservation(response) { waitForSemanticObservation = async () => response; }};
    })();""" + suffix)
    yield page
    context.close()


def test_multitarget_gate_accepts_distinct_cards_but_not_auth_blank_or_ambiguity(page):
    result = page.evaluate("""() => {
      const validation = {params: {include_vision: true}};
      const work = {application_id: '24', application_ids: ['24', '25']};
      const base = {ok: true, data: {page: {text: '软件工程师 我的投递记录'},
        applicationRecords: [{title: '软件工程师'}, {title: '算法工程师'}], entries: [{status: 'applied'}]}};
      const check = (data) => __vision.canRequestVision({ok: true, data}, validation, work);
      return [check(base.data), check({...base.data, page: {text: '请先登录'}}),
        check({page: {text: ''}, applicationRecords: [], diagnostics: {visibleMediaCount: 0}}),
        check({...base.data, applicationRecords: [{title: '软件工程师'}, {title: '软件工程师'}]}),
        check({...base.data, applicationRecords: [{title: '软件工程师', signals: {conflicting_statuses: true}}]}),
        check({...base.data, pageSegments: [{text: '请进行身份认证，使用邮箱验证'}]})];
    }""")
    assert result == [True, False, False, False, False, False]


def test_frame_segments_keep_separate_bounded_text(page):
    result = page.evaluate("""() => __vision.aggregateSemanticObservations(Array.from({length: 6}, (_, i) => ({
      frameId: i, frameUrl: `https://ats.example/frame${i}`, response: {ok: true, data: {
        page: {text: `frame-${i} ` + '文'.repeat(4500), title: `frame ${i}`},
        semanticNodes: [], applicationRecords: [], entries: []}}
    })), {}).data""")
    assert len(result["pageSegments"]) == 4
    assert all(len(item["text"]) == 4000 and item["truncated"] for item in result["pageSegments"])
    assert len({item["frameId"] for item in result["pageSegments"]}) == 4
    assert result["diagnostics"]["pageSegmentsTruncated"]


def test_segment_capture_masks_fields_contacts_and_restores_scroll(page):
    page.set_content("""<style>body {height: 4200px} div {background: transparent !important}</style>
        <input value='private'><p>person@example.test 13812345678</p><p>软件工程师 当前状态: 笔试中</p>""")
    result = page.evaluate("""async () => {
      scrollTo(0, 350);
      const before = scrollY;
      const args = [{apiBaseUrl: 'http://127.0.0.1:8000', apiToken: 'fixture-local-token'},
        {id: 7, windowId: 1}, location.href, 'vision-one'];
      const [a, b] = await Promise.all([__vision.captureVisiblePageVisionSerial(...args), __vision.captureVisiblePageVisionSerial(...args)]);
      return {before, after: scrollY, shots: __shots, requests: __requests, reading: a,
        masks: document.querySelectorAll('[data-recruitops-privacy-mask]').length,
        restored: document.querySelector('input').style.visibility, same: a === b};
    }""")
    assert result["before"] == result["after"] == 350
    assert len(result["shots"]) == 4 and len(result["requests"]) == 1
    assert all(b["time"] - a["time"] >= 550 for a, b in zip(result["shots"], result["shots"][1:]))
    assert result["shots"][0]["masked"] >= 2
    assert all(shot["inputHidden"] == "hidden" for shot in result["shots"])
    assert len(result["requests"][0]["image_data_urls"]) == 4
    assert result["masks"] == 0 and result["restored"] == "" and result["same"]


def test_mask_failure_with_unreadable_frame_never_captures(page):
    page.set_content("<input><iframe srcdoc='<p>Private inaccessible frame</p>'></iframe>")
    result = page.evaluate("""async () => {
      try { await __vision.captureVisiblePageVision({apiBaseUrl: 'http://127.0.0.1:8000'}, {id: 7, windowId: 1}, location.href, 'missing-frame'); }
      catch (error) { return {code: error.message, shots: __shots.length, requests: __requests.length,
        restored: globalThis.__recruitopsVisionCaptureV1 === undefined}; }
    }""")
    assert result == {"code": "VISION_PRIVACY_MASK_UNAVAILABLE", "shots": 0, "requests": 0, "restored": True}


def test_capture_failure_returns_dom_evidence_and_safe_error(page):
    result = page.evaluate("""async () => {
      __captureFail = true;
      __vision.setObservation({ok: true, data: {page: {text: '软件工程师 当前阶段待确认'},
        pageSegments: [{frameId: 0, text: '软件工程师 当前阶段待确认'}], semanticNodes: [{text: '软件工程师'}],
        applicationRecords: [{title: '软件工程师'}], entries: [], diagnostics: {}}});
      const protocol = RecruitOpsProtocol;
      return await __vision.executeCommandAuthorizedAction({apiBaseUrl: 'http://127.0.0.1:8000'}, {
        commandComplete: true, operation: protocol.commandTypes.OBSERVE_APPLICATION_STATUS_PAGE,
        action: protocol.actionTypes.OBSERVE_APPLICATION_PAGE, selector_key: protocol.selectorKeys.APPLICATION_PAGE,
        params: {include_vision: true, vision_fallback_reason: 'no_structured_evidence_visible_status_likely'},
        page_url: location.href, origin: location.origin, operation_id: 'failed-capture', application_id: '24', application_ids: ['24']
      }, {cancelled: false}, {readyState: 1, send() {}});
    }""")
    assert result["ok"] and result["status"] == "SUCCEEDED", result
    assert result["result"]["page"]["text"] == "软件工程师 当前阶段待确认"
    assert result["result"]["application_records"] == [{"title": "软件工程师"}]
    assert result["result"]["vision_error"] == "VISION_CAPTURE_FAILED"
    assert "private" not in str(result)
    assert result["result"]["page_segments"][0]["frameId"] == 0
    assert page.evaluate("globalThis.__recruitopsVisionCaptureV1 === undefined")


@pytest.mark.parametrize("change", ["query", "document", "cancel"])
def test_restore_navigation_reload_or_cancel_blocks_upload(page, change):
    result = page.evaluate("""async (change) => {
      const owner = {cancelled: false};
      const original = chrome.scripting.executeScript;
      chrome.scripting.executeScript = async (request) => {
        const result = await original(request);
        if (request.args?.[0] === 'restore') {
          if (change === 'query') { history.replaceState({}, '', '?different=1'); __tab.url = location.href; }
          if (change === 'document') globalThis.__recruitopsVisionDocumentV1 = 'reloaded-same-url';
          if (change === 'cancel') owner.cancelled = true;
        }
        return result;
      };
      try { await __vision.captureVisiblePageVision({apiBaseUrl: 'http://127.0.0.1:8000'}, {id: 7, windowId: 1}, location.href, 'restore-change', owner); }
      catch (error) { return {code: error.message, requests: __requests.length, restored: globalThis.__recruitopsVisionCaptureV1 === undefined}; }
    }""", change)
    assert result["code"] == ("VISION_CAPTURE_CANCELLED" if change == "cancel" else "CAPTURE_DOCUMENT_CHANGED")
    assert result["requests"] == 0 and result["restored"]


def test_same_url_document_reload_after_dom_evidence_blocks_capture(page):
    result = page.evaluate("""async () => {
      const identity = await __vision.visionFrameCaptureState('identity', 0);
      const expected = [{frameId: 0, documentToken: identity.documentToken, documentUrl: identity.documentUrl}];
      globalThis.__recruitopsVisionDocumentV1 = 'new-document';
      try { await __vision.captureVisiblePageVision({apiBaseUrl: 'http://127.0.0.1:8000'}, {id: 7, windowId: 1}, location.href, 'stale-dom', {cancelled: false}, expected); }
      catch (error) { return {code: error.message, shots: __shots.length, requests: __requests.length}; }
    }""")
    assert result == {"code": "CAPTURE_DOCUMENT_CHANGED", "shots": 0, "requests": 0}


@pytest.mark.parametrize("change", ["document", "cancel"])
def test_model_reply_is_rejected_if_document_or_owner_changed(page, change):
    result = page.evaluate("""async (change) => {
      const owner = {cancelled: false};
      const original = fetch;
      globalThis.fetch = async (...args) => {
        const response = await original(...args);
        const json = response.json;
        response.json = async () => {
          const body = await json();
          if (change === 'document') globalThis.__recruitopsVisionDocumentV1 = 'reloaded-after-response';
          else owner.cancelled = true;
          return body;
        };
        return response;
      };
      try { await __vision.captureVisiblePageVision({apiBaseUrl: 'http://127.0.0.1:8000'}, {id: 7, windowId: 1}, location.href, 'reply-change', owner); }
      catch (error) { return {code: error.message, requests: __requests.length}; }
    }""", change)
    assert result["code"] == ("VISION_CAPTURE_CANCELLED" if change == "cancel" else "CAPTURE_DOCUMENT_CHANGED")
    assert result["requests"] == 1


def test_timeout_covers_queued_work_and_cannot_upload_after_late_release(page):
    result = page.evaluate("""async () => {
      const originalTimeout = setTimeout;
      globalThis.setTimeout = (fn, ms, ...args) => originalTimeout(fn, ms === 45000 ? 80 : ms, ...args);
      let release;
      __vision.setQueue(new Promise((resolve) => { release = resolve; }));
      let code;
      try { await __vision.captureVisiblePageVisionSerial({apiBaseUrl: 'http://127.0.0.1:8000'}, {id: 7, windowId: 1}, location.href, 'queued-timeout'); }
      catch (error) { code = error.message; }
      release();
      await new Promise((resolve) => originalTimeout(resolve, 30));
      return {code, shots: __shots.length, requests: __requests.length};
    }""")
    assert result == {"code": "VISION_CAPTURE_TIMEOUT", "shots": 0, "requests": 0}


def test_timeout_includes_response_body_after_headers(page):
    result = page.evaluate("""async () => {
      const originalTimeout = setTimeout;
      globalThis.setTimeout = (fn, ms, ...args) => originalTimeout(fn, ms === 45000 ? 400 : ms, ...args);
      let signal;
      globalThis.fetch = async (_url, options) => {
        signal = options.signal; __requests.push(JSON.parse(options.body));
        return {ok: true, json: () => new Promise(() => {})};
      };
      const started = performance.now();
      try { await __vision.captureVisiblePageVisionSerial({apiBaseUrl: 'http://127.0.0.1:8000'}, {id: 7, windowId: 1}, location.href, 'body-timeout'); }
      catch (error) { return {code: error.message, requests: __requests.length, aborted: signal?.aborted,
        elapsed: performance.now() - started, restored: globalThis.__recruitopsVisionCaptureV1 === undefined}; }
    }""")
    assert result["code"] == "VISION_CAPTURE_TIMEOUT" and result["requests"] == 1
    assert result["aborted"] and result["restored"] and result["elapsed"] < 1500
