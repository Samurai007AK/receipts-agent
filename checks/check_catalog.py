"""Self-check for the data-catalog domain (leaf 1.4.1): app pages/API, column_tag policy, contract.

  .venv\\Scripts\\python checks\\check_catalog.py              -> prints CATALOG VERIFIED last
  .venv\\Scripts\\python checks\\check_catalog.py regressions  -> prints CATALOG REGRESSIONS VERIFIED last

The agent core (agent.py, browser.py, gateway.py, referee.py) is untouched: this proves the
environment and the domain policy alone are enough for the agent to work a second domain.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from checks._servers import PY, ROOT, check, company_app, fail, isolated_env, login_client, reset  # noqa: E402

CONTRACT = ROOT / "contracts" / "catalog_pii.json"
OWNER = "priya.shah@company.test"
CUSTOMERS = ["id", "email", "full_name", "signup_date", "country"]
TAG_FORM = '<form class="record" method="post" action="/catalog/customers/tags" data-record="column_tag">'
FIVE_HUNDRED = "500 Internal Server Error — the posting service timed out. Please try again."
REGRESSIONS = [(["check_app.py", "core"], "APP CORE VERIFIED"), (["check_app.py", "chaos"], "APP CHAOS VERIFIED"),
               (["check_gateway.py", "rules"], "GATEWAY RULES VERIFIED"),
               (["check_gateway.py", "flow"], "GATEWAY FLOW VERIFIED"),
               (["check_referee.py"], "REFEREE VERIFIED"), (["check_browser.py"], "BROWSER VERIFIED")]


def api(base: str, path: str, **params: str) -> object:
    r = httpx.get(base + path, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def alerts(html: str) -> list[str]:
    return re.findall(r'<div class="error" role="alert">(.*?)</div>', html, re.S)


def tags(base: str, table: str = "customers", column: str = "email") -> list[str]:
    return api(base, "/api/catalog/columns", table=table, column=column)[0]["tags"]


def hazards(base: str, name: str) -> list[dict]:
    return [h for h in api(base, "/api/chaos/log") if h["hazard"] == name]


class RecordForm(HTMLParser):
    """Fields of the page's data-record form: {name: {"type", "value", "options"}} (like browser.py reads them)."""

    def __init__(self) -> None:
        super().__init__()
        self.kind, self.fields, self._in, self._select = None, {}, False, None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if tag == "form" and "data-record" in a:
            self.kind, self._in = a["data-record"], True
        elif self._in and tag == "input" and a.get("name"):
            self.fields[a["name"]] = {"type": a.get("type", "text"), "value": a.get("value", ""), "options": []}
        elif self._in and tag == "select":
            self._select = a["name"]
            self.fields[a["name"]] = {"type": "select", "value": "", "options": []}
        elif self._select and tag == "option":
            field = self.fields[self._select]
            field["options"].append(a.get("value", ""))
            if "selected" in a:
                field["value"] = a.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._in = False
        elif tag == "select":
            self._select = None


def record_form(html: str) -> RecordForm:
    parser = RecordForm()
    parser.feed(html)
    return parser


def submitted(form: RecordForm, **values: str) -> dict:
    """The {name: {"value", "hidden"}} a browser submit of `form` sends after filling `values`."""
    return {name: {"value": values.get(name, f["value"]), "hidden": f["type"] == "hidden"}
            for name, f in form.fields.items()}


# ---------------------------------------------------------------- default mode

