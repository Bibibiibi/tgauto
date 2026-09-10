"""Browser layout checks with synthetic data; never connects to a real service.

Requires: pip install playwright && playwright install chromium
Run: python tests/ui_smoke.py
"""
import json
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright


HTML = (Path(__file__).resolve().parents[1] / "web/index.html").read_text()
TASKS = [dict(bot=name, command="/checkin", button="", enabled=True,
              timeout=30, click_wait=0, pause_after=2)
         for name in ["@daily_bot", "@cloud_bot", "@points_bot"]]
HISTORY = [dict(bot=task["bot"], status="success" if i != 1 else "timeout",
                reward={"amount": 6, "unit": "bet"} if i == 0 else ({"amount": 7, "unit": "咪咪"} if i == 2 else None),
                balance={"amount": 137, "unit": "bet"} if i == 0 else ({"amount": 148, "unit": "咪咪"} if i == 2 else None),
                points=None, last_run_at="2026-09-10T02:15:00Z",
                trigger="scheduled") for i, task in enumerate(TASKS)]
WEBSITE = dict(name="NodeSeek 签到", enabled=True, schedule_enabled=True,
               cookie_configured=True, reward_mode="fixed", updated_at="2026-09-10T02:00:00Z",
               login_url="https://www.nodeseek.com/signIn.html",
               schedule=dict(enabled=True, window="10:00-13:00", timezone="Asia/Shanghai",
                             next_run_at="2026-09-11T03:24:00Z"),
               last_result=dict(status="success", message="签到成功，获得 5 个鸡腿",
                                finished_at="2026-09-10T02:15:00Z", status_code=200,
                                trigger="scheduled", response_preview='{"success": true}'))
STATUS = dict(running=False, bot_history=HISTORY, results=[],
              schedule=dict(window="00:00-00:10", timezone="Asia/Shanghai",
                            next_run_at="2026-09-10T16:06:00Z"),
              logs=[dict(time="2026-09-10T02:15:00Z", message="[INFO] 示例运行记录")])


def main():
    screenshots = Path(tempfile.mkdtemp(prefix="tgauto-ui-"))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, color_scheme="light")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def route(request):
            path = request.request.url.removeprefix("http://tgauto.test")
            if path == "/":
                request.fulfill(body=HTML, content_type="text/html")
                return
            fixtures = {
                "/api/tasks": {"tasks": TASKS}, "/api/status": STATUS,
                "/api/settings": {"api_id": "123456", "api_hash_configured": True},
                "/api/bot-profiles": {"profiles": {}},
                "/api/website/settings": WEBSITE, "/api/website/status": WEBSITE,
                "/api/proxy/status": {"configured": True, "selected": "测试节点", "enabled": True,
                                      "nodes": [{"name": "DIRECT", "type": "DIRECT", "alive": True, "delay": 0},
                                                {"name": "测试节点", "type": "AnyTLS", "alive": True, "delay": 88}],
                                      "mihomo": {"running": True, "version": "test"}},
            }
            request.fulfill(body=json.dumps(fixtures.get(path, {})), content_type="application/json")

        page.route("**/*", route)
        page.goto("http://tgauto.test/")
        page.wait_for_function("document.getElementById('statBots').textContent === '3'")
        assert page.locator("#dashboardRows").get_by_text("6 bet", exact=True).count() == 1
        assert page.locator("#dashboardRows").get_by_text("7 咪咪", exact=True).count() == 1
        assert page.locator("#dashboardRows").get_by_text("137 bet", exact=True).count() == 1
        assert page.locator("#dashboardRows").get_by_text("148 咪咪", exact=True).count() == 1
        assert page.locator("#statRewardCaption").text_content() == "6 bet · 7 咪咪"
        page.screenshot(path=str(screenshots / "desktop-light.png"), full_page=True)
        for width in [1440, 390]:
            page.set_viewport_size({"width": width, "height": 1000 if width == 1440 else 844})
            for theme in ["light", "dark"]:
                page.evaluate("theme => applyTheme(theme)", theme)
                for nav in ["home", "tasks", "website", "website-settings", "settings", "subscription", "logs"]:
                    page.evaluate("nav => selectNav(nav)", nav)
                    page.wait_for_timeout(70)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, theme, nav)
                    assert page.locator("h1:visible").count() == 1, (width, theme, nav)
                page.evaluate("selectNav('website-settings')")
                page.locator("#websiteRewardMode").select_option("random")
                assert page.locator("#websiteRewardMode").input_value() == "random"
                page.evaluate("selectNav('subscription')")
                assert page.locator(".proxy-delay-test").count() == 1
                assert page.locator(".proxy-delay-test").first.text_content() == "测速"
            page.evaluate("selectNav('home')")
            page.screenshot(path=str(screenshots / f"{'desktop' if width == 1440 else 'mobile'}-dark.png"), full_page=True)
        page.evaluate("applyTheme('light'); selectNav('website-settings')")
        page.screenshot(path=str(screenshots / "mobile-settings.png"), full_page=True)
        page.reload()
        page.locator("#websiteRewardMode").wait_for(state="visible")
        assert page.locator("#consoleWebsiteSettingsView").is_visible()
        # Exercise real DOM state preservation, not only the unit-test stub.
        page.evaluate("selectNav('tasks')")
        page.locator('#tasks input[data-k="command"]').first.fill("/edited")
        page.locator("#add").click()
        assert page.locator('#tasks input[data-k="command"]').first.input_value() == "/edited"
        assert not errors, errors
        browser.close()
    print("28 view/theme/viewport checks, refresh persistence and form interactions: passed")
    print(screenshots)


if __name__ == "__main__":
    main()
