"""Offline identity-gate regressions through the real content-script listener."""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright


SRC = Path(__file__).parents[1] / "extension" / "src"
DJI_PATH = "/candidate/applications/deliver-query/dji"
DJI_TEXT = (
    "查询投递记录 请进行身份认证！ 填写个人信息 "
    "请填写简历中的手机号码 手机号* +86 发送验证码使用邮箱验证"
)
DJI_HTML = f"<main><h1>查询投递记录</h1><form>{DJI_TEXT}<input type='tel'></form></main>"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(service_workers="block")
    context.route("**/*", lambda route: route.fulfill(
        body="<!doctype html><title>Recruitment fixture</title><body></body>",
        content_type="text/html; charset=utf-8",
    ))
    page = context.new_page()
    page.goto(f"https://careers.example.test{DJI_PATH}")
    page.evaluate("""() => {
      globalThis.__effects = [];
      for (const event of ["click", "input", "change", "submit"]) {
        document.addEventListener(event, () => __effects.push(event), true);
      }
      globalThis.chrome = {runtime: {id: "fixture-extension", onMessage: {
        addListener(handler) { globalThis.__handler = handler; }
      }}};
    }""")
    for name in ("protocol.js", "actions.js", "content-script.js"):
        page.add_script_tag(path=str(SRC / name))
    try:
        yield page
    finally:
        context.close()


def observe(page, *, overrides=None, sender="fixture-extension"):
    return page.evaluate("""({overrides, sender}) => new Promise(resolve => {
      const protocol = RecruitOpsProtocol;
      __handler({
        protocolVersion: protocol.version,
        type: protocol.messageTypes.EXECUTE_CONTROLLED_ACTION,
        requestId: "identity-fixture",
        commandAuthorized: true,
        authorizedOrigin: location.origin,
        tabId: 7,
        action: protocol.actionTypes.OBSERVE_APPLICATION_PAGE,
        selectorKey: protocol.selectorKeys.APPLICATION_PAGE,
        params: {include_vision: false},
        actionTicket: "identity-fixture-ticket",
        ...overrides
      }, {id: sender}, resolve);
    })""", {"overrides": overrides or {}, "sender": sender})


def set_body(page, html):
    page.evaluate("html => { document.body.innerHTML = html; }", html)


def assert_pause(response, reason):
    assert response["ok"] is False
    assert response["type"] == "extension.pause_state"
    assert response["state"]["reason"] == reason
    assert response["state"]["requiresUserAction"] is True
    assert response["pause"] == response["state"]
    assert "data" not in response


@pytest.mark.parametrize("html", [
    f"<main>{DJI_TEXT}</main>",
    DJI_HTML,
    "<main>请先完成身份认证！ 手机号 获取短信验证码</main>",
    "<main>请进行身份验证！ 邮箱 使用邮箱验证</main>",
    f"<div role='dialog'>{DJI_TEXT}</div>",
], ids=["exact-dji-text", "dji-form", "sms", "email", "auth-dialog"])
def test_identity_gate_pauses_before_observation(page, html):
    set_body(page, html)
    response = observe(page)
    assert_pause(response, "login_required")
    assert response["state"]["resumeAction"] == "resume_after_login"
    assert page.evaluate("__effects") == []


@pytest.mark.parametrize("html", [
    "<main>填写个人信息 请填写简历中的手机号码 手机号* +86 发送验证码使用邮箱验证</main>",
    "<main>简历填写说明 手机验证码仅用于确认联系方式，有效期5分钟。</main>",
    "<main>身份认证说明：手机号可用于发送验证码，也可使用邮箱验证。</main>",
    "<main>请进行身份认证！</main>",
    "<main>查询投递记录 技术面试</main>",
    "<main>简历 手机号 邮箱<input type='tel'><input type='email'></main>",
    "<main>发送验证码 手机号<input autocomplete='one-time-code'></main>",
    f"<main>查询投递记录 技术面试</main><div hidden>{DJI_TEXT}</div>",
    f"<main>查询投递记录 技术面试</main><div style='display:none'>{DJI_TEXT}</div>",
    f"<main>{'岗位介绍 ' * 200}<footer>{DJI_TEXT}</footer></main>",
], ids=["resume-contact", "code-help", "identity-help", "prompt-only", "path-only",
        "contact-inputs", "otp-alone", "hidden", "display-none", "footer-help"])
def test_non_gates_on_same_dji_path_are_not_login(page, html):
    set_body(page, html)
    response = observe(page)
    assert response["ok"] is True, response
    assert "pause" not in response


@pytest.mark.parametrize("challenge", [
    "<div data-captcha>Challenge</div>",
    "<div data-sitekey='fixture'>Challenge</div>",
    "<div data-recruitops-auth='captcha'>Challenge</div>",
    "<p>安全检查 请完成安全验证</p>",
    "<input name='captcha'><p>验证码</p>",
    "<input placeholder='验证码'>",
], ids=["captcha-marker", "sitekey", "auth-marker", "challenge-text", "captcha-input", "legacy-code-input"])
def test_captcha_keeps_priority_over_identity_gate(page, challenge):
    set_body(page, DJI_HTML + challenge)
    response = observe(page)
    assert_pause(response, "captcha_required")
    assert response["state"]["resumeAction"] == "resume_after_captcha"


@pytest.mark.parametrize(("html", "reason"), [
    ("<main>请先登录</main>", "login_required"),
    ("<input type='password'>", "login_required"),
    ("<div role='dialog'>确认当前页面</div>", "state_unclear"),
    ("<input type='password'><div data-captcha>Challenge</div>", "captcha_required"),
    ("<main>请勿向他人透露验证码</main>", "captcha_required"),
])
def test_existing_pause_rules_are_preserved(page, html, reason):
    set_body(page, html)
    assert_pause(observe(page), reason)


@pytest.mark.parametrize(("overrides", "sender", "code"), [
    ({"commandAuthorized": False}, "fixture-extension", "COMMAND_AUTHORIZATION_REQUIRED"),
    ({}, "other-extension", "CURRENT_TAB_REQUIRED"),
    ({"authorizedOrigin": "https://other.example.test"}, "fixture-extension", "SOURCE_NOT_ALLOWED"),
    ({"actionTicket": ""}, "fixture-extension", "ACTION_TICKET_REQUIRED"),
    ({"unexpected": True}, "fixture-extension", "ACTION_MESSAGE_INVALID"),
    ({"action": "click"}, "fixture-extension", "ACTION_NOT_ALLOWED"),
])
def test_identity_gate_does_not_bypass_authorization(page, overrides, sender, code):
    set_body(page, DJI_HTML)
    response = observe(page, overrides=overrides, sender=sender)
    assert response["ok"] is False
    assert response["error"]["code"] == code
    assert "pause" not in response
    assert page.evaluate("__effects") == []


def test_identity_pause_consumes_ticket_without_reading_or_mutating_controls(page):
    set_body(page, DJI_HTML)
    page.evaluate("""() => {
      for (const control of document.querySelectorAll("input")) {
        Object.defineProperty(control, "value", {
          get() { throw new Error("Must not read identity credentials"); },
          set() { throw new Error("Must not fill identity credentials"); }
        });
      }
    }""")
    assert_pause(observe(page), "login_required")
    assert observe(page)["error"]["code"] == "ACTION_TICKET_SINGLE_USE"
    assert page.evaluate("__effects") == []
