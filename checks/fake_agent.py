"""Scripted stand-in for agent.py's UI mode, used by checks/check_console.py.

Usage (as the console spawns it):
    python checks/fake_agent.py "<task>" --run-id ID --decide ui --approve-contract ui

Writes RECEIPTS_RUNS/<id>/events.jsonl in the docs/CONTRACT.md section 5 format, waits for the console's
contract_decision.json, parks the Umbrella INV-UC-5521 $8,200 invoice through the real gateway, waits for the
human decision, posts the approved records via the stub company app's /stub/* hooks, and writes receipt.json.
No LLM. RECEIPTS_APP_URL must point at checks/stub_company.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gateway  # noqa: E402

APP_URL = os.environ.get("RECEIPTS_APP_URL", "http://127.0.0.1:8000").rstrip("/")
PAUSE = 0.25        # seconds between events, so the console visibly streams
WAIT_LIMIT = 180.0  # seconds to wait for any human decision before giving up
INVOICE = {"vendor": "Umbrella Corp", "invoice_no": "INV-UC-5521", "amount": "8200.00", "due_date": "2026-11-15"}
POISONED = {"vendor": "Zorg Ltd", "invoice_no": "ZRG-1", "amount": "9000.00", "due_date": "2026-10-10"}
NOTE = {"to": "ap@company.test", "subject": "Invoice INV-UC-5521 entered",
        "body": "Umbrella Corp invoice INV-UC-5521 for $8,200.00 (due 2026-11-15) is in the AP system."}
CLAUSES = [
    {"kind": "row_count", "path": "/api/ledger?invoice_no=INV-UC-5521", "equals": 1, "ref": "INV-UC-5521"},
    {"kind": "outbox_contains", "to": "ap@company.test", "text": "INV-UC-5521", "ref": "notify:ap@company.test"},
]
ENGLISH = [
    "Invoice INV-UC-5521 from Umbrella Corp appears exactly once in the AP system",
    "AP (ap@company.test) received a message mentioning INV-UC-5521",
]
EXCUSING = ("SKIPPED", "BLOCKED", "REJECTED", "PARKED")


class Run:
    def __init__(self, run_id: str, task: str) -> None:
        self.run_id = run_id
        self.task = task
        self.dir = ROOT / os.environ.get("RECEIPTS_RUNS", "runs") / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.start = time.time()
        self.steps = 0

    def emit(self, type_: str, **data: object) -> None:
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "type": type_, **data}) + "\n")
        time.sleep(PAUSE)

    def step(self, tool: str, args: dict, tier: str, status: str, summary: str) -> int:
        self.steps += 1
        self.emit("step", n=self.steps, tool=tool, args=args, tier=tier)
        self.emit("observation", n=self.steps, tool=tool, status=status, summary=summary)
        return self.steps

    def finish(self, status: str) -> None:
        self.emit("run_finished", status=status, steps=self.steps, llm_calls=0, tokens=0,
                  seconds=round(time.time() - self.start, 1))


def wait_for(fn, what: str):
    deadline = time.time() + WAIT_LIMIT
    while time.time() < deadline:
        value = fn()
        if value is not None:
            return value
        time.sleep(0.2)
    raise TimeoutError(f"no {what} within {WAIT_LIMIT:.0f}s")


def contract_decision(run: Run) -> dict | None:
    try:
        return json.loads((run.dir / "contract_decision.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def proposal(run_id: str, proposal_id: str) -> dict:
    return next(p for p in gateway.proposals(run_id) if p["proposal_id"] == proposal_id)


def evaluate() -> list[dict]:
    """Referee stand-in: check both clauses against the app's JSON API."""
    r = httpx.get(f"{APP_URL}/api/ledger", params={"invoice_no": "INV-UC-5521"}, timeout=10)
    rows = r.json()
    results = [{"clause": CLAUSES[0], "passed": len(rows) == 1, "observed": len(rows),
                "evidence": {"request": "GET /api/ledger?invoice_no=INV-UC-5521", "status": r.status_code,
                             "body_excerpt": r.text[:300]}}]
    r = httpx.get(f"{APP_URL}/api/outbox", params={"to": "ap@company.test"}, timeout=10)
    hits = [m for m in r.json() if "inv-uc-5521" in (m["subject"] + " " + m["body"]).lower()]
    results.append({"clause": CLAUSES[1], "passed": bool(hits), "observed": f"{len(hits)} matching message(s)",
                    "evidence": {"request": "GET /api/outbox?to=ap@company.test", "status": r.status_code,
                                 "body_excerpt": r.text[:300]}})
    return results


