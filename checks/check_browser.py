"""Self-check for browser.py against the real company app and gateway (no LLM).

Run: .venv\\Scripts\\python checks\\check_browser.py            -> BROWSER VERIFIED
     .venv\\Scripts\\python checks\\check_browser.py hazards    -> BROWSER HAZARDS VERIFIED

Default: login flow, snapshot format, guarded submit (blocked without / with a mismatching proposal,
verified with a matching one), duplicate submit, dispatch() end to end, fill_form errors and the
RECEIPTS_CRASH_AFTER_SUBMIT exit. Hazards: HTTP 500, session expiry, date rule, renamed button and
a missing element each produce the right detection line, and the agent-style recovery succeeds.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from checks._servers import PY, ROOT, check, company_app, fail, isolated_env, reset  # noqa: E402

INVOICE = {"vendor": "Acme Corp", "invoice_no": "INV-1042", "amount": "1240.00", "due_date": "2026-11-02"}
BY_LABEL = {"Vendor": "Acme Corp", "Invoice number": "INV-1042", "Amount": "1240.00", "Due date": "2026-11-02"}

CRASH_SCRIPT = r"""
import os, sys
sys.path.insert(0, os.getcwd())
from types import SimpleNamespace
import browser, gateway
b = browser.Browser(os.environ["RECEIPTS_APP_URL"])
ctx = SimpleNamespace(run_id="chk-crash", browser=b)
gateway.dispatch(ctx, "goto", {"path": "/login"})
gateway.dispatch(ctx, "fill_form", {"fields": {"Username": "clerk", "Password": "receipts123"}})
gateway.dispatch(ctx, "click", {"target": "Sign in"})
fields = {"vendor": "Globex Inc", "invoice_no": "INV-7731", "amount": "2310.50", "due_date": "2026-10-28"}
assert gateway.propose("chk-crash", "invoice", fields, "inbox:4")["status"] == "ALLOWED"
gateway.dispatch(ctx, "goto", {"path": "/invoices/new"})
gateway.dispatch(ctx, "fill_form", {"fields": fields})
print(gateway.dispatch(ctx, "click", {"target": "Save"}))
sys.exit(0)
"""


def make_ctx(run_id: str, b: Any) -> SimpleNamespace:
    events: list = []
    return SimpleNamespace(run_id=run_id, browser=b, events=events,
                           log=lambda kind, **data: events.append({"type": kind, **data}))


def ledger(base: str, invoice_no: str = "INV-1042") -> list[dict]:
    return httpx.get(f"{base}/api/ledger", params={"invoice_no": invoice_no}, timeout=10).json()


def lines(out: str) -> list[str]:
    return out.splitlines()


def login(ctx: SimpleNamespace) -> None:
    import gateway
    out = gateway.dispatch(ctx, "goto", {"path": "/login"})
    if not out.startswith("OK: /login (HTTP 200)") or "DETECTED" in out or "NOTE" in out:
        fail(f"going to /login on purpose must not report a problem:\n{out}")
    gateway.dispatch(ctx, "fill_form", {"fields": {"Username": "clerk", "Password": "receipts123"}})
    out = gateway.dispatch(ctx, "click", {"target": "Sign in"})
    if not out.startswith('OK: clicked "Sign in" → / (HTTP 200)'):
        fail(f"login failed:\n{out}")


def open_filled_form(ctx: SimpleNamespace, values: dict | None = None) -> str:
    import gateway
    gateway.dispatch(ctx, "goto", {"path": "/invoices/new"})
    return gateway.dispatch(ctx, "fill_form", {"fields": values or BY_LABEL})


def default_mode(base: str, b: Any) -> None:
    import browser
    import gateway

    reset(base)
    ctx = make_ctx("chk-default", b)

    print("login")
    out = browser.goto(ctx, path="/")
    check(lines(out)[0] == "OK: /login?next=/ (HTTP 200)", "goto / while signed out lands on the login page")
    check(lines(out)[1].startswith("NOTE: login required") and "session_expired" not in out,
          "first visit says login required, not session expired")
    browser.fill_form(ctx, fields={"Username": "clerk", "Password": "wrong"})
    out = browser.click(ctx, target="Sign in")
    check(lines(out)[0] == 'OK: clicked "Sign in" → /login (HTTP 401)'
          and "DETECTED: validation_error: Invalid username or password." in out and "session_expired" not in out,
          "a rejected sign-in is a validation_error, not a session expiry")
    out = browser.fill_form(ctx, fields={"Username": "clerk", "password": "receipts123"})
    check(lines(out)[0] == "OK: filled 2 fields" and "receipts123" not in out and 'password = "***"' in out,
          "fill_form by label and by name; the password is masked")
    out = browser.click(ctx, target="Sign in")
    check(lines(out)[0] == 'OK: clicked "Sign in" → / (HTTP 200)' and "PAGE / (HTTP 200)" in out,
          "clicking Sign in logs in and reports the landing page")

    print("snapshot")
    out = browser.goto(ctx, path="/invoices/new")
    snap = lines(out)
    check(snap[0] == "OK: /invoices/new (HTTP 200)" and snap[1].startswith("PAGE /invoices/new (HTTP 200) — New invoice"),
          "status line then PAGE line")
    for want in ("FORM record=invoice action=/invoices/new", '  - Vendor [vendor] = ""',
                 '  - Invoice number [invoice_no] = ""', '  - Amount [amount] = "" (placeholder 0.00)',
                 '  - Due date [due_date] = "" (placeholder YYYY-MM-DD)', "  buttons: Save"):
        check(want in snap, f"snapshot has line {want!r}")
    check(any(x.startswith("LINKS: ") and "Inbox -> /inbox" in x and "New invoice -> /invoices/new" in x for x in snap),
          "LINKS line with relative hrefs")
    check(any(x.startswith("TEXT: New invoice") for x in snap) and "ALERT" not in out and "DETECTED" not in out,
          "TEXT line, no alerts or detections on a clean form")
    check(len(browser.read_page(ctx)) <= 2500, "snapshot stays under 2500 chars")
    check("[next]" not in browser.read_page(ctx), "hidden fields are omitted")

    print("guarded submit")
    out = browser.click(ctx, target="Save")
    check(out.startswith("BLOCKED: no proposal for kind invoice") and out.endswith("(rule no-matching-proposal)"),
          "Save with no proposal is BLOCKED with the rule")
    check(browser.read_page(ctx).startswith("PAGE /invoices/new (HTTP 200)") and ledger(base) == [],
          "nothing was clicked: still on the clean form, ledger unchanged")
    p = gateway.propose(ctx.run_id, "invoice", INVOICE, "inbox:3")
    check(p["status"] == "ALLOWED", f"proposal for INV-1042 is ALLOWED ({p['proposal_id']})")
    out = browser.fill_form(ctx, fields={**BY_LABEL, "Amount": "1420.00"})
    check('  amount = "1420.00"' in lines(out) and "FORM record=invoice action=/invoices/new" in out,
          "fill_form returns the record form's current values")
    out = browser.click(ctx, target="Save")
    check(out.startswith("BLOCKED: form does not match proposal") and "amount" in out
          and "(rule no-matching-proposal)" in out and ledger(base) == [],
          "mismatching amount is BLOCKED, naming the field; ledger unchanged")

    print("dispatch end to end")
    out = gateway.dispatch(ctx, "fill_form", {"fields": {"amount": "$1,240.00"}})
    check(lines(out)[0] == "OK: filled 1 field", "dispatch fill_form")
    out = gateway.dispatch(ctx, "click", {"target": "Save"})
    first = lines(out)[0]
    check(first.startswith('OK: clicked "Save" → /invoices/') and first.endswith("?posted=1 (HTTP 200)"),
          f"matching submit lands on the posted invoice ({first})")
    check(any(x.startswith("VERIFIED: GET /api/ledger?invoice_no=INV-1042 -> 1 row") for x in lines(out)),
          "VERIFIED line with ledger evidence")
    rows = ledger(base)
    check(len(rows) == 1 and rows[0]["amount"] == 1240.0 and rows[0]["due_date"] == "2026-11-02",
          "exactly one ledger row with the proposed values")
    check(gateway.proposals(ctx.run_id)[-1]["status"] == "DONE", "proposal is DONE")
    acts = [(a["tool"], a["tier"], a["result_status"]) for a in gateway.actions(ctx.run_id)
            if not a["tool"].startswith("gateway.")]
    check(acts == [("fill_form", "write", "OK"), ("click", "write", "OK")], f"dispatch recorded the calls {acts}")
    check(gateway.dispatch(ctx, "read_page", {}).startswith("PAGE /invoices/"), "dispatch read_page")
    check(gateway.dispatch(ctx, "goto", {"path": "/chaos"}).startswith("BLOCKED: path /chaos is off-limits"),
          "goto /chaos is out of scope")

    open_filled_form(ctx)
    out = browser.click(ctx, target="Save")
    check(out.startswith("BLOCKED: ") and "(rule duplicate-submit)" in out and len(ledger(base)) == 1,
          "submitting the same invoice again is BLOCKED as a duplicate")

    out = browser.fill_form(ctx, fields={"Colour": "blue", "Vendor": "Acme Corp"})
    check(lines(out)[0].startswith('ERROR: no field "Colour"; available: Vendor [vendor], Invoice number [invoice_no]')
          and "(filled 1 other field)" in lines(out)[0], "unknown field is an ERROR listing the available fields")

    print("video")
    video_dir = Path(os.environ["RECEIPTS_RUNS"]) / "video"
    vb = browser.Browser(base, video_dir=str(video_dir))
    vb.start()
    check(vb.page.viewport_size == {"width": 1280, "height": 800}, "video mode uses a 1280x800 viewport")
    vb.goto("/login")
    vb.close()
    videos = list(video_dir.glob("*.webm"))
    check(len(videos) == 1 and videos[0].stat().st_size > 0, "close() finalizes the video file")

    print("crash after submit")
    proc = subprocess.run([PY, "-c", CRASH_SCRIPT], cwd=ROOT, capture_output=True, text=True, timeout=120,
                          env={**os.environ, "RECEIPTS_CRASH_AFTER_SUBMIT": "1"})
    check(proc.returncode == 3 and "VERIFIED" not in proc.stdout,
          f"RECEIPTS_CRASH_AFTER_SUBMIT=1 exits 3 right after the submit (got {proc.returncode} {proc.stderr[-300:]})")
    check(len(ledger(base, "INV-7731")) == 1, "the crashed submit's write landed")
    crashed = gateway.proposals("chk-crash")[-1]
    check(crashed["status"] == "SUBMITTED", "the crashed run's proposal is SUBMITTED, not confirmed")


def hazards_mode(base: str, b: Any) -> None:
    import browser
    import gateway

    print("save_500")
    reset(base, {"save_500": 1})
    ctx = make_ctx("chk-500", b)
    login(ctx)
    gateway.propose(ctx.run_id, "invoice", INVOICE, "inbox:3")
    open_filled_form(ctx)
    out = browser.click(ctx, target="Save")
    check(lines(out)[0] == 'OK: clicked "Save" → /invoices/new (HTTP 500)' and "DETECTED: server_error (HTTP 500)" in out,
          "HTTP 500 is DETECTED as server_error")
    check("VERIFIED" not in out and "validation_error" not in out and ledger(base) == [], "nothing verified or written")
    open_filled_form(ctx)
    out = browser.click(ctx, target="Save")
    check(any(x.startswith("VERIFIED: ") for x in lines(out)) and len(ledger(base)) == 1, "retry succeeds and is VERIFIED")

    print("session_expiry")
    reset(base, {"session_expiry": True})
    ctx = make_ctx("chk-expiry", b)
    login(ctx)
    gateway.propose(ctx.run_id, "invoice", INVOICE, "inbox:3")
    open_filled_form(ctx)
    out = browser.click(ctx, target="Save")
    check(lines(out)[0].startswith('OK: clicked "Save" → /login?next=/invoices/new&expired=1 (HTTP 200)')
          and "DETECTED: session_expired (redirected to login)" in out and "validation_error" not in out,
          "session expiry is DETECTED")
    check(ledger(base) == [], "nothing written")
    browser.fill_form(ctx, fields={"Username": "clerk", "Password": "receipts123"})
    out = browser.click(ctx, target="Sign in")
    check(lines(out)[0] == 'OK: clicked "Sign in" → /invoices/new (HTTP 200)', "re-login returns to the form")
    browser.fill_form(ctx, fields=BY_LABEL)
    out = browser.click(ctx, target="Save")
    check(any(x.startswith("VERIFIED: ") for x in lines(out)) and len(ledger(base)) == 1,
          "re-fill and submit succeeds and is VERIFIED")

    print("date_rule")
    reset(base, {"date_rule": True})
    ctx = make_ctx("chk-date", b)
    login(ctx)
    gateway.propose(ctx.run_id, "invoice", INVOICE, "inbox:3")
    out = open_filled_form(ctx)
    out = browser.click(ctx, target="Save")
    check("(HTTP 422)" in lines(out)[0]
          and "DETECTED: validation_error: Due date must be in DD/MM/YYYY format" in out
          and "ALERT: Due date must be in DD/MM/YYYY format" in out and "(placeholder DD/MM/YYYY)" in out,
          "the date rule is DETECTED as a validation error with the format text")
    check("VERIFIED" not in out and ledger(base) == [], "nothing written")
    browser.fill_form(ctx, fields={"Due date": "02/11/2026"})
    out = browser.click(ctx, target="Save")
    rows = ledger(base)
    check(any(x.startswith("VERIFIED: ") for x in lines(out)) and len(rows) == 1 and rows[0]["due_date"] == "2026-11-02",
          "DD/MM/YYYY passes the guard (same date) and is stored as ISO")

    print("renamed_button")
    reset(base, {"renamed_button": True})
    ctx = make_ctx("chk-renamed", b)
    login(ctx)
    gateway.propose(ctx.run_id, "invoice", INVOICE, "inbox:3")
    out = open_filled_form(ctx)
    snap = browser.read_page(ctx)
    check("Save" not in snap and "  buttons: (none visible)" in snap and "OTHER BUTTONS: More actions" in snap,
          "no Save button; More actions is listed")
    check("Submit for posting" not in snap, "the hidden menu item is not listed while the menu is closed")
    out = browser.click(ctx, target="Save")
    check(lines(out)[:2] == ['ERROR: no clickable element named "Save"', "DETECTED: element_not_found"]
          and "AVAILABLE BUTTONS: More actions" in out and "AVAILABLE LINKS: " in out and "Inbox" in out,
          "clicking the missing Save is element_not_found with the available names")
    out = browser.click(ctx, target="More actions")
    check(lines(out)[0].startswith('OK: clicked "More actions" (no navigation') and "  buttons: Submit for posting" in out,
          "opening More actions reveals Submit for posting")
    out = browser.click(ctx, target="Submit for posting")
    check(any(x.startswith("VERIFIED: ") for x in lines(out)) and len(ledger(base)) == 1,
          "Submit for posting submits through the guard and is VERIFIED")

    print("missing element")
    out = browser.click(ctx, target="Frobnicate")
    check(lines(out)[:2] == ['ERROR: no clickable element named "Frobnicate"', "DETECTED: element_not_found"]
          and "AVAILABLE LINKS: " in out, "unknown target is element_not_found")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "default"
    if mode not in ("default", "hazards"):
        fail(f"unknown mode {mode!r}; use no argument or 'hazards'")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    env = isolated_env()
    with company_app(env=env) as base:
        os.environ.update(env)
        os.environ["RECEIPTS_APP_URL"] = base
        import browser  # after the env is set: gateway/policy read it

        b = browser.Browser(base)
        b.start()
        try:
            (hazards_mode if mode == "hazards" else default_mode)(base, b)
        finally:
            b.close()
    print("BROWSER HAZARDS VERIFIED" if mode == "hazards" else "BROWSER VERIFIED")


if __name__ == "__main__":
    main()
