"""Native fill v1: synthetic intercepted pages, no server or real form writes."""

import json
import subprocess

import pytest

from test_desktop_browser import URL, browser, node_call, page  # noqa: F401


def mapping(target="candidate", value="Fixture Applicant", field_id="name"):
    return {"field_id": field_id, "target": {"id": target}, "value": value}


def preview(page, fields=None, url=URL):
    params = {"operation_id": "fill-fixture", "page_url": url, "fields": fields or [mapping()]}
    script = node_call("a.buildFormPreviewScript(input)", params)
    raw = page.evaluate(script)
    result = node_call("a.normalizeFormPreview(input.raw,input.params)", {"raw": raw, "params": params})
    return params, result


def fill_params(params, result):
    return {**params, "preview_id": result["preview_id"], "writes_enabled": True, "user_confirmed": True}


def fill(page, params, result):
    trusted = fill_params(params, result)
    script = node_call("a.buildFormFillScript(input)", trusted)
    raw = page.evaluate(script)
    return node_call("a.normalizeFormFill(input.raw,input.params)", {"raw": raw, "params": trusted})


def test_native_text_textarea_select_preview_then_fill(page):
    page.set_content("""<form><label for="candidate">Full name</label><input id="candidate">
      <textarea id="summary"></textarea><select id="degree"><option value="">Choose</option>
      <option value="masters">Masters</option></select><button type="submit">Apply</button></form>""")
    page.evaluate("""() => {
      window.events=[]; window.submissions=0;
      for(const type of ['input','change']) document.addEventListener(type,e=>events.push([type,e.target.id]));
      document.querySelector('form').addEventListener('submit',e=>{e.preventDefault();submissions++});
    }""")
    fields = [mapping(), mapping("summary", "Synthetic\nexperience", "summary"), mapping("degree", "masters", "degree")]
    params, result = preview(page, fields)
    assert result["status"] == "READY"
    assert page.locator("#candidate").input_value() == ""
    assert page.evaluate("events") == []
    filled = fill(page, params, result)
    assert filled["status"] == "FILLED"
    assert all(field["status"] == "FILLED" for field in filled["fields"])
    assert page.locator("#candidate").input_value() == "Fixture Applicant"
    assert page.locator("#summary").input_value() == "Synthetic\nexperience"
    assert page.locator("#degree").input_value() == "masters"
    assert page.evaluate("events") == [[event, target] for target in ["candidate", "summary", "degree"] for event in ["input", "change"]]
    assert page.evaluate("submissions") == 0
    assert filled["submission_attempted"] is False
    assert filled["database_updated"] is False
    assert "Fixture Applicant" not in json.dumps([result, filled])
    assert fill(page, params, result)["code"] == "PREVIEW_STALE"


@pytest.mark.parametrize("kind", ["text", "email", "tel", "url"])
def test_supported_native_input_types_and_unchanged_no_events(page, kind):
    page.set_content(f'<input id="candidate" type="{kind}" value="fixture">')
    page.evaluate("document.querySelector('input').addEventListener('input',()=>{throw Error('must not fire')})")
    params, result = preview(page, [mapping(value="fixture")])
    assert fill(page, params, result)["status"] == "UNCHANGED"


@pytest.mark.parametrize("html,status", [
    ('<input id="candidate" type="file">', "UNSUPPORTED"),
    ('<input id="candidate" type="submit">', "UNSUPPORTED"),
    ('<input id="candidate" type="checkbox">', "UNSUPPORTED"),
    ('<input id="candidate" type="number">', "UNSUPPORTED"),
    ('<input id="candidate" type="hidden">', "UNSUPPORTED"),
    ('<button id="candidate">Apply</button>', "UNSUPPORTED"),
    ('<select multiple id="candidate"><option>x</option></select>', "UNSUPPORTED"),
    ('<div id="candidate" contenteditable="true"></div>', "UNSUPPORTED"),
    ('<input id="candidate" role="combobox">', "UNSUPPORTED"),
    ('<input id="candidate" readonly>', "NOT_EDITABLE"),
    ('<input id="candidate" disabled>', "NOT_EDITABLE"),
    ('<fieldset disabled><input id="candidate"></fieldset>', "NOT_EDITABLE"),
    ('<div style="display:none"><input id="candidate"></div>', "NOT_EDITABLE"),
    ('<div style="opacity:0"><input id="candidate"></div>', "NOT_EDITABLE"),
    ('<div inert><input id="candidate"></div>', "NOT_EDITABLE"),
    ('<input id="candidate" autocomplete="one-time-code">', "SENSITIVE"),
    ('<label for="candidate">SMS verification code</label><input id="candidate">', "SENSITIVE"),
    ('<label for="candidate">验证码</label><input id="candidate">', "SENSITIVE"),
    ('<input id="candidate" name="otpCode">', "SENSITIVE"),
    ('<input id="candidate" name="pinCode">', "SENSITIVE"),
    ('<input id="candidate" name="code">', "SENSITIVE"),
    ('<input id="candidate" aria-label="Password">', "SENSITIVE"),
    ('<form action="/login"><input id="candidate"></form>', "SENSITIVE"),
    ('<input id="candidate" maxlength="3">', "VALUE_TOO_LONG"),
    ('<input id="candidate"><input id="candidate">', "AMBIGUOUS"),
    ('<p>No fields</p>', "NOT_FOUND"),
])
def test_unsafe_ambiguous_and_missing_fields_never_write(page, html, status):
    page.set_content(html)
    before = page.content()
    params, result = preview(page)
    assert result["status"] == "UNSUPPORTED"
    assert result["fields"][0]["status"] == status
    assert fill(page, params, result)["code"] == "PREVIEW_STALE"
    assert page.content() == before


