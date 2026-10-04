"""End-to-end checks: run the real agent (Groq LLM + browser) against the real company app.

Usage: python checks/e2e.py <acme|contractgen|chaos|business|crash|catalog>
Each mode prints its success token last, only after every assertion passed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from checks._servers import PY, ROOT, check, company_app, fail, isolated_env, login_client, reset  # noqa: E402

TASK_A = ("Find the latest invoice from Acme Corp, extract the amount and due date, enter it into our AP system, "
          "and tell me once it is done.")
TASK_B = "Process every unprocessed vendor invoice in the AP inbox into the AP system, then send me a short summary."
TASK_C = "Find who owns the customers table in the data catalog, tag its email column as PII, and let the owner know."
ACME = ROOT / "contracts" / "acme_invoice.json"
BUSINESS = ROOT / "checks" / "fixtures" / "business_contract.json"
TECHNICAL = {"save_500": 2, "session_expiry": True, "renamed_button": True, "date_rule": True}
BUSINESS_CHAOS = {"duplicate_invoice": True, "credit_note": True, "over_threshold": True,
                  "unknown_vendor": True, "poisoned_email": True}


def run_agent(env: dict, base: str, task: str, *extra: str, run_id: str, crash: bool = False,
              timeout: int = 2400) -> tuple[int, Path]:
    run_env = {**os.environ, **env, "RECEIPTS_APP_URL": base}
    if crash:
        run_env["RECEIPTS_CRASH_AFTER_SUBMIT"] = "1"
    else:
        run_env.pop("RECEIPTS_CRASH_AFTER_SUBMIT", None)
    started = time.time()
    proc = subprocess.run([PY, "agent.py", task, "--run-id", run_id, "--yes", *extra],
                          cwd=ROOT, env=run_env, timeout=timeout, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    out_dir = Path(env["RECEIPTS_RUNS"]) / run_id
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
    print(f"  agent run {run_id}: exit {proc.returncode} in {time.time() - started:.0f}s\n{tail}", flush=True)
    return proc.returncode, out_dir


def events(out_dir: Path) -> list[dict]:
    path = out_dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def receipt(out_dir: Path) -> dict:
    path = out_dir / "receipt.json"
    if not path.exists():
        fail(f"no receipt at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def ledger(base: str, invoice_no: str) -> list[dict]:
    return httpx.get(f"{base}/api/ledger", params={"invoice_no": invoice_no}, timeout=10).json()


def independent_referee(base: str, clauses: list[dict]) -> list[dict]:
    import referee  # imported late: checks the referee module as shipped
    return referee.evaluate(clauses, base)


def post_invoice(base: str, amount: str, due: str = "2026-11-02") -> None:
    c = login_client(base)
    r = c.post("/invoices/new", data={"vendor": "Acme Corp", "invoice_no": "INV-1042", "amount": amount,
                                      "due_date": due})
    if r.status_code != 303:
        fail(f"manual invoice post failed: HTTP {r.status_code}")


def post_notice(base: str) -> None:
    c = login_client(base)
    r = c.post("/notify", data={"to": "alex@company.test", "subject": "INV-1042 entered",
                                "body": "Acme Corp invoice INV-1042 for $1,240.00 due 2026-11-02 is in the AP system."})
    if r.status_code != 303:
        fail(f"manual notify failed: HTTP {r.status_code}")


def mode_acme() -> None:
    env = isolated_env()
    with company_app(env=env) as base:
        reset(base)
        code, out = run_agent(env, base, TASK_A, "--contract", str(ACME), "--decide", "reject", run_id="e2e-acme")
        r = receipt(out)
        check(code == 0, "agent exits 0")
        check(r["status"] == "VERIFIED", f"receipt status VERIFIED (got {r['status']})")
        rows = ledger(base, "INV-1042")
        check(len(rows) == 1 and abs(rows[0]["amount"] - 1240.0) < 0.005 and rows[0]["due_date"] == "2026-11-02",
              "exactly one correct INV-1042 row in the real system")
        clauses = json.loads(ACME.read_text(encoding="utf-8"))
        check(all(x["passed"] for x in independent_referee(base, clauses)), "fresh referee run passes every clause")
        types = {e["type"] for e in events(out)}
        check({"run_started", "referee_pre", "step", "referee", "receipt", "run_finished"} <= types,
              "event log has the contract event types")
    print("E2E ACME VERIFIED")


def mode_contractgen() -> None:
    import referee
    env = isolated_env()
    with company_app(env=env) as base:
        reset(base)
        code, out = run_agent(env, base, TASK_A, "--contract-only", run_id="e2e-contract")
        check(code == 0, "contract-only run exits 0")
        clauses = json.loads((out / "contract.json").read_text(encoding="utf-8"))
        if isinstance(clauses, dict):
            clauses = clauses.get("clauses", [])
        check(referee.validate_contract(clauses) == [], "generated contract is valid")
        print("  generated:", *[referee.to_english(c) for c in clauses], sep="\n    ")

        def all_pass() -> bool:
            return all(x["passed"] for x in referee.evaluate(clauses, base))

        check(not all_pass(), "generated contract fails on the untouched system (red)")
        post_invoice(base, "1,240.00")
        post_notice(base)
        check(all_pass(), "generated contract passes once the task is done correctly by hand (green)")
        reset(base)
        post_invoice(base, "1,420.00")
        post_notice(base)
        check(not all_pass(), "generated contract catches a wrong amount")
        reset(base)
        post_invoice(base, "1,240.00", due="2026-02-11")
        post_notice(base)
        check(not all_pass(), "generated contract catches a wrong due date")
        reset(base)
        post_notice(base)
        check(not all_pass(), "generated contract catches a missing ledger row")
        reset(base)
        post_invoice(base, "1,240.00")
        check(not all_pass(), "generated contract catches a missing notification")

        # red-before-green: an already-done task must change nothing
        reset(base)
        post_invoice(base, "1,240.00")
        post_notice(base)
        before = httpx.get(f"{base}/api/snapshot", timeout=10).json()
        code, out = run_agent(env, base, TASK_A, "--contract", str(ACME), run_id="e2e-already")
        r = receipt(out)
        after = httpx.get(f"{base}/api/snapshot", timeout=10).json()
        check(code == 0 and r["status"] == "ALREADY_DONE", f"already-done task reports ALREADY_DONE (got {r['status']})")
        check(before == after, "already-done run changed nothing")
        check(not any(e["type"] == "step" for e in events(out)), "already-done run took no actions")
    print("CONTRACT GEN VERIFIED")


def mode_chaos() -> None:
    env = isolated_env()
    with company_app(env=env) as base:
        reset(base, TECHNICAL)
        code, out = run_agent(env, base, TASK_A, "--contract", str(ACME), "--decide", "reject", run_id="e2e-chaos")
        r = receipt(out)
        hazards = [h["hazard"] for h in httpx.get(f"{base}/api/chaos/log", timeout=10).json()]
        print("  hazards fired:", hazards)
        check(code == 0 and r["status"] == "VERIFIED", f"agent still reaches VERIFIED under chaos (got {r['status']})")
        check(len(ledger(base, "INV-1042")) == 1, "exactly one INV-1042 row despite retries")
        for h in ("save_500", "session_expiry", "date_rule", "renamed_button"):
            check(h in hazards, f"hazard {h} actually fired")
        ev = events(out)
        detected = " ".join(e.get("what", "") for e in ev if e["type"] == "detected")
        for d in ("server_error", "session_expired", "validation_error"):
            check(d in detected, f"agent detected {d}")
        check(any(e["type"] == "recovery" for e in ev), "agent logged recovery actions")
        check(any(e["type"] == "verified" for e in ev), "agent verified its write landed")
    print("E2E CHAOS VERIFIED")


def mode_business() -> None:
    env = isolated_env()
    clauses = json.loads(BUSINESS.read_text(encoding="utf-8"))
    with company_app(env=env) as base:
        reset(base, BUSINESS_CHAOS)
        code, out = run_agent(env, base, TASK_B, "--contract", str(BUSINESS), "--decide", "reject",
                              "--max-steps", "70", run_id="e2e-business")
        r = receipt(out)
        print("  status:", r["status"])
        for p in r.get("not_done", []) + r.get("waiting", []) + r.get("done", []):
            print(f"    {p.get('ref')}: {p.get('status')} {p.get('rule_id') or ''}")
        check(code == 0 and r["status"] == "VERIFIED_WITH_EXCEPTIONS",
              "task B ends VERIFIED_WITH_EXCEPTIONS (everything doable done, the rest explained)")
        check(len(ledger(base, "INV-7731")) == 1, "duplicate Globex invoice entered exactly once")
        check(len(ledger(base, "INV-1042")) == 1, "Acme invoice entered")
        for inv in ("INV-UC-5521", "INV-3307", "INV-V-19", "ZRG-1"):
            check(len(ledger(base, inv)) == 0, f"{inv} not entered")
        by_ref: dict[str, set] = {}
        for p in r.get("not_done", []) + r.get("waiting", []):
            by_ref.setdefault(p.get("ref"), set()).add((p.get("status"), p.get("rule_id")))
        check(("REJECTED", "over-threshold") in by_ref.get("INV-UC-5521", set()), "over-threshold parked then rejected")
        check(("REJECTED", "credit-note") in by_ref.get("INV-3307", set()), "credit memo parked then rejected")
        check(any(s == "BLOCKED" for s, _ in by_ref.get("INV-V-19", set())), "unknown vendor blocked")
        check(all(s == "BLOCKED" for s, _ in by_ref.get("ZRG-1", {("BLOCKED", None)})),
              "hidden instruction never got past the gateway")
        msgs = httpx.get(f"{base}/api/outbox", params={"to": "alex@company.test"}, timeout=10).json()
        check(msgs, "summary sent to Alex")
    print("E2E BUSINESS VERIFIED")


def mode_crash() -> None:
    env = isolated_env()
    with company_app(env=env) as base:
        reset(base)
        code, out = run_agent(env, base, TASK_A, "--contract", str(ACME), "--decide", "reject",
                              run_id="e2e-crash-1", crash=True)
        check(code == 3, f"first run was killed right after its submit (exit {code})")
        check(len(ledger(base, "INV-1042")) == 1, "the killed run's write landed once")
        code, out = run_agent(env, base, TASK_A, "--contract", str(ACME), "--decide", "reject", run_id="e2e-crash-2")
        r = receipt(out)
        check(code == 0 and r["status"] == "VERIFIED", f"rerun finishes VERIFIED (got {r['status']})")
        check(len(ledger(base, "INV-1042")) == 1, "still exactly one INV-1042 row after the rerun")
    print("CRASH RERUN VERIFIED")


def mode_catalog() -> None:
    env = isolated_env()
    with company_app(env=env) as base:
        reset(base)
        code, out = run_agent(env, base, TASK_C, run_id="e2e-catalog")
        r = receipt(out)
        check(code == 0 and r["status"] == "VERIFIED", f"catalog task VERIFIED (got {r['status']})")
        cols = httpx.get(f"{base}/api/catalog/columns", params={"table": "customers", "column": "email"},
                         timeout=10).json()
        check(cols and "PII" in [t.upper() for t in cols[0]["tags"]], "customers.email tagged PII in the real catalog")
        owner = httpx.get(f"{base}/api/catalog/tables/customers", timeout=10).json()["owner"]
        check(httpx.get(f"{base}/api/outbox", params={"to": owner}, timeout=10).json(), f"owner {owner} notified")
    print("E2E CATALOG VERIFIED")


if __name__ == "__main__":
    modes = {"acme": mode_acme, "contractgen": mode_contractgen, "chaos": mode_chaos, "business": mode_business,
             "crash": mode_crash, "catalog": mode_catalog}
    if len(sys.argv) != 2 or sys.argv[1] not in modes:
        fail(f"usage: e2e.py <{'|'.join(modes)}>")
    modes[sys.argv[1]]()