def app_checks(base: str) -> None:
    print("[a] JSON API and seed")
    reset(base)
    tables = api(base, "/api/catalog/tables")
    check([t["name"] for t in tables] == ["customers", "orders", "payments"]
          and all(set(t) == {"name", "owner", "description", "columns"} and t["columns"] == 5 for t in tables),
          "GET /api/catalog/tables -> [{name, owner, description, columns: n}]")
    owners = {t["name"]: t["owner"] for t in tables}
    check(owners == {"customers": OWNER, "orders": "marco.rossi@company.test", "payments": "finance-data@company.test"},
          "owners are internal company.test addresses")
    t = api(base, "/api/catalog/tables/customers")
    check(set(t) == {"name", "owner", "description", "columns"} and t["owner"] == OWNER and t["description"],
          "GET /api/catalog/tables/customers -> owner + description")
    check([c["name"] for c in t["columns"]] == CUSTOMERS
          and all(set(c) == {"name", "type", "description", "tags"} and c["type"] and c["description"]
                  for c in t["columns"]), "customers columns carry name, type, description, tags")
    check(next(c for c in t["columns"] if c["name"] == "email")["tags"] == [], "customers.email starts untagged")
    check(api(base, "/api/catalog/tables/CUSTOMERS")["name"] == "customers", "table lookup is case-insensitive")
    check(httpx.get(base + "/api/catalog/tables/nope", timeout=10).status_code == 404, "unknown table -> 404")
    rows = api(base, "/api/catalog/columns", table="Customers", column="EMAIL")
    check(rows == [{"table": "customers", "column": "email", "type": "string", "tags": []}],
          "GET /api/catalog/columns filters table + column case-insensitively")
    check(tags(base, "orders", "customer_id") == ["FK"] and tags(base, "customers", "id") == ["PK"],
          "some columns are pre-tagged (PK, FK)")
    check(len(api(base, "/api/catalog/columns")) == 15 and len(api(base, "/api/catalog/columns", column="id")) == 3
          and api(base, "/api/catalog/columns", table="nope") == [], "unfiltered / column-only / no-match filters")
    paths = api(base, "/openapi.json")["paths"]
    reads = ["/api/catalog/tables", "/api/catalog/tables/{name}", "/api/catalog/columns"]
    check(all(paths[p]["get"].get("summary") for p in reads), "openapi lists the catalog reads with summaries")
    check(not any(not p.startswith("/api/") for p in paths), "catalog HTML routes stay out of openapi")
    check(api(base, "/api/snapshot")["catalog"] == {}, "untouched catalog snapshots as {}")

    print("[b] pages and the record form")
    anon = httpx.Client(base_url=base, follow_redirects=False, timeout=10)
    for path in ("/catalog", "/catalog/customers"):
        r = anon.get(path)
        check(r.status_code == 303 and r.headers["location"] == f"/login?next={path}", f"login wall: {path}")
    r = anon.post("/catalog/customers/tags", data={"table": "customers", "column": "email", "tag": "PII"})
    check(r.status_code == 303 and r.headers["location"].startswith("/login?next=") and tags(base) == [],
          "anonymous tag POST bounced, nothing written")
    c = login_client(base)
    r = c.get("/")
    check('href="/catalog"' in r.text and ">Catalog</a>" in r.text and "3 tables in the data catalog" in r.text,
          "portal and nav link to Catalog")
    r = c.get("/catalog")
    check(r.status_code == 200 and '<a href="/catalog" aria-current="page">Catalog</a>' in r.text
          and all(f'href="/catalog/{n}"' in r.text for n in owners) and all(o in r.text for o in owners.values())
          and all(f">{h}</th>" in r.text for h in ("Table", "Owner", "Description", "Columns")),
          "/catalog lists tables with owner, description and column count")
    r = c.get("/catalog/customers")
    check(r.status_code == 200 and OWNER in r.text and TAG_FORM in r.text and r.text.count("data-record=") == 1,
          "/catalog/customers shows the owner and one column_tag record form")
    check('<input type="hidden" name="table" value="customers">' in r.text, "hidden table field")
    check('<label for="column">Column</label>' in r.text and '<select id="column" name="column">' in r.text
          and '<label for="tag">Tag</label>' in r.text and 'id="tag" name="tag"' in r.text
          and 'placeholder="e.g. PII"' in r.text, "labelled Column select and Tag input")
    form = record_form(r.text)
    check(form.kind == "column_tag" and set(form.fields) == {"table", "column", "tag"}
          and form.fields["column"]["options"] == ["", *CUSTOMERS], "select offers every column (values = names)")
    check('<button type="submit">Save</button>' in r.text and "More actions" not in r.text, "Save button")
    check('<span class="chip">PK</span>' in r.text, "tags render as chips")
    check(c.get("/catalog/nope").status_code == 404, "unknown table page -> 404")

    print("[c] tagging: validation, write, idempotency")
    bad = {"empty": ({"table": "customers", "column": "", "tag": ""}, 2),
           "short tag": ({"table": "customers", "column": "email", "tag": "P"}, 1),
           "spaces in tag": ({"table": "customers", "column": "email", "tag": "P I I"}, 1),
           "unknown column": ({"table": "customers", "column": "phone", "tag": "PII"}, 1),
           "other table": ({"table": "orders", "column": "email", "tag": "PII"}, 1)}
    for name, (data, n) in bad.items():
        r = c.post("/catalog/customers/tags", data=data)
        check(r.status_code == 422 and len(alerts(r.text)) == n and TAG_FORM in r.text, f"{name} -> 422 with {n} alert(s)")
    r = c.post("/catalog/customers/tags", data={"table": "customers", "column": "email", "tag": "P"})
    check('<option value="email" selected>' in r.text and 'value="P"' in r.text, "422 re-renders submitted values")
    check(tags(base) == [] and api(base, "/api/snapshot")["catalog"] == {}, "invalid posts wrote nothing")
    r = c.post("/catalog/customers/tags", data={"table": "customers", "column": "email", "tag": "pii"})
    check(r.status_code == 303 and r.headers["location"] == "/catalog/customers?tagged=email&tag=PII",
          "valid tag -> 303 /catalog/customers?tagged=email")
    r = c.get(r.headers["location"])
    check('<div class="notice" role="status">Tagged customers.email as PII</div>' in r.text and not alerts(r.text)
          and '<span class="chip pii">PII</span>' in r.text, "status banner and PII chip")
    check(tags(base) == ["PII"], "API shows customers.email tagged PII (upper-cased)")
    check(c.post("/catalog/customers/tags", data={"table": "customers", "column": "email", "tag": "PII"}).status_code
          == 303 and tags(base) == ["PII"], "tagging again is idempotent")
    check(c.post("/catalog/customers/tags", data={"column": "full_name", "tag": "GDPR"}).status_code == 303
          and tags(base, "customers", "full_name") == ["GDPR"], "the path names the table (hidden field optional)")
    reset(base)
    check(tags(base) == [] and api(base, "/api/snapshot")["catalog"] == {}, "reset restores the catalog seed")

    print("[d] chaos applies to the tag form")
    good = {"table": "customers", "column": "email", "tag": "PII"}
    reset(base, {"save_500": 1})
    c = login_client(base)
    r = c.post("/catalog/customers/tags", data=good)
    check(r.status_code == 500 and alerts(r.text) == [FIVE_HUNDRED] and tags(base) == [], "save_500 -> 500, nothing written")
    check(c.post("/catalog/customers/tags", data=good).status_code == 303 and tags(base) == ["PII"], "retry succeeds")
    check(len(hazards(base, "save_500")) == 1, "save_500 logged")
    reset(base, {"session_expiry": True})
    c = login_client(base)
    r = c.post("/catalog/customers/tags", data=good)
    check(r.status_code == 303 and r.headers["location"] == "/login?next=/catalog/customers&expired=1"
          and tags(base) == [], "session_expiry -> /login?next=/catalog/customers&expired=1, nothing written")
    check(c.get("/catalog").status_code == 303 and len(hazards(base, "session_expiry")) == 1, "session dropped and logged")
    reset(base, {"renamed_button": True})
    c = login_client(base)
    r = c.get("/catalog/customers")
    check(">Save</button>" not in r.text and '<details class="more"><summary>More actions</summary>'
          '<button type="submit">Submit for posting</button></details>' in r.text, "renamed_button: More actions variant")
    check(len(hazards(base, "renamed_button")) == 1, "renamed_button logged")
    check(c.post("/catalog/customers/tags", data=good).status_code == 303 and tags(base) == ["PII"],
          "renamed form still posts")

    print("[e] snapshot + referee.diff")
    import referee
    reset(base)
    before = referee.snapshot(base)
    login_client(base).post("/catalog/customers/tags", data=good)
    after = referee.snapshot(base)
    check(after["catalog"] == {"customers": {"email": {"type": "string", "tags": ["PII"],
                                                        "description": "Primary contact email address of the customer."}}},
          "snapshot catalog = {table: {column: info}} for changed columns")
    changes = referee.diff(before, after)
    check([(x["table"], x["ref"]) for x in changes] == [("catalog", "customers.email")],
          f"diff names the change customers.email: {[(x['table'], x['ref'], x['change']) for x in changes]}")
    clauses = json.loads(CONTRACT.read_text(encoding="utf-8"))
    check(referee.unexpected_changes(changes, clauses) == [], "the contract expects the customers.email change")


