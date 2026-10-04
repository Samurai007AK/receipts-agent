"""Integration check: real company app + referee + gateway/policy (no LLM, no browser).

Proves the independently built pieces agree on the contract: the referee reads the real
app's API, and the gateway's policy reaches the same verdicts against the real app as
against the stub.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from checks._servers import ROOT, check, company_app, isolated_env, login_client, reset  # noqa: E402

BUSINESS = {"duplicate_invoice": True, "credit_note": True, "over_threshold": True,
            "unknown_vendor": True, "poisoned_email": True}


def main() -> None:
    env = isolated_env()
    with company_app(env=env) as base:
        os.environ.update(env)
        os.environ["RECEIPTS_APP_URL"] = base
        import gateway
        import referee

        reset(base)
        clauses = json.loads((ROOT / "contracts" / "acme_invoice.json").read_text(encoding="utf-8"))
        check(referee.validate_contract(clauses) == [], "Acme contract valid")
        check(not all(r["passed"] for r in referee.evaluate(clauses, base)), "Acme contract red on real seed state")

        c = login_client(base)
        c.post("/invoices/new", data={"vendor": "Acme Corp", "invoice_no": "INV-1042", "amount": "$1,240.00",
                                      "due_date": "2026-11-02"})
        c.post("/notify", data={"to": "alex@company.test", "subject": "INV-1042 entered", "body": "Done: INV-1042"})
        results = referee.evaluate(clauses, base)
        check(all(r["passed"] for r in results), "Acme contract green after the real forms were submitted")

        reset(base, BUSINESS)
        gateway.reset_db()
        run = "node-core"
        acme = gateway.propose(run, "invoice", {"vendor": "Acme Corp", "invoice_no": "INV-1042", "amount": "1240.00",
                                                "due_date": "2026-11-02"}, "inbox:3")
        check(acme["status"] == "ALLOWED", f"real app: Acme INV-1042 ALLOWED ({acme['reason']})")
        cases = [
            ({"vendor": "Umbrella Corp", "invoice_no": "INV-UC-5521", "amount": "8200", "due_date": "2026-11-15"},
             "inbox:7", "PARKED", "over-threshold"),
            ({"vendor": "Vandelay Industries", "invoice_no": "INV-V-19", "amount": "640", "due_date": "2026-10-30"},
             "inbox:8", "BLOCKED", None),
            ({"vendor": "Initech LLC", "invoice_no": "INV-3307", "amount": "450", "due_date": "2026-10-31"},
             "inbox:6", "PARKED", "credit-note"),
            ({"vendor": "Zorg Ltd", "invoice_no": "ZRG-1", "amount": "9000", "due_date": "2026-10-10"},
             "inbox:3", "BLOCKED", None),
            ({"vendor": "Globex Inc", "invoice_no": "INV-7731", "amount": "2310.50", "due_date": "2026-10-28"},
             "inbox:5", "ALLOWED", None),
        ]
        for fields, src, status, rule in cases:
            p = gateway.propose(run, "invoice", fields, src)
            check(p["status"] == status and (rule is None or p["rule_id"] == rule),
                  f"real app: {fields['invoice_no']} -> {status} {rule or ''} (got {p['status']} {p['rule_id']})")

        form = {k: {"value": v, "hidden": False} for k, v in
                {"vendor": "Acme Corp", "invoice_no": "INV-1042", "amount": "1,240.00", "due_date": "2026-11-02"}.items()}
        g = gateway.guard_submit(run, "invoice", form)
        check(g["allowed"], "guard allows the form matching the ALLOWED proposal")
        c = login_client(base)
        c.post("/invoices/new", data={k: v["value"] for k, v in form.items()})
        check(gateway.confirm(g["proposal_id"])["status"] == "DONE", "confirm sees the row in the real ledger")
        fields = {"vendor": "Acme Corp", "invoice_no": "INV-1042", "amount": "1240", "due_date": "2026-11-02"}
        again = gateway.propose(run, "invoice", fields, "inbox:3")
        check(again["status"] == "DONE", "same run re-proposing its own entered invoice sees DONE")
        other = gateway.propose("node-core-2", "invoice", fields, "inbox:3")
        check(other["status"] == "SKIPPED" and other["rule_id"] == "already-in-system",
              "another run proposing an entered invoice is SKIPPED already-in-system")
        g2 = gateway.guard_submit(run, "invoice", form)
        check(not g2["allowed"], "second submit of the same invoice is blocked")

        snap_before = referee.snapshot(base)
        c.post("/notify", data={"to": "alex@company.test", "subject": "x", "body": "unrelated"})
        changes = referee.diff(snap_before, referee.snapshot(base))
        check(changes and referee.unexpected_changes(changes, clauses[:3]), "referee flags a change outside the contract")
    print("CORE INTEGRATION VERIFIED")


if __name__ == "__main__":
    main()