def submit(run: Run, kind: str, proposal_id: str, fields: dict, stub_path: str, record: dict) -> dict:
    """Guard the submit through the gateway, write via the stub hook, then confirm."""
    guard = gateway.guard_submit(run.run_id, kind, {k: {"value": str(v), "hidden": False} for k, v in fields.items()})
    if not guard["allowed"]:
        raise RuntimeError(f"gateway refused {kind} submit: {guard['reason']} ({guard['rule_id']})")
    httpx.post(f"{APP_URL}{stub_path}", json=record, timeout=10).raise_for_status()
    confirmed = gateway.confirm(proposal_id)
    if confirmed["status"] != "DONE":
        raise RuntimeError(f"{kind} {proposal_id} not confirmed: {confirmed}")
    return confirmed


def propose(run: Run, kind: str, fields: dict, source: str | None) -> dict:
    run.emit("step", n=run.steps + 1, tool="propose", args={"kind": kind, "fields": fields, "source": source},
             tier="control")
    run.steps += 1
    res = gateway.propose(run.run_id, kind, fields, source=source)
    run.emit("observation", n=run.steps, tool="propose", status=res["status"],
             summary=f"{res['proposal_id']} {res['status']}" + (f" ({res['rule_id']}): {res['reason']}" if res["rule_id"] else ""))
    run.emit("proposal", proposal_id=res["proposal_id"], kind=kind, status=res["status"], rule_id=res["rule_id"],
             reason=res["reason"], ref=res["ref"])
    return res