def gateway_checks(base: str, env: dict[str, str]) -> None:
    print("[f] policy + gateway for column_tag")
    os.environ.update(env)
    os.environ["RECEIPTS_APP_URL"] = base
    import gateway
    import policy
    import referee
    reset(base)
    full = {"table": "customers", "column": "email", "tag": "PII"}
    check(policy.EXISTS["column_tag"](full) is False, "EXISTS false before tagging")
    check(policy.EXISTS["column_tag"]({"table": "orders", "column": "customer_id", "tag": "fk"}) is True,
          "EXISTS true for a tag already on the column (case-insensitive)")
    check(policy.REF["column_tag"](full) == "customers.email", "REF = <table>.<column>")
    check(policy.REF["column_tag"]({"column": "email", "tag": "PII"}) == "customers.email",
          "REF infers the table of a unique column")
    check(policy.EVIDENCE["column_tag"](full) == "GET /api/catalog/columns?table=customers&column=email -> tags (none)",
          "EVIDENCE before tagging")
    for fields, why in (({"table": "customers", "column": "phone", "tag": "PII"}, "does not exist in table customers"),
                        ({"table": "nope", "column": "email", "tag": "PII"}, "does not exist in table nope"),
                        ({"column": "id", "tag": "PII"}, "several tables (customers, orders, payments)"),
                        ({"tag": "PII"}, "column is missing")):
        p = gateway.propose("cat-bad", "column_tag", fields)
        check(p["status"] == "BLOCKED" and p["rule_id"] == "unknown-column" and why in p["reason"],
              f"unknown-column BLOCKED: {p['reason']}")
    for kind, fields in (("invoice", {"vendor": "Acme Corp", "invoice_no": "INV-1042", "amount": "1240.00",
                                      "due_date": "2026-11-02"}), ("notification", {"to": "x@outside.example"})):
        check(gateway.propose("cat-bad", kind, fields)["rule_id"] in ("not-in-source", "external-recipient"),
              f"{kind} rules unchanged")

    c = login_client(base)
    form = record_form(c.get("/catalog/customers").text)
    p = gateway.propose("cat-run", "column_tag", full)
    check(p["status"] == "ALLOWED" and p["ref"] == "customers.email", f"propose {{table, column, tag}} ALLOWED: {p}")
    g = gateway.guard_submit("cat-run", "column_tag", submitted(form, column="email", tag="SECRET"))
    check(not g["allowed"] and g["rule_id"] == "no-matching-proposal", "a form with another tag is refused")
    g = gateway.guard_submit("cat-run", "column_tag", submitted(form, column="email", tag="PII"))
    check(g["allowed"] and g["status"] == "SUBMITTED" and g["proposal_id"] == p["proposal_id"],
          "guard_submit matches the real form (hidden table included)")
    agent_like = gateway.propose("cat-agent", "column_tag", {"column": "email", "tag": "pii"})
    g = gateway.guard_submit("cat-agent", "column_tag", submitted(form, column="email", tag="PII"))
    check(agent_like["status"] == "ALLOWED" and agent_like["ref"] == "customers.email" and g["allowed"],
          "a proposal of only the visible fields (column, tag) also matches the form")
    check(gateway.confirm(p["proposal_id"])["status"] == "UNCONFIRMED", "confirm before the write -> UNCONFIRMED")
    r = c.post("/catalog/customers/tags", data={k: f["value"] for k, f in submitted(form, column="email", tag="PII").items()})
    check(r.status_code == 303, "the guarded form posts")
    done = gateway.confirm(p["proposal_id"])
    check(done == {"status": "DONE", "evidence": "GET /api/catalog/columns?table=customers&column=email -> tags PII"},
          f"confirm -> DONE with evidence: {done}")
    g = gateway.guard_submit("cat-run", "column_tag", submitted(form, column="email", tag="PII"))
    check(not g["allowed"] and g["rule_id"] == "duplicate-submit", "a second submit is refused as a duplicate")
    check(gateway.propose("cat-other", "column_tag", full)["status"] == "SKIPPED", "another run sees SKIPPED")
    note = gateway.propose("cat-run", "notification", {"to": OWNER, "subject": "customers.email tagged PII",
                                                       "body": "Hi Priya, I tagged customers.email as PII."})
    check(note["status"] == "ALLOWED" and note["ref"] == f"notify:{OWNER}", "notifying the owner is ALLOWED")

    print("[g] contract catalog_pii.json")
    clauses = json.loads(CONTRACT.read_text(encoding="utf-8"))
    check(referee.validate_contract(clauses) == [], "contract is valid")
    check({c_["ref"] for c_ in clauses} == {"customers.email", f"notify:{OWNER}"}, "contract refs")
    reset(base)
    before = referee.snapshot(base)
    check(not any(x["passed"] for x in referee.evaluate(clauses, base)), "red on the untouched system")
    c = login_client(base)
    c.post("/catalog/customers/tags", data={"table": "customers", "column": "email", "tag": "PII"})
    check(not all(x["passed"] for x in referee.evaluate(clauses, base)), "tag alone is not enough (owner not told)")
    c.post("/notify", data={"to": OWNER, "subject": "customers.email is now tagged PII",
                            "body": "Hi Priya, the email column of customers is now classified as PII."})
    results = referee.evaluate(clauses, base)
    unexpected = referee.unexpected_changes(referee.diff(before, referee.snapshot(base)), clauses)
    check(all(x["passed"] for x in results) and unexpected == [], "green once the task is done by hand")
    check(referee.verdict(results, [], unexpected)["status"] == "VERIFIED", "referee verdict VERIFIED")
    reset(base)


