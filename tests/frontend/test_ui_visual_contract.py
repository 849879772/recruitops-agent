"""Structural regression checks for the screenshot-led visual update."""

from html.parser import HTMLParser
from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parents[2] / "apps" / "web"


class NavigationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = None
        self.buttons = []
        self.asset_versions = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "button" and "nav-item" in attrs.get("class", "").split():
            self.current = {"view": attrs.get("data-view"), "icons": []}
            self.buttons.append(self.current)
        if tag == "svg" and self.current is not None:
            self.current["icons"].append(attrs)
        asset = attrs.get("src", attrs.get("href", ""))
        if asset.startswith(("./styles.css?", "./app.js?", "./company-sources.js?", "./configuration.js?")):
            self.asset_versions.append(asset.split("?v=", 1)[1])

    def handle_endtag(self, tag):
        if tag == "button":
            self.current = None


def parse_navigation():
    parser = NavigationParser()
    parser.feed((WEB_ROOT / "index.html").read_text(encoding="utf-8"))
    return parser


def test_navigation_keeps_business_routes_and_decorative_line_icons():
    buttons = parse_navigation().buttons
    assert {item["view"] for item in buttons} >= {
        "jobs", "companies", "applications", "schedule", "automations",
        "assistant", "mail", "approvals", "tasks", "integrations",
    }
    for button in buttons:
        assert len(button["icons"]) == 1, button["view"]
        icon = button["icons"][0]
        assert icon.get("aria-hidden") == "true"
        assert icon.get("focusable") == "false"
        assert icon.get("viewbox") == "0 0 24 24"
        assert "ui-icon" in icon.get("class", "").split()


def test_script_and_style_versions_are_updated_together():
    versions = parse_navigation().asset_versions
    assert len(versions) == 4
    assert versions == ["0.4.20-20260915"] * 4
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    assert '<script defer src="./company-sources.js?v=0.4.20-20260915"></script>' in html
    assert '<script defer src="./knowledge.js?v=0.4.20-20260915"></script>' in html


def test_assistant_leads_personal_navigation_and_messages_do_not_stretch():
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    personal = html.split('<p class="nav-group-label">我的</p>', 1)[1]
    assert personal.split('data-view="', 1)[1].startswith('assistant"')
    css = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")
    rules = css.split('.agent-message-list {', 1)[1].split('}', 1)[0]
    assert 'align-content: start;' in rules
    assert 'grid-auto-rows: max-content;' in rules


def test_jobs_metrics_use_colored_icon_blocks():
    source = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    for icon_name in ("jobs", "companies", "match", "pending"):
        marker = f"metric-icon metric-icon--{icon_name}"
        assert marker in source
        icon = source.split(marker, 1)[1].split("</span>", 1)[0]
        assert '<svg class="ui-icon"' in icon
        assert 'viewBox="0 0 24 24"' in icon
        assert 'aria-hidden="true"' in icon
        assert 'focusable="false"' in icon


def test_application_event_success_refreshes_schedule():
    source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    event_handler = source.split('submit(eventForm, "添加日程", async () => {', 1)[1].split("    editor.appendChild(eventForm);", 1)[0]
    assert 'await api(LOCAL_SCHEDULE_EVENTS_URL, {' in event_handler
    assert 'method: "POST"' in event_handler
    assert "await loadFullSchedule();" in event_handler


def test_featured_jobs_default_fold_preserves_expandable_analysis():
    html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    assert '<details class="workspace-section featured-jobs-section" aria-labelledby="featured-jobs-heading">' in html
    assert 'id="featured-job-list"' in html
    assert 'element("details", "featured-job-disclosure")' in source
    assert 'analysisDisclosure.append(' in source
    assert 'card.append(header, actions, analysisDisclosure)' in source


def test_job_actions_keep_table_cell_and_nonshrinking_inner_group():
    source = (WEB_ROOT / "app.js").read_text(encoding="utf-8")
    css = (WEB_ROOT / "styles.css").read_text(encoding="utf-8")
    assert 'element("div", "job-table-action-group")' in source
    assert 'actionCell.appendChild(actionGroup)' in source
    assert '.job-table-actions {\n  display: table-cell;' in css
    assert '.job-table-action-group > button {\n  flex: 0 0 auto;\n  white-space: nowrap;' in css
