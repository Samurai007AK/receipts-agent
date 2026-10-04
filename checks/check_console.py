"""Self-check for console.py: drive the operator page with headless Chromium against the stub company app
and checks/fake_agent.py, and exercise every console API route. Prints CONSOLE VERIFIED last."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime

import httpx
from playwright.sync_api import expect, sync_playwright

from _servers import PY, ROOT, check, fail, isolated_env, serve, stub_app

sys.path.insert(0, str(ROOT))

PRESET_A = ("Find the latest invoice from Acme Corp, extract the amount and due date, enter it into our AP system, "
            "and tell me once it is done.")
PRESET_B = ("Process every unprocessed vendor invoice in the AP inbox into the AP system, "
            "then send me a short summary.")
SCREENSHOT = ROOT / "runs" / "screens" / "console.png"
STAGES = SCREENSHOT.parent  # console-contract.png and console-parked.png: intermediate states for visual review


def events(con: str, run_id: str, after: int = 0) -> dict:
    r = httpx.get(f"{con}/api/runs/{run_id}/events", params={"after": after}, timeout=10)
    r.raise_for_status()
    return r.json()


def wait_finished(con: str, run_id: str, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = events(con, run_id)
        if d["finished"]:
            return d
        time.sleep(0.3)
    fail(f"run {run_id} did not finish within {timeout}s")
    return {}


def api_before(con: str, stub: str) -> None:
    presets = httpx.get(f"{con}/api/presets").json()
    by_id = {p["id"]: p for p in presets}
    check({"A", "B", "C"} <= set(by_id) and all(p.get("title") for p in presets), "presets A, B, C with titles")
    check(by_id["A"]["task"] == PRESET_A and by_id["B"]["task"] == PRESET_B, "preset A and B tasks match the contract")
    page = httpx.get(con)
    check(page.status_code == 200 and "Receipts" in page.text and json.dumps(stub) in page.text,
          "GET / serves the page with the company app URL")
    check(httpx.get(f"{con}/api/runs/nope/events").status_code == 404, "events for unknown run -> 404")
    check(httpx.get(f"{con}/api/runs/..%2F..%2Fdocs/events").status_code == 404, "path-traversal run id -> 404")
    check(httpx.post(f"{con}/api/runs/nope/contract", json={"action": "approve"}).status_code == 404,
          "contract decision for unknown run -> 404")
    check(httpx.post(f"{con}/api/runs", json={"task": "   "}).status_code == 422, "empty task -> 422")
    hz = httpx.get(f"{con}/api/hazards")
    check(hz.status_code == 200 and hz.json() == [], "hazards proxy -> [] when the app has no chaos log")
    check(httpx.get(f"{con}/api/parked").json() == [], "nothing parked before any run")


def drive_ui(con: str, stub: str) -> str:
    hazard: dict = {}

    def fake_hazards(route) -> None:  # the stub app has no chaos log; inject one hazard to test merging
        if not hazard:
            hazard["row"] = {"ts": datetime.now().isoformat(timespec="seconds"), "hazard": "session_expiry",
                             "detail": "session dropped on POST /invoices/new"}
        route.fulfill(json=[hazard["row"]])

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(con)
        expect(page.locator("h1")).to_have_text("Receipts — every task comes with proof")
        chaos = page.locator("#chaos-link")
        check(chaos.get_attribute("href") == f"{stub}/chaos" and chaos.get_attribute("target") == "_blank",
              "chaos link opens company /chaos in a new tab")
        expect(page.locator(".preset")).to_have_count(3)

        page.locator('.preset[data-id="A"]').click()
        check(page.locator("#task").input_value() == PRESET_A, "preset A fills the task box")
        page.locator("#run-btn").click()
        page.wait_for_function("location.hash.startsWith('#run=')")
        run_id = page.evaluate("location.hash.slice(5)")
        check(re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", run_id), f"run id format YYYYMMDD-HHMMSS-hex4 ({run_id})")
        page.route("**/api/hazards", fake_hazards)

        approve = page.locator("#contract-approve")
        approve.wait_for(state="visible", timeout=20000)
        expect(page.locator("#clauses li")).to_have_count(2)
        expect(page.locator("#contract-card")).to_contain_text("appears exactly once in the AP system")
        expect(page.locator("#run-status")).to_have_text("Needs your approval")
        check(page.locator("#run-btn").is_disabled(), "Run is disabled while a run is active")
        STAGES.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(STAGES / "console-contract.png"), full_page=True)

        check(httpx.get(f"{con}/api/runs/{run_id}/receipt").status_code == 404, "receipt -> 404 before the run ends")
        first = events(con, run_id)
        n = first["next"]
        types = [e["type"] for e in first["events"]]
        check(n == len(first["events"]) and types[:2] == ["run_started", "contract_proposed"] and not first["finished"],
              "events?after=0 returns everything so far, not finished")
        tail = events(con, run_id, 1)
        check(tail["events"][:n - 1] == first["events"][1:], "events?after=1 skips exactly one event")
        rest = events(con, run_id, n)
        check(rest["next"] >= n and rest["events"] == events(con, run_id)["events"][n:rest["next"]],
              "events?after=next returns only newer events")

        approve.click()
        park = page.locator(".park-card")
        park.wait_for(timeout=30000)
        expect(park).to_contain_text("INV-UC-5521")
        expect(park).to_contain_text("over-threshold")
        expect(page.locator("#contract-badge")).to_have_text("Approved")
        parked = httpx.get(f"{con}/api/parked", params={"run_id": run_id}).json()
        check(len(parked) == 1 and parked[0]["ref"] == "INV-UC-5521" and parked[0]["kind"] == "invoice",
              "GET /api/parked lists the parked invoice")
        expect(page.locator(".ev-proposal.k-blocked")).to_contain_text("ZRG-1")
        page.screenshot(path=str(STAGES / "console-parked.png"), full_page=True)
        bad = httpx.post(f"{con}/api/parked/{parked[0]['proposal_id']}", json={"decision": "maybe"})
        check(bad.status_code == 422, "invalid park decision -> 422")

        page.reload()  # mid-run reload must rebuild the page from event 0
        park.wait_for(timeout=15000)
        expect(page.locator(".ev-run_started")).to_have_count(1)
        expect(page.locator("#contract-approve")).to_have_count(0)
        expect(page.locator("#contract-badge")).to_have_text("Approved")
        check(page.locator("#log .ev").count() >= 10, "after reload the log is re-read from event 0")

        park.get_by_role("button", name="Approve once").click()
        expect(page.locator("#receipt-status")).to_have_text("VERIFIED", timeout=60000)
        expect(page.locator(".park-card")).to_have_count(0)
        expect(page.locator("#clauses li.pass")).to_have_count(2)
        receipt = page.locator("#receipt-card")
        expect(receipt.locator(".rsec.done")).to_contain_text("INV-UC-5521")
        expect(receipt.locator(".rsec.notdone")).to_contain_text("Deliberately NOT done")
        expect(receipt.locator(".rsec.notdone")).to_contain_text("ZRG-1")
        expect(receipt.locator(".rsec.notdone .rule")).not_to_have_text("")
        expect(receipt.locator(".rsec.waiting")).to_contain_text("Nothing is waiting on you")
        expect(receipt.locator(".rsec.evidence")).to_contain_text("GET /api/ledger?invoice_no=INV-UC-5521")
        expect(receipt.locator(".rsec.evidence .excerpt").first).to_contain_text("INV-UC-5521")
        for kind in ("hazard", "detected", "recovery", "verified"):
            check(page.locator(f"#log .ev.k-{kind}.moment").count() >= 1, f"log highlights a {kind.upper()} moment")
        expect(page.locator("#log .ev-hazard")).to_have_count(1)
        expect(page.locator(".ev-decision")).to_contain_text("approved by human")
        expect(page.locator("#run-status")).to_have_text("VERIFIED")
        check(page.locator("#run-btn").is_enabled(), "Run is enabled again after the run finishes")
        check(not errors, f"no JavaScript errors on the page {errors}")

        page.locator("#log").evaluate("el => el.scrollTop = 0")
        SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SCREENSHOT), full_page=True)
        check(SCREENSHOT.stat().st_size > 20000, f"screenshot saved to {SCREENSHOT}")
        browser.close()
    return run_id


def api_after(con: str, stub: str, run_id: str) -> None:
    import gateway  # imported late: RECEIPTS_DB in os.environ points at the isolated DB the console uses

    done = wait_finished(con, run_id)
    check(done["events"][-1]["type"] == "run_finished", "events end with run_finished and finished=true")
    r = httpx.get(f"{con}/api/runs/{run_id}/receipt")
    check(r.status_code == 200 and r.json()["status"] == "VERIFIED" and r.json()["run_id"] == run_id,
          "receipt -> 200 with status VERIFIED after the run")
    check(httpx.get(f"{con}/api/parked", params={"run_id": run_id}).json() == [], "parked list is empty after decision")
    check(httpx.post(f"{con}/api/runs/{run_id}/contract", json={"action": "maybe"}).status_code == 422,
          "invalid contract action -> 422")
    check(len(httpx.get(f"{stub}/api/ledger", params={"invoice_no": "INV-UC-5521"}).json()) == 1,
          "approved invoice reached the company app")
    check(gateway.proposals(run_id), "gateway holds this run's proposals before reset")

    r = httpx.post(f"{con}/api/reset", timeout=30)
    check(r.status_code == 200 and r.json()["app"] == "ok" and r.json()["gateway"] == "ok", "POST /api/reset -> app ok, gateway ok")
    check(httpx.get(f"{stub}/api/ledger", params={"invoice_no": "INV-UC-5521"}).json() == [], "reset restored the app seed data")
    check(gateway.proposals() == [], "reset cleared the gateway database")


def crash_run(stub: str, env: dict[str, str]) -> None:
    """An agent that dies without run_finished gets a synthetic error event; a shell-string command also works."""
    cmd = f'"{PY}" -c "import sys; print(chr(66)+chr(79)+chr(79)+chr(77)); sys.exit(3)"'
    with serve("console", env={**env, "RECEIPTS_APP_URL": stub, "RECEIPTS_AGENT_CMD": cmd}) as con:
        run_id = httpx.post(f"{con}/api/runs", json={"task": "crash please"}).json()["run_id"]
        d = wait_finished(con, run_id)
        last = d["events"][-1] if d["events"] else {}
        check(last.get("type") == "error" and "code 3" in last.get("message", "") and "BOOM" in last["message"],
              "dead agent -> synthetic error event with exit code and log tail")
        again = events(con, run_id)
        check(len(again["events"]) == len(d["events"]), "synthetic error is appended only once")
        check(httpx.get(f"{con}/api/runs/{run_id}/receipt").status_code == 404, "no receipt for a crashed run")
        r = httpx.post(f"{con}/api/reset", timeout=30)
        check(r.status_code == 200, "reset works with no live runs")
    with serve("console", env={**env, "RECEIPTS_APP_URL": "http://127.0.0.1:9"}) as con:
        r = httpx.post(f"{con}/api/reset", timeout=30)
        check(r.status_code == 200 and r.json()["app"].startswith("error") and r.json()["gateway"] == "ok",
              "reset reports an unreachable company app instead of failing")
        check(httpx.get(f"{con}/api/hazards").status_code == 502, "hazards -> 502 when the company app is unreachable")


def main() -> None:
    env = isolated_env()
    os.environ.update(env)
    with stub_app(env=env) as stub:
        agent = json.dumps([PY, str(ROOT / "checks" / "fake_agent.py")])
        with serve("console", env={**env, "RECEIPTS_APP_URL": stub, "RECEIPTS_AGENT_CMD": agent}) as con:
            api_before(con, stub)
            run_id = drive_ui(con, stub)
            api_after(con, stub, run_id)
        crash_run(stub, env)
    print("CONSOLE VERIFIED", flush=True)


if __name__ == "__main__":
    main()
