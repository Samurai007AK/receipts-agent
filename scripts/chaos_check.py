"""Prove, through a real browser, that every technical chaos hazard breaks a naive invoice save.

Run: .venv\\Scripts\\python scripts\\chaos_check.py

For a baseline (no chaos) and each technical hazard (save_500, session_expiry, renamed_button,
date_rule) this script resets the company app with that one flag, signs in through the login form,
fills the New invoice form exactly the way a naive automation script would (ISO date, click "Save")
and asserts what really happened in the browser, in the ledger and in the chaos log. If any hazard
fails to break the save, the agent's later "recovery" would be theatre, so this exits non-zero.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
from playwright.sync_api import Browser, Page, Response, TimeoutError as PlaywrightTimeout, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from checks._servers import check, company_app, isolated_env, reset  # noqa: E402

INVOICE = {"Vendor": "Acme Corp", "Invoice number": "INV-1042", "Amount": "1240.00", "Due date": "2026-11-02"}
NAIVE_CLICK_MS = 2_000  # how long a naive script would wait for its "Save" button before giving up
ALERT_500 = "500 Internal Server Error — the posting service timed out. Please try again."
ALERT_EXPIRED = "Your session expired. Please sign in again."
ALERT_DATE = "Due date must be in DD/MM/YYYY format"


def ledger_rows(base: str) -> list[dict]:
    return httpx.get(f"{base}/api/ledger", params={"invoice_no": INVOICE["Invoice number"]}, timeout=10).json()


def hazards_logged(base: str) -> list[str]:
    return [entry["hazard"] for entry in httpx.get(f"{base}/api/chaos/log", timeout=10).json()]


def alerts(page: Page) -> list[str]:
    return [text.strip() for text in page.get_by_role("alert").all_inner_texts()]


def open_invoice_form(browser: Browser, base: str, chaos: dict) -> Page:
    """Reset the app with `chaos`, sign in through the real login form, open New invoice and fill it."""
    reset(base, chaos)
    page = browser.new_context(base_url=base).new_page()
    page.set_default_timeout(15_000)
    page.goto("/login")
    page.get_by_label("Username").fill("clerk")
    page.get_by_label("Password").fill("receipts123")
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(f"{base}/")
    page.goto("/invoices/new")
    for label, value in INVOICE.items():
        page.get_by_label(label, exact=True).fill(value)
    return page


def naive_save(page: Page) -> tuple[int, Response]:
    """Click Save like a naive script; return (status of the POST itself, response of the page we land on)."""
    with page.expect_navigation() as landed:
        with page.expect_response(lambda r: r.request.method == "POST") as posted:
            page.get_by_role("button", name="Save", exact=True).click()
    return posted.value.status, landed.value


def baseline(browser: Browser, base: str) -> None:
    page = open_invoice_form(browser, base, {})
    post_status, landed = naive_save(page)
    check(post_status == 303 and "/invoices/" in page.url and page.url.endswith("?posted=1"),
          f"baseline: Save redirects to the posted invoice (POST {post_status}, now at {page.url})")
    check(landed.status == 200 and page.get_by_text("Invoice posted").is_visible(),
          "baseline: 'Invoice posted' banner is shown")
    rows = ledger_rows(base)
    check(len(rows) == 1 and rows[0]["amount"] == 1240.0 and rows[0]["due_date"] == "2026-11-02"
          and rows[0]["vendor"] == "Acme Corp" and rows[0]["source"] == "web",
          f"baseline: /api/ledger has the new INV-1042 row ({rows})")
    check(hazards_logged(base) == [], "baseline: chaos log is empty")
    print("  WORKS   baseline: with chaos off a naive Save posts INV-1042 to the ledger", flush=True)


def save_500(browser: Browser, base: str) -> None:
    page = open_invoice_form(browser, base, {"save_500": 1})
    post_status, landed = naive_save(page)
    check(post_status == 500 and landed.status == 500, f"save_500: Save answers HTTP 500 (got {post_status})")
    check(ALERT_500 in alerts(page), f"save_500: timeout alert is shown ({alerts(page)!a})")  # !a: alert has an em dash; Windows pipes are cp1252
    check(ledger_rows(base) == [], "save_500: ledger unchanged")
    check("save_500" in hazards_logged(base), "save_500: hazard recorded in /api/chaos/log")
    print("  BROKEN  save_500: Save returned HTTP 500 'posting service timed out'; nothing written", flush=True)


def session_expiry(browser: Browser, base: str) -> None:
    page = open_invoice_form(browser, base, {"session_expiry": True})
    post_status, _ = naive_save(page)
    check(post_status == 303 and page.url.startswith(f"{base}/login?") and "expired=1" in page.url,
          f"session_expiry: Save bounces to the login page (POST {post_status}, now at {page.url})")
    check(ALERT_EXPIRED in alerts(page), f"session_expiry: expired-session alert is shown ({alerts(page)})")
    check(ledger_rows(base) == [], "session_expiry: ledger unchanged")
    check("session_expiry" in hazards_logged(base), "session_expiry: hazard recorded in /api/chaos/log")
    print("  BROKEN  session_expiry: Save logged the clerk out and landed on /login; nothing written", flush=True)


def renamed_button(browser: Browser, base: str) -> None:
    page = open_invoice_form(browser, base, {"renamed_button": True})
    save = page.get_by_role("button", name="Save", exact=True)
    check(save.count() == 0, "renamed_button: the form has no 'Save' button")
    try:
        save.click(timeout=NAIVE_CLICK_MS)
        clicked = True
    except PlaywrightTimeout:
        clicked = False
    check(not clicked, f"renamed_button: a naive click on 'Save' times out after {NAIVE_CLICK_MS} ms")
    more = page.locator("summary", has_text="More actions")
    submit = page.get_by_role("button", name="Submit for posting", exact=True)
    check(more.is_visible() and not submit.is_visible(),
          "renamed_button: a 'More actions' summary is shown, its button hidden")
    more.click()
    check(submit.is_visible(), "renamed_button: opening 'More actions' reveals 'Submit for posting'")
    check(ledger_rows(base) == [], "renamed_button: ledger unchanged")
    check("renamed_button" in hazards_logged(base), "renamed_button: hazard recorded in /api/chaos/log")
    print("  BROKEN  renamed_button: no Save button; the submit hides under 'More actions' as "
          "'Submit for posting'", flush=True)


def date_rule(browser: Browser, base: str) -> None:
    page = open_invoice_form(browser, base, {"date_rule": True})
    post_status, landed = naive_save(page)
    check(post_status == 422 and landed.status == 422, f"date_rule: Save answers HTTP 422 (got {post_status})")
    check(ALERT_DATE in alerts(page), f"date_rule: DD/MM/YYYY alert is shown ({alerts(page)})")
    check(ledger_rows(base) == [], "date_rule: ledger unchanged")
    check("date_rule" in hazards_logged(base), "date_rule: hazard recorded in /api/chaos/log")
    print("  BROKEN  date_rule: ISO due date 2026-11-02 rejected (422), app now demands DD/MM/YYYY", flush=True)


def main() -> None:
    with company_app(env=isolated_env()) as base, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for scenario in (baseline, save_500, session_expiry, renamed_button, date_rule):
                scenario(browser, base)
        finally:
            browser.close()
    print("CHAOS CHECK PASSED", flush=True)


if __name__ == "__main__":
    main()
