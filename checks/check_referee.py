"""Self-check for referee.py.

  .venv\\Scripts\\python checks\\check_referee.py               -> prints REFEREE VERIFIED last
  .venv\\Scripts\\python checks\\check_referee.py independence  -> prints REFEREE INDEPENDENT last
"""
from __future__ import annotations

import ast
import copy
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

sys.stdout.reconfigure(errors="backslashreplace")  # never die printing a localized OS error on cp1252
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from checks._servers import ROOT, check, fail, free_port, stub_app  # noqa: E402

FORBIDDEN = {"gateway", "policy", "browser", "agent", "openai"}  # agent modules + any LLM SDK
ACME = json.loads((ROOT / "contracts" / "acme_invoice.json").read_text(encoding="utf-8"))
ACME_INVOICE = {"vendor": "Acme Corp", "invoice_no": "INV-1042", "amount": 1240.0, "due_date": "2026-11-02"}
ACME_NOTE = {"to": "alex@company.test", "subject": "INV-1042 entered",
             "body": "Hi Alex, Acme invoice INV-1042 ($1,240.00, due 2026-11-02) is now in the AP system."}


def forbidden_imports(source: str) -> list[str]:
    """Top-level module names in `source` that the referee must never import (static + dynamic imports)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            names = [a.name for a in node.names] + ([node.module] if node.module else [])
        elif (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
              and isinstance(node.args[0].value, str)
              and getattr(node.func, "id", getattr(node.func, "attr", "")) in ("__import__", "import_module")):
            names = [node.args[0].value]
        found += [n.split(".")[0] for n in names if n.split(".")[0] in FORBIDDEN]
    return found


def independence() -> None:
    for bad in ("import gateway", "from policy import RULES", "import browser as b",
                "import importlib\nimportlib.import_module('agent')", "__import__('gateway.x')",
                "from openai import OpenAI", "from . import policy"):
        check(forbidden_imports(bad), f"import scan catches: {bad.splitlines()[-1]}")
    check(not forbidden_imports("import httpx\nfrom pathlib import Path"), "import scan passes a clean module")
    source = (ROOT / "referee.py").read_text(encoding="utf-8")
    hits = forbidden_imports(source)
    check(not hits, f"referee.py imports none of {sorted(FORBIDDEN)} (found {hits})")
    print("REFEREE INDEPENDENT", flush=True)


def plain_text_server() -> tuple[ThreadingHTTPServer, str]:
    """A server whose /api/* answers 200 text/plain, to prove non-JSON handling."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"<html>definitely not json</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def main() -> None:
    with stub_app() as base:
        http = httpx.Client(base_url=base, timeout=10)

        def reset() -> None:
            http.post("/api/reset", json={}).raise_for_status()

        def add_invoice(**row) -> None:
            http.post("/stub/invoices", json=row).raise_for_status()

        def add_message(**msg) -> None:
            http.post("/stub/outbox", json=msg).raise_for_status()

        print("[contract file + english]")
        check(referee.validate_contract(ACME) == [], "acme contract is valid")
        check(all(c.get("ref") and c.get("description") for c in ACME), "every acme clause has ref and description")
        english = [referee.to_english(c) for c in ACME]
        check(english == ["Exactly 1 ledger row for INV-1042", "Ledger INV-1042: amount equals 1,240.00",
                          "Ledger INV-1042: due date equals 2026-11-02",
                          "A message to alex@company.test mentions INV-1042"], f"english reads naturally: {english}")
        check(referee.to_english({"kind": "row_count", "path": "/api/vendors", "equals": 4}) == "Exactly 4 vendors",
              "english: plural with no subject")
        check(referee.to_english({"kind": "row_count", "path": "/api/ledger?invoice_no=X-1", "equals": 0})
              == "No ledger rows for X-1", "english: zero rows")
        check(referee.to_english({"kind": "file_contains", "path": "out/r.txt", "text": "done"})
              == "The file out/r.txt contains done", "english: file")
        check(referee.to_english({"kind": "nope"}).startswith("Unrecognised"), "english: unknown kind does not raise")
        check(isinstance(referee.to_english(None), str), "english: garbage clause does not raise")  # type: ignore[arg-type]

        print("[normalization]")
        for raw, want in [("$1,240.00", "1240.00"), ("USD 2,310.50", "2310.50"), ("1240", "1240.00"),
                          (1240, "1240.00"), (1240.5, "1240.50"), ("-$450.00", "-450.00"), ("$ 8,200", "8200.00"),
                          ("2026-11-02", "2026-11-02"), ("02/11/2026", "2026-11-02"), ("2 November 2026", "2026-11-02"),
                          ("November 2, 2026", "2026-11-02"), ("2 Nov 2026", "2026-11-02"), ("Nov. 2, 2026", "2026-11-02"),
                          ("31 February 2026", "31 february 2026"), ("  Acme   CORP ", "acme corp"), ("INV-1042", "inv-1042"),
                          ("1,24", "1,24"), (None, "")]:
            check(referee._norm(raw) == want, f"norm({raw!r}) == {want!r}")
        print("[a] seed state is red")
        reset()
        red = referee.evaluate(ACME, base)
        check(len(red) == 4 and not any(r["passed"] for r in red), "every acme clause fails on the seed state")
        check(red[0]["observed"] == 0, "row_count observes 0 rows")
        check(red[1]["observed"] == "no rows were returned", "field clause explains the missing row")
        ev = red[0]["evidence"]
        check(ev["request"] == "GET /api/ledger?vendor=Acme%20Corp&invoice_no=INV-1042" and ev["status"] == 200
              and ev["body_excerpt"] == "[]", f"evidence shows exact request, status, body: {ev}")
        check(red[3]["evidence"]["request"] == "GET /api/outbox?to=alex@company.test", "outbox evidence request")
        check(all(set(r) == {"clause", "passed", "observed", "evidence"}
                  and set(r["evidence"]) == {"request", "status", "body_excerpt"} for r in red), "result shape")

        print("[b] after the work is done it is green")
        add_invoice(**ACME_INVOICE)
        add_message(**ACME_NOTE)
        green = referee.evaluate(ACME, base)
        check(all(r["passed"] for r in green), f"every acme clause passes: {[r['observed'] for r in green]}")
        check(green[1]["observed"] == 1240.0 and green[2]["observed"] == "2026-11-02", "observed raw values")
        check(len(green[1]["evidence"]["body_excerpt"]) <= 300, "body excerpt is at most 300 chars")
        add_invoice(**ACME_INVOICE)
        dup = referee.evaluate(ACME, base)
        check(not dup[0]["passed"] and dup[0]["observed"] == 2, "a duplicate row fails row_count")

        print("[c] wrong-amount mutation fails only the amount clause")
        reset()
        add_invoice(**{**ACME_INVOICE, "amount": 1420.0})
        add_message(**ACME_NOTE)
        bad = referee.evaluate(ACME, base)
        check([r["passed"] for r in bad] == [True, False, True, True], "only the amount clause fails")
        check(bad[1]["observed"] == 1420.0, "observed shows the wrong amount")

        print("[d] every clause kind passes and fails")
        reset()
        add_invoice(vendor="Initech LLC", invoice_no="INV-T1", amount=10.0, due_date="2026-12-01",
                    tags=["PII", "Finance"])
        add_message(to="Alex@Company.test", subject="Weekly", body="Processed:\n  inv-7731 and others")
        cases = [
            ({"kind": "row_count", "path": "/api/vendors", "equals": 4}, True),
            ({"kind": "row_count", "path": "/api/vendors", "equals": 5}, False),
            ({"kind": "row_count", "path": "/api/inbox/3", "equals": 1}, False),  # object, not a list
            ({"kind": "http_field_equals", "path": "/api/ledger?invoice_no=INV-0988", "field": "amount",
              "equals": "$980.00"}, True),
            ({"kind": "http_field_equals", "path": "/api/ledger?invoice_no=INV-0988", "field": "amount",
              "equals": 981}, False),
            ({"kind": "http_field_equals", "path": "/api/ledger?invoice_no=INV-0988", "field": "due_date",
              "equals": "30 September 2026"}, True),
            ({"kind": "http_field_equals", "path": "/api/ledger?invoice_no=INV-0988", "field": "due_date",
              "equals": "30/09/2026"}, True),
            ({"kind": "http_field_equals", "path": "/api/ledger?invoice_no=INV-0988", "field": "due_date",
              "equals": "September 30, 2026"}, True),
            ({"kind": "http_field_equals", "path": "/api/ledger?invoice_no=INV-0988", "field": "due_date",
              "equals": "2026-09-29"}, False),
            ({"kind": "http_field_equals", "path": "/api/ledger?invoice_no=INV-7610", "field": "amount",
              "equals": "USD 1,775.00"}, True),
            ({"kind": "http_field_equals", "path": "/api/ledger?invoice_no=INV-0988", "field": "vendor",
              "equals": "  acme   CORP "}, True),
            ({"kind": "http_field_equals", "path": "/api/inbox/3", "field": "from",
              "equals": "billing@acme.example"}, True),
            ({"kind": "http_field_equals", "path": "/api/snapshot", "field": "invoices.1.invoice_no",
              "equals": "INV-7610"}, True),
            ({"kind": "http_field_equals", "path": "/api/snapshot", "field": "invoices.9.invoice_no",
              "equals": "INV-7610"}, False),
            ({"kind": "http_field_equals", "path": "/api/ledger?invoice_no=INV-0988", "field": "colour",
              "equals": "red"}, False),
            ({"kind": "http_field_equals", "path": "/api/inbox/999", "field": "from", "equals": "x"}, False),
            ({"kind": "http_field_contains", "path": "/api/ledger?invoice_no=INV-T1", "field": "tags",
              "contains": "pii"}, True),
            ({"kind": "http_field_contains", "path": "/api/ledger?invoice_no=INV-T1", "field": "tags",
              "contains": "secret"}, False),
            ({"kind": "http_field_contains", "path": "/api/ledger?invoice_no=INV-T1", "field": "tags.0",
              "contains": "pI"}, True),
            ({"kind": "http_field_contains", "path": "/api/inbox/3", "field": "body",
              "contains": "PAYMENT DUE BY"}, True),
            ({"kind": "http_field_contains", "path": "/api/inbox/3", "field": "body",
              "contains": "wire transfer"}, False),
            ({"kind": "http_field_contains", "path": "/api/ledger?invoice_no=INV-T1", "field": "amount",
              "contains": "10"}, False),  # a number is neither a list nor text
            ({"kind": "outbox_contains", "to": "alex@company.test", "text": "INV-7731"}, True),
            ({"kind": "outbox_contains", "to": "alex@company.test", "text": "weekly"}, True),
            ({"kind": "outbox_contains", "to": "alex@company.test", "text": "INV-1042"}, False),
            ({"kind": "outbox_contains", "to": "sam@company.test", "text": "weekly"}, False),
        ]
        tmp = Path(tempfile.mkdtemp(prefix="referee-check-")) / "report.txt"
        tmp.write_text("Run summary\nStatus:   DONE\nCafé entries: 3\n", encoding="utf-8")
        cases += [
            ({"kind": "file_contains", "path": str(tmp), "text": "status: done"}, True),
            ({"kind": "file_contains", "path": str(tmp), "text": "café"}, True),
            ({"kind": "file_contains", "path": str(tmp), "text": "failed"}, False),
            ({"kind": "file_contains", "path": str(tmp.with_name("missing.txt")), "text": "x"}, False),
            ({"kind": "file_contains", "path": "contracts/acme_invoice.json", "text": "INV-1042"}, True),
        ]
        results = referee.evaluate([c for c, _ in cases], base)
        for (clause, want), res in zip(cases, results):
            label = referee.to_english(clause)
            check(res["passed"] is want, f"{clause['kind']}: {label} -> {'pass' if want else 'fail'}")
            if not want:
                check(res["observed"] is not None and res["observed"] != "", "  failure has an observed value")
        kinds = {c["kind"] for c, w in cases if w} & {c["kind"] for c, w in cases if not w}
        check(kinds == set(referee.CLAUSE_KINDS), "every clause kind has a passing and a failing case")

        print("[d] evaluate never raises")
        dead = f"http://127.0.0.1:{free_port()}"
        [down] = referee.evaluate([ACME[0]], dead)
        check(not down["passed"] and "could not reach the app" in down["observed"]
              and down["evidence"]["status"] is None, f"network error -> failed clause: {down['observed']}")
        server, text_base = plain_text_server()
        try:
            [nj] = referee.evaluate([ACME[1]], text_base)
        finally:
            server.shutdown()
        check(not nj["passed"] and "not JSON" in nj["observed"] and nj["evidence"]["status"] == 200,
              "non-JSON response -> failed clause")
        [nf] = referee.evaluate([{"kind": "row_count", "path": "/api/nope", "equals": 0}], base)
        check(not nf["passed"] and nf["evidence"]["status"] == 404 and "HTTP 404" in nf["observed"], "HTTP 404")
        junk = referee.evaluate([{"kind": "row_count"}, "garbage", {"kind": "teleport"}], base)  # type: ignore[list-item]
        check(len(junk) == 3 and not any(r["passed"] for r in junk)
              and all(r["observed"].startswith("invalid clause") for r in junk), "malformed clauses -> failed")
        check(referee.evaluate([], base) == [], "empty contract -> no results")

        print("[e] validate_contract catches malformed shapes")
        bad_contracts = {
            "empty list": [],
            "not a list": {"kind": "row_count"},
            "clause not an object": ["row_count"],
            "unknown kind": [{"kind": "teleport"}],
            "row_count without path": [{"kind": "row_count", "equals": 1}],
            "row_count without equals": [{"kind": "row_count", "path": "/api/ledger"}],
            "path outside /api/": [{"kind": "row_count", "path": "/invoices", "equals": 1}],
            "path not text": [{"kind": "row_count", "path": 5, "equals": 1}],
            "row_count equals text": [{"kind": "row_count", "path": "/api/ledger", "equals": "1"}],
            "row_count equals float": [{"kind": "row_count", "path": "/api/ledger", "equals": 1.0}],
            "row_count equals bool": [{"kind": "row_count", "path": "/api/ledger", "equals": True}],
            "row_count negative": [{"kind": "row_count", "path": "/api/ledger", "equals": -1}],
            "field_equals without field": [{"kind": "http_field_equals", "path": "/api/ledger", "equals": 1}],
            "field_equals without equals": [{"kind": "http_field_equals", "path": "/api/ledger", "field": "a"}],
            "field_equals equals null": [{"kind": "http_field_equals", "path": "/api/x", "field": "a", "equals": None}],
            "field_equals empty field": [{"kind": "http_field_equals", "path": "/api/x", "field": " ", "equals": 1}],
            "field_contains without contains": [{"kind": "http_field_contains", "path": "/api/x", "field": "t"}],
            "field_contains contains list": [{"kind": "http_field_contains", "path": "/api/x", "field": "t",
                                              "contains": ["a"]}],
            "field_contains bad path": [{"kind": "http_field_contains", "path": "api/x", "field": "t",
                                         "contains": "a"}],
            "outbox without to": [{"kind": "outbox_contains", "text": "x"}],
            "outbox without text": [{"kind": "outbox_contains", "to": "a@b.c"}],
            "outbox to not an address": [{"kind": "outbox_contains", "to": "alex", "text": "x"}],
            "outbox empty text": [{"kind": "outbox_contains", "to": "a@b.c", "text": ""}],
            "file without path": [{"kind": "file_contains", "text": "x"}],
            "file without text": [{"kind": "file_contains", "path": "out/x.txt"}],
            "ref not text": [{"kind": "outbox_contains", "to": "a@b.c", "text": "x", "ref": 7}],
        }
        for name, contract in bad_contracts.items():
            errors = referee.validate_contract(contract)  # type: ignore[arg-type]
            check(errors and all(isinstance(e, str) and e for e in errors), f"rejects {name}: {errors[:1]}")
        mixed = referee.validate_contract([ACME[0], {"kind": "teleport"}, {"kind": "row_count", "equals": 1}])
        check(len(mixed) == 2 and mixed[0].startswith("Clause 2") and mixed[1].startswith("Clause 3"),
              f"errors name the offending clause: {mixed}")

        print("[f] snapshot + diff + unexpected_changes")
        reset()
        before = referee.snapshot(base)
        check(set(before) == {"invoices", "outbox", "catalog"} and len(before["invoices"]) == 2, "snapshot shape")
        check(referee.diff(before, referee.snapshot(base)) == [], "no changes -> empty diff")
        add_invoice(**ACME_INVOICE)
        add_message(**ACME_NOTE)
        after = referee.snapshot(base)
        changes = referee.diff(before, after)
        check(sorted((c["table"], c["ref"], c["change"]) for c in changes)
              == [("invoices", "INV-1042", "added"), ("outbox", "notify:alex@company.test", "added")],
              f"diff finds the added row and message: {[(c['table'], c['ref'], c['change']) for c in changes]}")
        check(changes[0]["row"]["invoice_no"] == "INV-1042", "diff carries the row")
        check(referee.unexpected_changes(changes, ACME) == [], "changes named by the contract are expected")
        no_refs = [{k: v for k, v in c.items() if k != "ref"} for c in ACME]
        check(referee.unexpected_changes(changes, no_refs) == [], "expected via path / outbox address without refs")
        add_invoice(vendor="Zorg Ltd", invoice_no="ZRG-1", amount=9000.0, due_date="2026-10-10")
        changes = referee.diff(before, referee.snapshot(base))
        unexpected = referee.unexpected_changes(changes, ACME)
        check([c["ref"] for c in unexpected] == ["ZRG-1"], "a change the contract does not name is flagged")
        check(referee.unexpected_changes(changes, ACME, allowed_refs=("zrg-1",)) == [], "allowed_refs accepts it")
        check(referee.unexpected_changes(changes, [{"kind": "outbox_contains", "to": "x@y.z", "text": "zrg-1"}],
                                         ("INV-1042", "notify:alex@company.test")) == [],
              "a ref appearing in a clause's text is expected")
        near = [{"table": "invoices", "ref": "INV-10", "change": "added", "row": {}},
                {"table": "invoices", "ref": "", "change": "added", "row": {}}]
        check(len(referee.unexpected_changes(near, ACME)) == 2, "partial-token and empty refs are not excused")
        b2 = copy.deepcopy(after)
        a2 = copy.deepcopy(after)
        a2["invoices"][0]["status"] = "void"
        del a2["outbox"][0]
        b2["catalog"] = {"customers": {"email": {"tags": []}, "name": {"tags": []}}}
        a2["catalog"] = {"customers": {"email": {"tags": ["PII"]}, "name": {"tags": []}, "phone": {"tags": []}}}
        got = sorted((c["table"], c["ref"], c["change"]) for c in referee.diff(b2, a2))
        check(got == [("catalog", "customers.email", "changed"), ("catalog", "customers.phone", "added"),
                      ("invoices", "INV-0988", "changed"), ("outbox", "notify:alex@company.test", "removed")],
              f"diff detects changed / removed / catalog column changes: {got}")
        tag_clause = [{"kind": "http_field_contains", "path": "/api/catalog/customers.email", "field": "tags",
                       "contains": "PII", "ref": "customers.email"}]
        cat_changes = [c for c in referee.diff(b2, a2) if c["table"] == "catalog"]
        check([c["ref"] for c in referee.unexpected_changes(cat_changes, tag_clause)] == ["customers.phone"],
              "catalog change named by the contract is expected; the other is flagged")

        print("[g] verdict")
        reset()
        red = referee.evaluate(ACME, base)
        add_invoice(**ACME_INVOICE)
        add_message(**ACME_NOTE)
        green = referee.evaluate(ACME, base)
        v = referee.verdict(green, [], [])
        check(v["status"] == "VERIFIED" and v["exceptions"] == [] and v["failures"] == [], "all green -> VERIFIED")

        def props(status: str, ref_inv: str = "INV-1042") -> list[dict]:
            return [{"proposal_id": "P-inv", "kind": "invoice", "ref": ref_inv, "status": status,
                     "rule_id": "over-threshold", "reason": "needs approval"},
                    {"proposal_id": "P-note", "kind": "notification", "ref": "notify:alex@company.test",
                     "status": "SKIPPED", "rule_id": "already-in-system", "reason": "already sent"}]

        for status in ("SKIPPED", "BLOCKED", "PARKED", "REJECTED"):
            v = referee.verdict(red, props(status), [])
            check(v["status"] == "VERIFIED_WITH_EXCEPTIONS" and len(v["exceptions"]) == 4 and v["failures"] == [],
                  f"red clauses excused by {status} proposals -> VERIFIED_WITH_EXCEPTIONS")
        exc = referee.verdict(red, props("PARKED"), [])["exceptions"][0]
        check(exc == {"clause": ACME[0], "proposal_id": "P-inv", "status": "PARKED", "rule_id": "over-threshold",
                      "reason": "needs approval"}, "exception lists clause, proposal_id, status, rule_id, reason")
        no_ref = [{k: v for k, v in c.items() if k != "ref"} for c in ACME[:3]]
        [nr] = referee.evaluate(no_ref[:1], f"http://127.0.0.1:{free_port()}")
        check(referee.verdict([nr], props("BLOCKED"), [])["status"] == "VERIFIED_WITH_EXCEPTIONS",
              "proposal ref appearing in the clause path excuses it")
        mixed = [green[0], red[1], green[2], green[3]]
        check(referee.verdict(mixed, props("BLOCKED"), [])["status"] == "VERIFIED_WITH_EXCEPTIONS",
              "a single excused failure -> VERIFIED_WITH_EXCEPTIONS")
        for status in ("DONE", "ALLOWED", "SUBMITTED"):
            v = referee.verdict(red, props(status), [])
            check(v["status"] == "FAILED" and len(v["failures"]) == 3 and len(v["exceptions"]) == 1,
                  f"{status} proposal does not excuse -> FAILED")
        check(referee.verdict(red, props("BLOCKED", "INV-9999"), [])["status"] == "FAILED",
              "proposal for another ref does not excuse -> FAILED")
        check(referee.verdict(red, [], [])["status"] == "FAILED", "no proposals -> FAILED")
        retried = props("BLOCKED") + [{"proposal_id": "P-inv2", "ref": "INV-1042", "status": "DONE"}]
        check(referee.verdict(red, retried, [])["status"] == "FAILED",
              "a blocked attempt cannot excuse a clause the agent later did (DONE) but got wrong")
        surprise = [{"table": "invoices", "ref": "ZRG-1", "change": "added", "row": {}}]
        v = referee.verdict(green, [], surprise)
        check(v["status"] == "FAILED" and v["unexpected"] == surprise, "all green but unexpected change -> FAILED")
        check(referee.verdict(red, props("BLOCKED"), surprise)["status"] == "FAILED",
              "excused failures plus unexpected change -> FAILED")
        http.close()
    print("REFEREE VERIFIED", flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "independence":
        independence()
    elif len(sys.argv) > 1:
        fail(f"unknown mode {sys.argv[1]!r}; use no argument or 'independence'")
    else:
        import referee  # noqa: E402  (imported here so the independence mode never executes it)
        main()