@pytest.mark.parametrize("html,code", [
    ('<input type="password"><input id="candidate">', "LOGIN_REQUIRED"),
    ('<div data-captcha>Captcha</div><input id="candidate">', "CAPTCHA_REQUIRED"),
    ('<dialog open aria-modal="true">Blocked</dialog><input id="candidate">', "STATE_UNCLEAR"),
])
def test_login_captcha_overlay_gate_whole_plan(page, html, code):
    page.set_content(html)
    params, result = preview(page)
    assert result["status"] == "BLOCKED"
    assert result["code"] == code
    assert fill(page, params, result)["status"] == "BLOCKED"
    assert page.locator("#candidate").input_value() == ""


@pytest.mark.parametrize("options", [
    '<option value="other">Other</option>',
    '<option disabled value="wanted">Wanted</option>',
    '<optgroup disabled><option value="wanted">Wanted</option></optgroup>',
    '<option value="wanted">One</option><option value="wanted">Two</option>',
])
def test_select_requires_unique_enabled_exact_value(page, options):
    page.set_content(f'<select id="candidate">{options}</select>')
    _, result = preview(page, [mapping(value="wanted")])
    assert result["fields"][0]["status"] == "OPTION_NOT_FOUND"


def test_name_mapping_duplicate_alias_and_batch_rejection(page):
    page.set_content('<input id="candidate" name="full_name"><input id="other" type="file">')
    fields = [{"field_id": "name", "target": {"name": "full_name"}, "value": "Fixture"}]
    params, result = preview(page, fields)
    assert fill(page, params, result)["status"] == "FILLED"
    _, result = preview(page, fields + [mapping(field_id="same_element")])
    assert result["fields"][1]["status"] == "DUPLICATE_TARGET"
    params, result = preview(page, [mapping(), mapping("other", "never", "file")])
    assert result["status"] == "UNSUPPORTED"
    assert fill(page, params, result)["status"] == "BLOCKED"
    assert page.locator("#candidate").input_value() == "Fixture"


@pytest.mark.parametrize("change", [
    "document.querySelector('input').value='edited by user'",
    "document.querySelector('input').outerHTML='<input id=\"candidate\">'",
    "document.querySelector('input').setAttribute('placeholder','changed meaning')",
    "document.body.insertAdjacentHTML('beforeend','<input id=\"candidate\">')",
    "Date.now=()=>Number.MAX_SAFE_INTEGER",
])
def test_preview_revalidates_changes_before_any_write(page, change):
    page.set_content('<input id="candidate">')
    params, result = preview(page)
    page.evaluate(change)
    outcome = fill(page, params, result)
    assert outcome["status"] in ["BLOCKED", "UNSUPPORTED"]
    assert page.locator("#candidate").first.input_value() != "Fixture Applicant"


def test_changed_approved_value_and_superseded_receipt_fail(page):
    page.set_content('<input id="candidate">')
    params, old = preview(page)
    _, fresh = preview(page)
    assert fill(page, params, old)["code"] == "PREVIEW_STALE"
    assert fill(page, params, fresh)["code"] == "PREVIEW_STALE"
    params, fresh = preview(page)
    params["fields"][0]["value"] = "Unapproved replacement"
    assert fill(page, params, fresh)["code"] == "PREVIEW_STALE"
    assert page.locator("#candidate").input_value() == ""


@pytest.mark.parametrize("src", ["/child", "https://other.example/child"])
def test_fill_is_top_frame_only(page, src):
    page.set_content(f'<iframe src="{src}"></iframe>')
    params, result = preview(page)
    assert result["iframe_count"] == 1
    assert result["fields"][0]["status"] == "NOT_FOUND"
    script = node_call("a.buildFormPreviewScript(input)", params)
    assert page.frames[1].evaluate(script)["code"] == "FRAME_NOT_ALLOWED"
    script = node_call("a.buildFormFillScript(input)", fill_params(params, result))
    assert page.frames[1].evaluate(script)["code"] == "FRAME_NOT_ALLOWED"


@pytest.mark.parametrize("destination", [URL + "?job=other", URL + "#/other", "https://other.example/form"])
def test_full_url_navigation_blocks_fill(page, destination):
    page.set_content('<input id="candidate">')
    params, result = preview(page)
    page.goto(destination)
    page.set_content('<input id="candidate">')
    assert fill(page, params, result)["code"] == "SOURCE_NOT_ALLOWED"
    assert page.locator("#candidate").input_value() == ""