def browser_checks(base: str) -> None:
    """The agent's own tools (browser.py + agent.propose) on the tag form, in a real browser, no LLM."""
    print("[h] agent tools in a real browser (renamed_button on)")
    import agent  # noqa: F401  (registers the agent tools: propose)
    import browser
    import gateway
    reset(base, {"renamed_button": True})
    b = browser.Browser(base)
    b.start()
    try:
        ctx = SimpleNamespace(run_id="cat-browser", browser=b, notes={}, parked_logged=set(),
                              log=lambda *_, **__: None)
        gateway.dispatch(ctx, "goto", {"path": "/login"})
        gateway.dispatch(ctx, "fill_form", {"fields": {"Username": "clerk", "Password": "receipts123"}})
        gateway.dispatch(ctx, "click", {"target": "Sign in"})
        out = gateway.dispatch(ctx, "click", {"target": "Catalog"})
        check("customers" in out and OWNER in out, "the agent can find the catalog and the owner from the nav")
        out = gateway.dispatch(ctx, "goto", {"path": "/catalog/customers"})
        check("FORM record=column_tag action=/catalog/customers/tags" in out and "Column [column]" in out
              and "Tag [tag]" in out and "[table]" not in out, "snapshot shows the record form (hidden table not listed)")
        out = gateway.dispatch(ctx, "propose", {"kind": "column_tag", "fields": {"table": "customers", "column": "email",
                                                                               "tag": "PII"}})
        check(out.startswith("ERROR") and "column, tag" in out, "agent propose only accepts the visible fields")
        out = gateway.dispatch(ctx, "propose", {"kind": "column_tag", "fields": {"column": "email", "tag": "PII"}})
        check(out.startswith("ALLOWED") and "customers.email" in out, f"agent propose ALLOWED: {out.splitlines()[0]}")
        out = gateway.dispatch(ctx, "fill_form", {"fields": {"Column": "email", "Tag": "PII"}})
        check(out.startswith("OK: filled 2 fields"), "fill_form fills the Column select and the Tag input")
        gateway.dispatch(ctx, "click", {"target": "More actions"})
        out = gateway.dispatch(ctx, "click", {"target": "Submit for posting"})
        check("VERIFIED: GET /api/catalog/columns?table=customers&column=email -> tags PII" in out
              and "Tagged customers.email as PII" in out, "guarded submit lands and is VERIFIED")
        gateway.dispatch(ctx, "fill_form", {"fields": {"Column": "email", "Tag": "PII"}})
        gateway.dispatch(ctx, "click", {"target": "More actions"})
        out = gateway.dispatch(ctx, "click", {"target": "Submit for posting"})
        check(out.startswith("BLOCKED") and "duplicate-submit" in out, "a repeat submit is blocked as a duplicate")
    finally:
        b.close()
    reset(base)


def default() -> None:
    env = isolated_env()
    with company_app(env=env) as base:
        app_checks(base)
        gateway_checks(base, env)
        browser_checks(base)
    print("CATALOG VERIFIED", flush=True)


# ---------------------------------------------------------------- regressions

def regressions() -> None:
    for args, token in REGRESSIONS:
        proc = subprocess.run([PY, str(ROOT / "checks" / args[0]), *args[1:]], cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=900)
        lines = (proc.stdout or "").strip().splitlines()
        if proc.returncode != 0 or not lines or lines[-1].strip() != token:
            print("\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:]))
        check(proc.returncode == 0 and lines and lines[-1].strip() == token,
              f"{' '.join(args)} exits 0 and prints {token} last")
    print("CATALOG REGRESSIONS VERIFIED", flush=True)


if __name__ == "__main__":
    modes = {"default": default, "regressions": regressions}
    mode = sys.argv[1] if len(sys.argv) > 1 else "default"
    if mode not in modes:
        fail("usage: check_catalog.py [regressions]")
    modes[mode]()
