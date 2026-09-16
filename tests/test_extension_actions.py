import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
EXTENSION = ROOT / "extension"
SRC = EXTENSION / "src"
FIXTURES = EXTENSION / "fixtures"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_source(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


def read_fixture_resource() -> dict:
    return read_json(FIXTURES / "application-status-fixtures.json")


def launch_fixture_browser(playwright):
    from playwright.sync_api import Error

    last_error = None
    for options in ({}, {"channel": "msedge"}):
        try:
            return playwright.chromium.launch(headless=True, **options)
        except Error as exc:
            last_error = exc
    pytest.skip(f"Chromium or Edge is unavailable for DOM fixture tests: {last_error}")


def test_active_action_collects_generic_semantic_evidence() -> None:
    protocol = read_json(EXTENSION / "protocol.json")
    actions = read_source("actions.js")

    assert set(protocol["actions"]) == {"observe_application_page"}
    assert protocol["actions"]["observe_application_page"]["evidenceOnly"] is True
    assert protocol["actions"]["observe_application_page"]["parameters"] == [
        "include_vision",
        "vision_fallback_reason",
        "retain_on_pause",
    ]
    assert set(protocol["selectorWhitelist"]) == {"application_page"}
    assert "validateActionRequest" in actions
    assert "selectorCandidates" in actions
    assert "job_detail_link" not in actions
    assert "job_filter" not in actions
    assert "next_page" not in actions


def test_runtime_protocol_validates_observation_dispatch_envelope() -> None:
    from playwright.sync_api import sync_playwright

    commands = [
        {
            "operation": "observe_application_status_page",
            "command": {
                "action": "observe_application_page",
                "selector_key": "application_page",
                "params": {"include_vision": False},
                "page_url": "https://ats.example/applications/1",
                "origin": "https://ats.example",
                "application_id": "1",
                "application_ids": ["1"],
            },
        },
    ]
    with sync_playwright() as playwright:
        browser = launch_fixture_browser(playwright)
        try:
            page = browser.new_page()
            page.add_script_tag(path=str(SRC / "protocol.js"))
            results = page.evaluate(
                """commands => commands.map((item, index) =>
                  globalThis.RecruitOpsProtocol.validateBridgeDispatch({
                    type: "operation.dispatch",
                    payload: {operation_id: `operation-${index}`, ...item}
                  }))""",
                commands,
            )
            invalid = page.evaluate(
                """() => globalThis.RecruitOpsProtocol.validateBridgeDispatch({
                  type: "operation.dispatch",
                  payload: {
                    operation_id: "broken",
                    operation: "observe_application_status_page",
                    command: {action: "observe_application_page"}
                  }
                })"""
            )
        finally:
            browser.close()

    assert all(item["ok"] is True and item["missingFields"] == [] for item in results)
    assert invalid["ok"] is False
    assert invalid["code"] == "COMMAND_INVALID"
    assert set(invalid["missingFields"]) == {
        "command.selector_key",
        "command.params",
        "command.page_url",
        "command.origin",
        "command.application_id",
        "command.application_ids",
    }


@pytest.mark.parametrize(
    ("url", "container", "status_text"),
    [
        (
            "https://app.mokahr.com/campus-recruitment/acme/1#/candidateHome/applications",
            '<main><section class="candidate-application-list"><h2>My applications</h2>'
            '<div role="status">Backend Engineer - Interview</div></section></main>',
            "Backend Engineer - Interview",
        ),
        (
            "https://acme.zhiye.com/campus/personal/deliveryRecord",
            '<div role="main"><div class="delivery-record"><h2>投递记录</h2>'
            '<span aria-label="申请状态">测评中</span></div></div>',
            "测评中",
        ),
        (
            "https://acme.jobs.feishu.cn/campus/position/application",
            '<main><div data-testid="application-list"><h2>投递进展</h2>'
            '<div role="status">技术面试</div></div></main>',
            "技术面试",
        ),
        (
            "https://careers.example.test/my/applications",
            '<main><article class="application"><h2>Software Engineer</h2>'
            '<strong aria-label="Status">Offer</strong></article></main>',
            "Offer",
        ),
    ],
    ids=["moka", "beisen", "feishu", "self-built"],
)
def test_observation_protocol_is_stable_across_recruitment_page_families(
    url: str,
    container: str,
    status_text: str,
) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = launch_fixture_browser(playwright)
        try:
            page = browser.new_page()
            page.route(
                "**/*",
                lambda route: route.fulfill(
                    body=f"<!doctype html><title>Application status</title>{container}",
                    content_type="text/html; charset=utf-8",
                ),
            )
            page.goto(url)
            page.evaluate(
                """
                globalThis.__listenerCount = 0;
                globalThis.chrome = {runtime: {id: "fixture-extension", onMessage: {
                  addListener(handler) {
                    globalThis.__listenerCount += 1;
                    globalThis.__recruitopsMessageHandler = handler;
                  }
                }}};
                """
            )
            for _ in range(2):
                for script in ("protocol.js", "actions.js", "content-script.js"):
                    page.add_script_tag(path=str(SRC / script))
            response = page.evaluate(
                """() => new Promise((resolve) => {
                  globalThis.__recruitopsMessageHandler({
                    protocolVersion: globalThis.RecruitOpsProtocol.version,
                    type: globalThis.RecruitOpsProtocol.messageTypes.EXECUTE_CONTROLLED_ACTION,
                    requestId: "platform-observe",
                    commandAuthorized: true,
                    authorizedOrigin: location.origin,
                    tabId: 7,
                    action: globalThis.RecruitOpsProtocol.actionTypes.OBSERVE_APPLICATION_PAGE,
                    selectorKey: globalThis.RecruitOpsProtocol.selectorKeys.APPLICATION_PAGE,
                    params: {include_vision: false},
                    actionTicket: "platform-ticket"
                  }, {id: globalThis.chrome.runtime.id}, resolve);
                })"""
            )
            listener_count = page.evaluate("globalThis.__listenerCount")
        finally:
            browser.close()

    assert listener_count == 1
    assert response["ok"] is True
    assert response["data"]["page"]["origin"] == url.split("/", 3)[0] + "//" + url.split("/", 3)[2]
    assert any(status_text in node["text"] for node in response["data"]["semanticNodes"])


def test_background_uses_one_persistent_bridge_dispatch_path_with_self_contained_command() -> None:
    source = read_source("background.js")

    imports = 'importScripts("./protocol.js", "./allowlist.js", "./config.js", "./actions.js");'
    assert imports in source
    assert source.index(imports) < source.index("globalThis.RecruitOpsProtocol")

    for fragment in (
        "new WebSocket(bridgeUrl)",
        "crypto.subtle.importKey",
        "HMAC",
        "operation.dispatch",
        'type: "ack"',
        'type: "progress"',
        'type: "result"',
        'type: "heartbeat"',
        "bridgeDeviceId",
        "scheduleBridgeReconnect",
        "executeCommandAuthorizedAction",
        "commandAuthorized: true",
        "const validation = protocol.validateBridgeDispatch(message)",
        "const command = validation.command || {}",
        "action: command.action",
        "selector_key: command.selector_key",
        "params: command.params",
        "page_url: command.page_url",
        "application_ids: command.application_ids",
        "evidence_only: true",
        "database_updated: false",
        "captureVisiblePageVision",
        "/api/browser/vision",
    ):
        assert fragment in source

    for fragment in (
        "REQUEST_CONTROLLED_ACTION",
        "approvalToken",
        "approval_token",
        "/api/browser/actions",
        "/api/browser/application-status/observations",
        "/api/browser/actions/pending",
        "/api/browser/actions/outcome",
        "pollCommandAuthorizedActions",
        "chrome.alarms",
        "consume: true",
    ):
        assert fragment not in source

    assert "/api/browser/observations" in source
    assert "/api/browser/application-captures" in source
    assert "headers.Authorization = `Bearer ${settings.apiToken}`" in source
    assert "credentials: \"omit\"" in source
    assert "executeScript({func" not in source
    assert "eval(" not in source
    assert "new Function" not in source
    assert "protocol_version: 1" not in source
    assert "protocol_version: protocol.bridgeProtocol.version" in source


def test_content_script_collects_bounded_semantic_nodes_and_returns_pause_states() -> None:
    content = read_source("content-script.js")

    for fragment in (
        "READ_SANITIZED_DOM",
        "EXECUTE_CONTROLLED_ACTION",
        "ACTION_TICKET_SINGLE_USE",
        "createPauseState",
        "CAPTCHA_REQUIRED",
        "LOGIN_REQUIRED",
        "STATE_UNCLEAR",
        "querySelectorAll",
        "readSanitizedText",
        "createSemanticObservation",
        "semanticNode",
        "aria-current",
        "backgroundColor",
        "visibleMediaCount",
    ):
        assert fragment in content

    for fragment in (
        "document.cookie",
        "chrome.cookies",
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "userGesture",
        "clickWhitelistedElement",
        "performFilter",
        "performNextPage",
        "eval(",
        "new Function",
        "innerHTML",
        "requestSubmit",
        ".submit(",
    ):
        assert fragment not in content

    assert 'performance.getEntriesByType("resource")' in content
    assert "entry.responseStatus" in content
    assert "entry.requestHeaders" not in content
    assert "entry.responseHeaders" not in content
    assert "querySelector(message.selectorKey)" not in content
    assert "APPLICATION_STATUS_RULES" not in content


def test_zero_record_pages_report_text_media_and_blank_signals_for_vision_gate() -> None:
    from playwright.sync_api import sync_playwright

    cases = [
        ("text", "<main><p>投递进度页面正文</p></main>", 0),
        ("image-only", "<main><canvas style='width: 80px; height: 40px'></canvas></main>", 1),
        ("blank", "<main></main>", 0),
    ]
    with sync_playwright() as playwright:
        browser = launch_fixture_browser(playwright)
        try:
            for case_id, html, expected_media_count in cases:
                page = browser.new_page()
                page.set_content(html)
                page.evaluate(
                    """
                    globalThis.chrome = {runtime: {id: "fixture-extension", onMessage: {
                      addListener(handler) { globalThis.__recruitopsMessageHandler = handler; }
                    }}};
                    """
                )
                for script in ("protocol.js", "actions.js", "application-records.js", "content-script.js"):
                    page.add_script_tag(path=str(SRC / script))
                response = page.evaluate(
                    """
                    () => new Promise((resolve) => {
                      globalThis.__recruitopsMessageHandler(
                        {
                          protocolVersion: globalThis.RecruitOpsProtocol.version,
                          type: globalThis.RecruitOpsProtocol.messageTypes.EXECUTE_CONTROLLED_ACTION,
                          requestId: "vision-gate-signals",
                          commandAuthorized: true,
                          authorizedOrigin: location.origin,
                          tabId: 7,
                          action: globalThis.RecruitOpsProtocol.actionTypes.OBSERVE_APPLICATION_PAGE,
                          selectorKey: globalThis.RecruitOpsProtocol.selectorKeys.APPLICATION_PAGE,
                          params: {
                            include_vision: true,
                            vision_fallback_reason: "no_structured_evidence_visible_status_likely"
                          },
                          actionTicket: "vision-gate-ticket"
                        },
                        {id: globalThis.chrome.runtime.id},
                        resolve
                      );
                    })
                    """
                )
                assert response["ok"] is True, (case_id, response)
                diagnostics = response["data"]["diagnostics"]
                assert diagnostics["visibleMediaCount"] == expected_media_count
                page.close()
        finally:
            browser.close()


def test_popup_has_no_manual_status_actions_or_single_use_token() -> None:
    popup = (EXTENSION / "popup.html").read_text(encoding="utf-8")
    script = (EXTENSION / "popup.js").read_text(encoding="utf-8")

    assert 'id="authorize"' in popup
    assert 'id="open-options"' in popup
    assert 'id="capture-application"' in popup
    assert 'id="capture-job-id"' in popup
    assert 'id="capture-note"' in popup
    assert 'id="approval-token"' not in popup
    assert 'data-action=' not in popup
    assert "REQUEST_CONTROLLED_ACTION" not in script
    assert "approvalToken" not in script
    assert "filterQuery" not in script
    assert "AUTHORIZE_CURRENT_TAB" in script
    assert "SUBMIT_DOM_OBSERVATION" in script
    assert "SUBMIT_APPLICATION_CAPTURE" in script


def test_options_keep_connection_diagnostics_without_claim_or_poll_controls() -> None:
    manifest = read_json(EXTENSION / "manifest.json")
    protocol = read_json(EXTENSION / "protocol.json")
    options = (EXTENSION / "options.html").read_text(encoding="utf-8")
    options_script = (EXTENSION / "options.js").read_text(encoding="utf-8")
    background = read_source("background.js")

    assert manifest["version"] == "0.3.23"
    assert protocol["version"] == 4
    assert protocol["messages"]["CONFIG_TEST"]["type"] == "extension.config.test"
    assert 'id="test-connection"' in options
    assert "chrome.runtime.getManifest().version" in options_script
    assert "CONFIG_TEST" in options_script
    assert "bridgeMessage" in options_script
    assert "connecting" in options_script
    assert "retry_scheduled" in options_script
    assert "connectBrowserBridge();" in background
    assert "recordBridgeStatus" in background
    assert 'message.ack === true' in background
    assert 'recordBridgeStatus("connected", {deviceId})' in background
    assert 'reason: "authentication_timeout"' in background
    assert 'return recordBridgeStatus("connecting")' not in background
    assert "ensureCommandPoller" not in background
    assert "poll" not in options_script.casefold()
    assert "领取" not in options_script


def test_browser_bridge_keeps_human_action_tab_visible_until_retry() -> None:
    background = read_source("background.js")

    assert "let keepTabOpen = false;" in background
    assert "keepTabOpen = true;" in background
    assert "chrome.tabs.update(tab.id, {active: true})" in background
    assert "chrome.windows.update(tab.windowId, {focused: true})" in background
    assert "tab_retained: true" in background
    assert 'page_url: sanitizedTabUrl(tab.url)' in background
    assert 'protocol.createPauseState(' in background
    assert 'if (!keepTabOpen && typeof tab?.id === "number")' in background


def test_oc_capture_stops_immediately_when_signed_links_fall_back_to_login() -> None:
    background = read_source("background.js")
    content = read_source("content-script.js")

    assert "isOcLoginFallbackUrl" in background
    assert 'parsed.pathname === "/780.html"' in background
    assert "loginRequired: true" in background
    assert "loginFallbacks === completed" in background
    assert "resolved.size === 0" in background
    assert "login_fallbacks: loginFallbacks" in background
    assert "login_required: loginRequired" in background
    assert 'bridgeFailure("LOGIN_REQUIRED"' in background
    assert "if (!keepTabOpen" in background
    assert "transientSignedLink" in content
    assert "scrubbed before persistence" in content
    assert "scrubOcTransientApplyUrls(records);" in background
    assert background.index("scrubOcTransientApplyUrls(records);") < background.index(
        '"VALIDATING", "Persisting the sanitized GiveMeOC snapshot locally."'
    )


def test_browser_bridge_waits_for_spa_content_and_uses_vision_after_dom_only_observation() -> None:
    background = read_source("background.js")
    content = read_source("content-script.js")

    assert "waitForSemanticObservation" in background
    assert "usableSemanticObservation" in background
    assert "attempt <= 16" in background
    assert "semanticObservationScore" in background
    assert "stableAttempts >= 2" in background
    assert "canRequestVision" in background
    assert "work?.application_ids" in background
    assert "records.length > 1" in background
    assert "visibleMediaCount" in background
    assert "normalizeVisionResponse" in background
    assert 'typeof body.text !== "string"' in background
    assert 'document.body.innerText || ""' in content
    assert 'redactSensitiveText(element.innerText || "")' in content
    assert "visibleTextLength" in content
    assert "sender?.tab?.id" not in content
    assert 'sender?.id !== chrome.runtime.id' in content
    assert 'bridgeFailure("ACTION_EXECUTION_FAILED", {detail})' in background


def test_oc_link_resolution_classifies_non_job_entries_before_persistence() -> None:
    background = read_source("background.js")

    assert "function isOcSignedApplyUrl(value)" in background
    assert "function isUsableOcDestinationUrl(value)" in background
    assert 'parsed.hostname !== "www.givemeoc.com"' in background
    assert 'parsed.hostname !== "givemeoc.com"' in background
    assert "function classifyOcDestinationUrl(value)" in background
    assert 'hostname === "wj.qq.com"' in background
    assert 'hostname === "mp.weixin.qq.com"' in background
    assert "if (isOcSignedApplyUrl(value) && !seen.has(value))" in background
    assert 'record.resolved_apply_urls = destinations.slice(0, 20);' in background
    assert 'record.excluded_apply_urls = excluded.slice(0, 20);' in background
    assert '"excluded_non_job_entry"' in background
    assert "addressable: resolved.size - excludedCount" in background
    assert "excluded: excludedCount" in background


def test_browser_bridge_keeps_token_out_of_transport_and_marks_result_as_evidence_only() -> None:
    protocol = read_json(EXTENSION / "protocol.json")
    config = read_source("config.js")
    background = read_source("background.js")

    bridge = protocol["browserBridge"]
    assert bridge["authentication"] == {
        "challenge": "server_generated",
        "algorithm": "HMAC-SHA256",
        "tokenSource": "local_api_token",
        "tokenInUrl": False,
        "tokenInMessages": False,
    }
    assert 'url.pathname = BROWSER_BRIDGE_PATH' in config
    assert 'url.search = ""' in config
    assert 'url.hash = ""' in config
    assert 'message.type === "operation.dispatch"' in background
    assert 'protocol.actionTypes.OBSERVE_APPLICATION_PAGE' in background
    assert 'type: "auth"' in background
    assert 'type: "result"' in background
    assert "approval_token" not in background
    assert "database_updated: false" in background
    assert "evidence_only: true" in background


def test_popup_application_capture_remains_a_sanitized_preview_only() -> None:
    popup = (EXTENSION / "popup.html").read_text(encoding="utf-8")
    script = (EXTENSION / "popup.js").read_text(encoding="utf-8")
    background = read_source("background.js")

    assert 'id="capture-application"' in popup
    assert 'id="capture-job-id"' in popup
    assert 'id="capture-note"' in popup
    assert "AUTHORIZE_CURRENT_TAB" in script
    assert "SUBMIT_APPLICATION_CAPTURE" in script
    assert "已生成待审批投递记录" in script
    assert "已经写入" not in script
    assert 'fetch(`${apiBaseUrl}/api/browser/application-captures`' in background
    assert 'credentials: "omit"' in background
    assert "boundedText(snapshot.text, 20000)" in background
    assert "job_id: boundedText(message.jobId, 200) || null" in background
    assert "document.cookie" not in background
    assert "chrome.cookies" not in background


def test_semantic_page_selector_covers_logged_in_logout_and_redesign() -> None:
    from bs4 import BeautifulSoup

    resource = read_fixture_resource()
    assert resource["version"] == 2
    assert resource["source_type"] == "synthetic_edge_fixture"
    assert resource["captured_at"] == "2026-08-31"
    assert {item["page_state"] for item in resource["fixtures"]} == {
        "logged_in",
        "logged_out",
        "captcha",
        "popup_overlay",
        "structure_changed",
        "evidence_conflict",
    }
    assert len(resource["fixtures"]) == 6
    assert all((FIXTURES / item["file"]).is_file() for item in resource["fixtures"])
    assert {item["expected"] for item in resource["fixtures"]} == {"entries", "pause", "conflict"}
    assert {item["expected_status"] for item in resource["write_guard_cases"]} == {
        "updated",
        "STATE_UNCLEAR",
    }

    protocol = read_json(EXTENSION / "protocol.json")
    selectors = protocol["selectorWhitelist"]["application_page"]

    multiple = BeautifulSoup(
        (FIXTURES / "application-status-multiple.html").read_text(encoding="utf-8"),
        "html.parser",
    )
    status_nodes = []
    for selector in selectors:
        status_nodes.extend(multiple.select(selector))
    assert len({id(node) for node in status_nodes}) >= 1
    assert any("written test" in node.get_text(" ", strip=True).casefold() for node in status_nodes)
    assert not multiple.select("form")

    logged_out = BeautifulSoup(
        (FIXTURES / "application-status-logged-out.html").read_text(encoding="utf-8"),
        "html.parser",
    )
    assert logged_out.select("input[type='password']")
    assert any(logged_out.select(selector) for selector in selectors)

    redesign = BeautifulSoup(
        (FIXTURES / "application-status-redesign.html").read_text(encoding="utf-8"),
        "html.parser",
    )
    assert "Status: Interview" in redesign.get_text(" ", strip=True)
    assert any(redesign.select(selector) for selector in selectors)

    captcha = BeautifulSoup(
        (FIXTURES / "application-status-captcha.html").read_text(encoding="utf-8"),
        "html.parser",
    )
    assert captcha.select("[data-recruitops-auth='captcha']")

    overlay = BeautifulSoup(
        (FIXTURES / "application-status-overlay.html").read_text(encoding="utf-8"),
        "html.parser",
    )
    assert overlay.select("[role='dialog'][aria-modal='true']")

    conflict = BeautifulSoup(
        (FIXTURES / "application-status-conflict.html").read_text(encoding="utf-8"),
        "html.parser",
    )
    assert [node.get("data-recruitops-application-status") for node in conflict.select(
        "[data-recruitops-application-status]"
    )] == ["written", "interview"]


def test_application_status_dom_fixtures_execute_only_the_typed_observe_action() -> None:
    from playwright.sync_api import sync_playwright

    resource = read_fixture_resource()
    content_script = read_source("content-script.js")
    assert "createSemanticObservation" in content_script
    assert "semanticNodes" in content_script
    assert "APPLICATION_STATUS_RULES" not in content_script
    assert ".submit(" not in content_script
    assert "form.submit" not in content_script.casefold()

    with sync_playwright() as playwright:
        browser = launch_fixture_browser(playwright)
        try:
            for item in resource["fixtures"]:
                page = browser.new_page()
                html = (FIXTURES / item["file"]).read_text(encoding="utf-8")
                page.set_content(html)
                page.evaluate(
                    """
                    globalThis.chrome = {
                      runtime: {
                        id: "fixture-extension",
                        onMessage: {
                          addListener(handler) {
                            globalThis.__recruitopsMessageHandler = handler;
                          }
                        }
                      }
                    };
                    """
                )
                for script in (
                    "protocol.js",
                    "actions.js",
                    "application-records.js",
                    "content-script.js",
                ):
                    page.add_script_tag(path=str(SRC / script))

                response = page.evaluate(
                    """
                    () => new Promise((resolve) => {
                      globalThis.__recruitopsMessageHandler(
                        {
                          protocolVersion: globalThis.RecruitOpsProtocol.version,
                          type: globalThis.RecruitOpsProtocol.messageTypes.EXECUTE_CONTROLLED_ACTION,
                          requestId: "fixture-read",
                          commandAuthorized: true,
                          authorizedOrigin: location.origin,
                          tabId: 7,
                          action: globalThis.RecruitOpsProtocol.actionTypes.OBSERVE_APPLICATION_PAGE,
                          selectorKey: globalThis.RecruitOpsProtocol.selectorKeys.APPLICATION_PAGE,
                          params: {include_vision: false},
                          actionTicket: "fixture-ticket"
                        },
                        {id: globalThis.chrome.runtime.id},
                        resolve
                      );
                    })
                    """
                )

                if item["expected"] == "entries":
                    assert response["ok"] is True, (item["id"], response)
                    assert response["data"]["semanticNodes"]
                    assert response["data"]["entries"], (item["id"], response["data"])
                    assert {entry["status"] for entry in response["data"]["entries"]} == set(
                        item["statuses"]
                    )
                    assert "alice@example.com" not in json.dumps(response)
                elif item["expected"] == "pause":
                    assert response["ok"] is False
                    assert response["pause"]["reason"] == item["pause_reason"]
                    assert response["pause"]["requiresUserAction"] is True
                else:
                    assert response["ok"] is True
                    assert response["data"]["entries"] == []
                    assert response["data"]["applicationRecords"]
                    assert response["data"]["applicationRecords"][0]["signals"]["conflicting_statuses"] is True

                page.close()
        finally:
            browser.close()


def test_application_record_extraction_keeps_adjacent_record_without_later_status() -> None:
    from playwright.sync_api import sync_playwright

    html = """
    <main class="application-records">
      <article class="application-card">
        <h3>【2027 届校招】机器人软件工程师</h3>
        <p>北京校招 投递简历 2026-08-18</p>
        <p data-recruitops-application-status>流程终止</p>
        <time>2026-08-19</time>
      </article>
      <article class="application-card">
        <h3>【2027 届校招】机器人端到端评测工程师</h3>
        <p>官网投递 北京校招 投递简历 2026-08-18</p>
      </article>
    </main>
    """
    with sync_playwright() as playwright:
        browser = launch_fixture_browser(playwright)
        try:
            page = browser.new_page()
            page.set_content(html)
            page.add_script_tag(path=str(SRC / "application-records.js"))
            records = page.evaluate(
                "() => globalThis.RecruitOpsApplicationRecords.extract(document).records"
            )
        finally:
            browser.close()

    assert len(records) == 2
    by_title = {item["title"]: item for item in records}
    assert by_title["【2027 届校招】机器人软件工程师"]["status"] == "rejected"
    assert by_title["【2027 届校招】机器人端到端评测工程师"]["status"] == ""


def test_removed_oc_action_is_rejected_without_reactivating_production() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = launch_fixture_browser(playwright)
        try:
            page = browser.new_page()
            page.set_content("<main></main>")
            page.evaluate(
                """
                globalThis.chrome = {runtime: {id: "fixture-extension", onMessage: {
                  addListener(handler) { globalThis.__recruitopsMessageHandler = handler; }
                }}};
                """
            )
            for script in ("protocol.js", "actions.js", "content-script.js"):
                page.add_script_tag(path=str(SRC / script))
            response = page.evaluate(
                """() => new Promise((resolve) => {
                  globalThis.__recruitopsMessageHandler({
                    protocolVersion: globalThis.RecruitOpsProtocol.version,
                    type: globalThis.RecruitOpsProtocol.messageTypes.EXECUTE_CONTROLLED_ACTION,
                    requestId: "removed-oc",
                    commandAuthorized: true,
                    authorizedOrigin: location.origin,
                    tabId: 7,
                    action: "capture_oc_page",
                    selectorKey: "oc_company_table",
                    params: {page: 1, apply_filters: true},
                    actionTicket: "removed-oc-ticket"
                  }, {id: globalThis.chrome.runtime.id}, resolve);
                })"""
            )
        finally:
            browser.close()

    assert response["ok"] is False
    assert response["error"]["code"] == "ACTION_NOT_ALLOWED"