def test_event_handler_changes_next_field_reports_partial_not_success(page):
    page.set_content('<input id="candidate"><input id="next">')
    params, result = preview(page, [mapping(), mapping("next", "Second", "second")])
    page.evaluate("document.querySelector('#candidate').addEventListener('input',()=>document.querySelector('#next').type='password')")
    filled = fill(page, params, result)
    assert filled["status"] == "PARTIAL"
    assert page.locator("#next").input_value() == ""
    assert filled["fields"][1]["status"] == "NOT_ATTEMPTED"


def test_controlled_input_rejection_is_partial(page):
    page.set_content('<input id="candidate">')
    page.evaluate("document.querySelector('input').addEventListener('input',e=>e.target.value='')")
    params, result = preview(page)
    filled = fill(page, params, result)
    assert filled["status"] == "PARTIAL"
    assert filled["code"] == "VALUE_REJECTED"


def test_later_event_reverts_earlier_value_cannot_report_success(page):
    page.set_content('<input id="candidate"><input id="next">')
    page.evaluate("document.querySelector('#next').addEventListener('change',()=>document.querySelector('#candidate').value='')")
    params, result = preview(page, [mapping(), mapping("next", "Second", "second")])
    filled = fill(page, params, result)
    assert filled["status"] == "PARTIAL"
    assert filled["fields"][0]["status"] == "FAILED"


def test_dynamic_duplicate_created_by_event_blocks_remaining_write(page):
    page.set_content('<input id="candidate"><input id="next">')
    page.evaluate("document.querySelector('#candidate').addEventListener('change',()=>document.body.insertAdjacentHTML('beforeend','<input id=next>'))")
    params, result = preview(page, [mapping(), mapping("next", "Second", "second")])
    filled = fill(page, params, result)
    assert filled["status"] == "PARTIAL"
    assert filled["fields"][1]["status"] == "NOT_ATTEMPTED"
    assert page.locator("#next").first.input_value() == ""


@pytest.mark.parametrize("flags", [{}, {"writes_enabled": True}, {"user_confirmed": True},
                                     {"writes_enabled": False, "user_confirmed": True}])
def test_writes_require_both_explicit_flags(flags):
    params = {"operation_id": "fixture", "page_url": URL, "fields": [mapping()], "preview_id": "receipt", **flags}
    assert node_call("(() => {try {a.buildFormFillScript(input);return 'bad'}catch(e){return e.code}})()", params) == "FORM_FILL_DISABLED"


@pytest.mark.parametrize("change", [
    {"callback": "alert(1)"}, {"page_url": "javascript:alert(1)"},
    {"fields": []}, {"fields": [mapping()] * 26},
    {"fields": [{"field_id": "x", "target": {"selector": "input"}, "value": "x"}]},
    {"fields": [{"field_id": "x", "target": {"id": "x", "name": "x"}, "value": "x"}]},
    {"fields": [mapping(value="x" * 2001)]},
])
def test_form_generator_rejects_unbounded_or_executable_input(change):
    params = {"operation_id": "fixture", "page_url": URL, "fields": [mapping()], **change}
    with pytest.raises(subprocess.CalledProcessError):
        node_call("a.buildFormPreviewScript(input)", params)


def test_literal_values_never_become_executable_code(page):
    page.set_content('<input id="candidate">')
    literal = "');globalThis.pwned=true;//</script>"
    params, result = preview(page, [mapping(value=literal)])
    assert fill(page, params, result)["status"] == "FILLED"
    assert page.locator("#candidate").input_value() == literal
    assert page.evaluate("globalThis.pwned === undefined")


def test_form_normalizer_rejects_forged_or_extra_evidence(page):
    page.set_content('<input id="candidate">')
    params, result = preview(page)
    for patch in [{"operation_id": "another"}, {"cookies": "no"}, {"database_updated": True},
                  {"submission_attempted": True}, {"status": "FILLED"},
                  {"fields": [{"field_id": "other", "status": "READY", "match_count": 1}]},
                  {"fields": [{"field_id": "name", "status": "READY", "match_count": 1, "value": "no"}]}]:
        with pytest.raises(subprocess.CalledProcessError):
            node_call("a.normalizeFormPreview(input.raw,input.params)", {"raw": {**result, **patch}, "params": params})


def test_preview_receipt_is_private_to_dedicated_isolated_world(page):
    page.set_content('<input id="candidate">')
    session = page.context.new_cdp_session(page)
    frame_id = session.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
    world = session.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "desktop-fill-fixture"})
    params = {"operation_id": "isolated", "page_url": URL, "fields": [mapping()]}

    def evaluate(script):
        result = session.send("Runtime.evaluate", {"expression": script, "contextId": world["executionContextId"], "returnByValue": True})
        assert "exceptionDetails" not in result
        return result["result"]["value"]

    result = evaluate(node_call("a.buildFormPreviewScript(input)", params))
    assert result["status"] == "READY"
    assert page.evaluate("globalThis.__recruitopsDesktopNativeFormV1 === undefined")
    assert evaluate(node_call("a.buildFormFillScript(input)", fill_params(params, result)))["status"] == "FILLED"
    assert page.locator("#candidate").input_value() == "Fixture Applicant"
    session.detach()