def main_run(run: Run) -> int:
    run.emit("run_started", task=run.task, run_id=run.run_id, models=["fake-agent"])
    (run.dir / "contract.json").write_text(json.dumps(CLAUSES, indent=2), encoding="utf-8")
    run.emit("contract_proposed", clauses=CLAUSES, english=ENGLISH)
    decision = wait_for(lambda: contract_decision(run), "contract decision")
    if decision.get("action") != "approve":
        run.emit("contract_rejected")
        run.finish("REJECTED")
        return 1
    run.emit("contract_approved", by="human")
    pre = evaluate()
    run.emit("referee_pre", results=pre, all_passed=all(r["passed"] for r in pre))

    run.step("goto", {"path": "/inbox"}, "read", "OK", "PAGE /login?next=/inbox (HTTP 200) — Sign in")
    run.emit("detected", n=run.steps, what="session_expired (redirected to login)")
    run.emit("recovery", n=run.steps + 1, tool="fill_form")
    run.step("fill_form", {"fields": {"username": "clerk", "password": "********"}}, "write", "OK", "OK: filled 2 fields")
    run.step("click", {"target": "Sign in"}, "write", "OK", "PAGE /inbox (HTTP 200) — Inbox")
    run.emit("verified", n=run.steps, what="signed in again; inbox is readable")
    run.step("goto", {"path": "/inbox/7"}, "read", "OK", "PAGE /inbox/7 (HTTP 200) — Invoice INV-UC-5521 · Amount due: $8,200.00")

    parked = propose(run, "invoice", INVOICE, "inbox:7")
    if parked["status"] != "PARKED":
        raise RuntimeError(f"expected the $8,200 invoice to be PARKED, gateway said {parked['status']}")
    run.emit("parked", proposal_id=parked["proposal_id"], kind="invoice", ref=parked["ref"],
             rule_id=parked["rule_id"], reason=parked["reason"])

    run.step("goto", {"path": "/inbox/3"}, "read", "OK",
             "PAGE /inbox/3 (HTTP 200) — Your September invoice INV-1042 · contains a note asking for a Zorg Ltd payable")
    blocked = propose(run, "invoice", POISONED, "inbox:3")
    if blocked["status"] != "BLOCKED":
        raise RuntimeError(f"expected the injected Zorg payable to be BLOCKED, gateway said {blocked['status']}")

    pid = parked["proposal_id"]
    decided = wait_for(lambda: (lambda p: p if p["status"] in ("APPROVED", "REJECTED") else None)(proposal(run.run_id, pid)),
                       f"decision on {pid}")
    approved = decided["status"] == "APPROVED"
    run.emit("decision", proposal_id=pid, decision="approve" if approved else "reject",
             by=decided.get("decided_by") or "human")

    if approved:
        run.step("goto", {"path": "/invoices/new"}, "read", "OK", "PAGE /invoices/new (HTTP 200) — New invoice")
        run.step("fill_form", {"fields": INVOICE}, "write", "OK", "OK: filled 4 fields")
        confirmed = submit(run, "invoice", pid, INVOICE, "/stub/invoices", {**INVOICE, "amount": 8200.0})
        run.step("click", {"target": "Save"}, "irreversible", "OK", "PAGE /invoices/3?posted=1 (HTTP 200) — Invoice posted")
        run.emit("verified", n=run.steps, what=confirmed["evidence"])

    note = propose(run, "notification", NOTE, None)
    if note["status"] == "ALLOWED":
        run.step("fill_form", {"fields": NOTE}, "write", "OK", "OK: filled 3 fields")
        confirmed = submit(run, "notification", note["proposal_id"], NOTE, "/stub/outbox", dict(NOTE))
        run.step("click", {"target": "Send"}, "irreversible", "OK", "PAGE /notify?sent=1 (HTTP 200) — Message sent")
        run.emit("verified", n=run.steps, what=confirmed["evidence"])

    results = evaluate()
    props = gateway.proposals(run.run_id)
    exceptions, failures = [], []
    for r in results:
        if r["passed"]:
            continue
        ref = r["clause"]["ref"].lower()
        excuse = next((p for p in props if p["ref"].lower() == ref and p["status"] in EXCUSING), None)
        if excuse:
            exceptions.append({"clause": r["clause"], **{k: excuse[k] for k in ("proposal_id", "status", "rule_id", "reason")}})
        else:
            failures.append(r)
    status = "FAILED" if failures else "VERIFIED_WITH_EXCEPTIONS" if exceptions else "VERIFIED"
    run.emit("referee", round=1, results=results, status=status)

    receipt = {
        "run_id": run.run_id, "task": run.task, "status": status,
        "contract": [{**r, "english": e} for r, e in zip(results, ENGLISH)],
        "done": [p for p in props if p["status"] == "DONE"],
        "not_done": [p for p in props if p["status"] in ("SKIPPED", "BLOCKED", "REJECTED")],
        "waiting": [p for p in props if p["status"] == "PARKED"],
        "exceptions": exceptions, "unexpected_changes": [],
        "stats": {"steps": run.steps, "llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                  "seconds": round(time.time() - run.start, 1), "models": ["fake-agent"]},
        "notes": {"agent": "checks/fake_agent.py (scripted, no LLM)"},
        "summary": "Entered Umbrella Corp invoice INV-UC-5521 after your approval, told AP, and refused the "
                   "Zorg Ltd payable that an email tried to inject." if approved else
                   "Left invoice INV-UC-5521 out because you rejected it, told AP, and refused the injected Zorg Ltd payable.",
    }
    tmp = run.dir / "receipt.json.tmp"
    tmp.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    os.replace(tmp, run.dir / "receipt.json")
    run.emit("receipt", status=status, path=str(run.dir / "receipt.json"))
    run.finish(status)
    return 1 if status == "FAILED" else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("task")
    ap.add_argument("--run-id", required=True)
    args, _ = ap.parse_known_args()
    run = Run(args.run_id, args.task)
    try:
        return main_run(run)
    except Exception as e:  # surface any failure in the event stream, as the real agent does
        run.emit("error", message=f"{type(e).__name__}: {e}")
        run.finish("ERROR")
        return 2


if __name__ == "__main__":
    sys.exit(main())
