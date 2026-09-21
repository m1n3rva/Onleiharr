import pytest


@pytest.mark.browser
def test_playwright_chromium_smoke():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content("<html><head><title>Smoke Test</title></head><body>ok</body></html>")
        assert page.title() == "Smoke Test"
        browser.close()
