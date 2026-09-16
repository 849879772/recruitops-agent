import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
EXTENSION = ROOT / "extension"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_source(name: str) -> str:
    return (EXTENSION / "src" / name).read_text(encoding="utf-8")


def test_manifest_is_mv3_with_minimum_permissions_and_local_api_only() -> None:
    manifest = read_json(EXTENSION / "manifest.json")

    assert manifest["manifest_version"] == 3
    assert manifest["version"] == "0.3.21"
    assert set(manifest["permissions"]) == {"activeTab", "scripting", "storage", "tabs"}
    assert set(manifest["host_permissions"]) == {
        "http://127.0.0.1/*",
        "http://localhost/*",
        "http://[::1]/*",
        "<all_urls>",
    }
    assert "content_scripts" not in manifest
    assert "optional_host_permissions" not in manifest
    assert not ({"cookies", "history", "webRequest", "downloads"} & set(manifest["permissions"]))


def test_options_keep_local_configuration_and_bridge_diagnostics() -> None:
    options_html = (EXTENSION / "options.html").read_text(encoding="utf-8")
    options_js = (EXTENSION / "options.js").read_text(encoding="utf-8")

    assert "allowed-origins" not in options_html
    assert "来源白名单" not in options_html
    assert 'origins: ["<all_urls>"]' in options_js
    assert "chrome.permissions.contains" in options_js
    assert "chrome.storage.local.get" in options_js
    assert "chrome.storage.local.set" in options_js
    assert "CONFIG_TEST" in options_js
    assert "bridgeMessage" in options_js
    assert "retry_scheduled" in options_js


def test_protocol_has_one_active_bridge_status_action_and_no_legacy_http_status_flow() -> None:
    protocol = read_json(EXTENSION / "protocol.json")
    messages = protocol["messages"]
    bridge = protocol["browserBridge"]

    assert protocol["version"] == 4
    assert messages["AUTHORIZE_CURRENT_TAB"]["requiresUserGesture"] is True
    assert messages["READ_SANITIZED_DOM"]["requiresUserAuthorization"] is True
    assert messages["SUBMIT_DOM_OBSERVATION"]["localOnly"] is True
    assert messages["SUBMIT_APPLICATION_CAPTURE"]["localOnly"] is True
    assert "REQUEST_CONTROLLED_ACTION" not in messages
    assert "controlledActionApproval" not in protocol
    assert "applicationStatusReview" not in protocol
    protocol_text = json.dumps(protocol)
    assert "/api/browser/actions" not in protocol_text
    assert "/api/browser/application-status/observations" not in protocol_text
    assert messages["EXECUTE_CONTROLLED_ACTION"] == {
        "type": "extension.execute_controlled_action",
        "direction": "service_worker_to_content_script",
        "activeBridgeOnly": True,
        "requiresCommandAuthorization": True,
        "containsApprovalToken": False,
        "singleUseActionTicket": True,
        "returnsSanitizedEvidenceOnly": True,
        "databaseWrite": False,
        "payload": [
            "protocolVersion",
            "type",
            "requestId",
            "commandAuthorized",
            "authorizedOrigin",
            "tabId",
            "action",
            "selectorKey",
            "params",
            "actionTicket",
        ],
    }

    assert set(protocol["actions"]) == {"observe_application_page"}
    action = protocol["actions"]["observe_application_page"]
    assert action == {
        "commandType": "observe_application_status_page",
        "selectorKey": "application_page",
        "parameters": ["include_vision", "vision_fallback_reason", "retain_on_pause"],
        "evidenceOnly": True,
    }
    assert set(protocol["selectorWhitelist"]) == {"application_page"}

    assert bridge["transport"] == "persistent_websocket"
    assert bridge["path"] == "/browser-bridge"
    assert bridge["authentication"]["tokenInUrl"] is False
    assert bridge["authentication"]["tokenInMessages"] is False
    assert bridge["commandEnvelope"]["commandRequired"] == [
        "action",
        "selector_key",
        "params",
        "page_url",
        "origin",
        "application_id",
        "application_ids",
    ]
    assert bridge["commandEnvelope"]["typedActions"][0] == {
        "commandType": "observe_application_status_page",
        "action": "observe_application_page",
        "selectorKey": "application_page",
        "parameters": ["include_vision", "vision_fallback_reason", "retain_on_pause"],
    }
    assert len(bridge["commandEnvelope"]["typedActions"]) == 1
    assert bridge["result"] == {
        "kind": "structured_application_records_and_optional_vision_evidence",
        "databaseWrite": False,
        "successMeans": "evidence_collected_only",
    }


def test_extension_source_has_no_manual_status_route_or_credential_escape_hatch() -> None:
    background = read_source("background.js")
    actions = read_source("actions.js")
    application_records = read_source("application-records.js")
    content_script = read_source("content-script.js")

    assert "executeCommandAuthorizedAction" in background
    assert "commandAuthorized: true" in background
    assert "new WebSocket(bridgeUrl)" in background
    assert "crypto.subtle.importKey" in background
    assert "HMAC" in background
    assert "operation.dispatch" in background
    assert 'type: "ack"' in background
    assert 'type: "progress"' in background
    assert 'type: "result"' in background
    assert 'type: "heartbeat"' in background
    assert "scheduleBridgeReconnect" in background
    assert "database_updated: false" in background
    assert "/api/browser/observations" in background
    assert "/api/browser/application-captures" in background
    assert "/api/browser/vision" in background
    assert "captureVisiblePageVision" in background
    assert "observation.vision" not in background

    for source in (background, actions, application_records, content_script):
        assert "approvalToken" not in source
        assert "approval_token" not in source
        assert "eval(" not in source
        assert "new Function" not in source
    for fragment in (
        "REQUEST_CONTROLLED_ACTION",
        "/api/browser/actions",
        "/api/browser/application-status/observations",
        "pollCommandAuthorizedActions",
        "chrome.alarms",
        "document.cookie",
        "chrome.cookies",
    ):
        assert fragment not in background + actions + application_records + content_script

    assert "fetch(" not in content_script
    assert "WebSocket" not in content_script
    assert "querySelector(message" not in content_script
    assert "requestSubmit" not in content_script
    assert ".submit(" not in content_script
    assert "userGesture" not in content_script
    assert "clickWhitelistedElement" not in content_script
    assert "performFilter" not in content_script


def test_passive_capture_and_status_fixtures_remain_available() -> None:
    protocol = read_json(EXTENSION / "protocol.json")
    fixtures = read_json(EXTENSION / "fixtures" / "application-status-fixtures.json")

    assert protocol["applicationCapture"]["createsApprovalPreviewOnly"] is True
    assert fixtures["selector_key"] == "application_page"
    assert all((EXTENSION / "fixtures" / item["file"]).is_file() for item in fixtures["fixtures"])
