"""Self-check for gateway.py + policy.py against the stub company app.

Usage: python checks/check_gateway.py rules|flow|selftest
Prints GATEWAY RULES VERIFIED / GATEWAY FLOW VERIFIED / GATEWAY SELFTEST VERIFIED last.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from checks._servers import PY, ROOT, check, fail, isolated_env, stub_app  # noqa: E402

CLEAN = {"vendor": "Acme Corp", "invoice_no": "INV-1042", "amount": "1240.00", "due_date": "2026-11-02"}
UMBRELLA = {"vendor": "Umbrella Corp", "invoice_no": "INV-UC-5521", "amount": "8200.00", "due_date": "2026-11-15"}


def _setup(base: str, env: dict[str, str]):
    os.environ.update(env)
    os.environ["RECEIPTS_APP_URL"] = base
    import gateway
    import policy
    return gateway, policy


def form(fields: dict, **hidden: str) -> dict:
    out = {k: {"value": str(v), "hidden": False} for k, v in fields.items()}
    out.update({k: {"value": v, "hidden": True} for k, v in hidden.items()})
    return out


def rules(base: str, env: dict[str, str]) -> None:
    gateway, policy = _setup(base, env)
    norm = gateway.norm
    cases = [(" Acme   Corp ", "acme corp"), ("$1,240.00", "1240.00"), ("USD 2,310.50", "2310.50"),
             ("1240", "1240.00"), (1240, "1240.00"), (2310.5, "2310.50"), ("-$450.00", "-450.00"),
             ("2026-11-02", "2026-11-02"), ("02/11/2026", "2026-11-02"), ("2 November 2026", "2026-11-02"),
             ("November 2, 2026", "2026-11-02"), ("2nd Nov 2026", "2026-11-02"), ("31/02/2026", "31/02/2026"),
             ("INV-1042", "inv-1042"), (None, ""), ("  Hello\n  World ", "hello world")]
    for raw, want in cases:
        check(norm(raw) == want, f"norm({raw!r}) == {want!r} (got {norm(raw)!r})")
    text = "Total due: $1,240.00 (or USD 2,310.50, 99.95) INV-1042 on 1 October 2026, due 2026-11-02 / 30/09/2026"
    check([str(m) for m in policy.money_in(text)] == ["1240.00", "2310.50", "99.95"], "money_in ignores ids, days, years")
    check(policy.dates_in(text) == ["2026-10-01", "2026-11-02", "2026-09-30"], "dates_in finds all three formats")

    a = gateway.propose("r1", "invoice", CLEAN, "inbox:3")
    check(a["status"] == "ALLOWED" and a["rule_id"] is None and a["ref"] == "INV-1042", f"clean INV-1042 ALLOWED: {a}")
    canonical = json.dumps({k: norm(v) for k, v in CLEAN.items()}, sort_keys=True)
    want_id = "P-" + hashlib.sha256(f"invoice|{canonical}".encode()).hexdigest()[:10]
    check(a["proposal_id"] == want_id, f"proposal_id follows CONTRACT 2.3 ({want_id})")
    b = gateway.propose("r2", "invoice", {"vendor": " acme corp", "invoice_no": "inv-1042", "amount": "$1,240.00",
                                          "due_date": "2 November 2026"}, "3")
    check(b["proposal_id"] == a["proposal_id"] and b["status"] == "ALLOWED", "same intent, other formatting/run -> same id")
    other = subprocess.run([PY, "-c", "import gateway, json, sys; "
                            f"print(gateway.propose('r3', 'invoice', {CLEAN!r}, 'inbox:3')['proposal_id'])"],
                           cwd=ROOT, env={**os.environ}, capture_output=True, text=True, timeout=60)
    check(other.stdout.strip() == a["proposal_id"], f"same id in another process: {other.stdout.strip()} {other.stderr[-300:]}")

    httpx.post(f"{base}/stub/invoices", json={**CLEAN, "amount": 1240.0}, timeout=5).raise_for_status()
    s = gateway.propose("r1", "invoice", CLEAN, "inbox:3")
    check(s["status"] == "SKIPPED" and s["rule_id"] == "already-in-system", f"after the row exists -> SKIPPED: {s}")

    u = gateway.propose("r1", "invoice", UMBRELLA, "inbox:7")
    check(u["status"] == "PARKED" and u["rule_id"] == "over-threshold" and "5000.00" in u["reason"], f"Umbrella 8200 PARKED: {u}")

    v = gateway.propose("r1", "invoice", {"vendor": "Vandelay Industries", "invoice_no": "INV-V-19", "amount": "640.00",
                                          "due_date": "2026-10-30"}, "inbox:8")
    check(v["status"] == "BLOCKED" and v["rule_id"] == "vendor-not-allowlisted", f"Vandelay BLOCKED: {v}")
    check("Vandelay Industries" in v["reason"] and "closest allowed vendor" in v["reason"], "vendor reason names the closest vendor")

    z = gateway.propose("r1", "invoice", {"vendor": "Zorg Ltd", "invoice_no": "ZRG-1", "amount": "$9,000.00",
                                          "due_date": "2026-10-10"}, "inbox:3")
    check(z["status"] == "BLOCKED" and z["rule_id"] == "vendor-not-allowlisted", f"Zorg BLOCKED: {z}")
    check("not on the approved vendor list" in z["reason"] and "sent from acme.example" in z["reason"]
          and "belongs to Acme Corp" in z["reason"], "Zorg reason has both vendor and sender-mismatch parts")
    g = gateway.propose("r1", "invoice", {"vendor": "Globex Inc", "invoice_no": "INV-1042", "amount": "1240.00",
                                          "due_date": "2026-11-02"}, "inbox:3")
    check(g["rule_id"] == "sender-mismatch" and "acme.example" in g["reason"] and "globex.example" in g["reason"],
          f"sender-mismatch names both domains: {g}")

    c = gateway.propose("r1", "invoice", {"vendor": "Initech LLC", "invoice_no": "INV-3307", "amount": "450",
                                          "due_date": "2026-10-31"}, "inbox:6")
    check(c["status"] == "PARKED" and c["rule_id"] == "credit-note" and "CREDIT MEMO" in c["reason"], f"Initech credit memo PARKED: {c}")

    x = gateway.propose("r1", "invoice", {**CLEAN, "invoice_no": "INV-1043", "amount": "1420"}, "inbox:3")
    check(x["status"] == "BLOCKED" and x["rule_id"] == "not-in-source", f"1420 BLOCKED not-in-source: {x}")
    check("amount 1420.00 not found in source email inbox:3 (found: 1240.00" in x["reason"]
          and "invoice_no INV-1043 not found" in x["reason"], f"not-in-source reason is specific: {x['reason']}")
    d = gateway.propose("r1", "invoice", {**CLEAN, "invoice_no": "INV-1043", "due_date": "2026-11-03"}, "inbox:3")
    check("due_date 2026-11-03 not found" in d["reason"] and "2026-11-02" in d["reason"], "due_date reason lists found dates")
    n = gateway.propose("r1", "invoice", CLEAN | {"invoice_no": "INV-9"}, None)
    check(n["rule_id"] == "not-in-source" and "no source email" in n["reason"], "missing source BLOCKED")
    m = gateway.propose("r1", "invoice", CLEAN | {"invoice_no": "INV-9"}, "inbox:99")
    check(m["rule_id"] == "not-in-source" and "inbox:99" in m["reason"], "unknown source BLOCKED")

    e = gateway.propose("r1", "notification", {"to": "someone@gmail.com", "subject": "Done", "body": "INV-1042 posted"})
    check(e["status"] == "PARKED" and e["rule_id"] == "external-recipient" and "gmail.com" in e["reason"],
          f"external recipient PARKED: {e}")
    check(e["ref"] == "notify:someone@gmail.com", "notification ref")
    i = gateway.propose("r1", "notification", {"to": "alex@company.test", "subject": "Done", "body": "ok"})
    check(i["status"] == "ALLOWED", "internal recipient ALLOWED")
    mix = gateway.propose("r1", "notification", {"to": "alex@company.test, x@gmail.com", "subject": "s", "body": "b"})
    check(mix["status"] == "PARKED" and "gmail.com" in mix["reason"], "an external address among several is PARKED")
    k = gateway.propose("r1", "memo", {"text": "hi"})
    check(k["status"] == "ALLOWED" and k["ref"] == "memo", "kind without rules ALLOWED, ref = kind")

    os.environ["RECEIPTS_APP_URL"] = "http://127.0.0.1:9"  # nothing listens: policy must fail closed
    try:
        down = gateway.propose("r1", "invoice", CLEAN | {"invoice_no": "INV-2000"}, "inbox:3")
    finally:
        os.environ["RECEIPTS_APP_URL"] = base
    check(down["status"] == "BLOCKED" and down["rule_id"] == "policy-unavailable", f"app down -> policy-unavailable: {down}")
    print("GATEWAY RULES VERIFIED")


def flow(base: str, env: dict[str, str]) -> None:
    gateway, policy = _setup(base, env)
    run = "flow-1"
    g = gateway.guard_submit(run, "invoice", form(CLEAN))
    check(not g["allowed"] and g["status"] == "BLOCKED" and g["rule_id"] == "no-matching-proposal"
          and "no proposal" in g["reason"] and g["proposal_id"] is None, f"no proposal -> blocked: {g}")

    a = gateway.propose(run, "invoice", CLEAN, "inbox:3")
    pid = a["proposal_id"]
    g = gateway.guard_submit(run, "invoice", form(CLEAN | {"amount": "1420.00"}))
    check(not g["allowed"] and g["rule_id"] == "no-matching-proposal" and "amount" in g["reason"]
          and "due_date" not in g["reason"], f"mismatching amount blocked, reason names it: {g['reason']}")
    g = gateway.guard_submit(run, "invoice", form(CLEAN | {"memo": "extra"}))
    check(not g["allowed"] and "memo" in g["reason"], "extra visible non-empty field blocks")
    g = gateway.guard_submit("other-run", "invoice", form(CLEAN))
    check(not g["allowed"], "another run's proposal does not authorize this run")
    g = gateway.guard_submit(run, "invoice", form(CLEAN | {"amount": "$1,240.00", "due_date": "02/11/2026", "memo": ""},
                                                  csrf="tok123", next="/"))
    check(g["allowed"] and g["status"] == "SUBMITTED" and g["proposal_id"] == pid,
          f"matching form allowed (formatting, hidden and empty extras ignored): {g}")
    check(gateway.proposals(run)[0]["status"] == "SUBMITTED", "proposal now SUBMITTED")
    g = gateway.guard_submit(run, "invoice", form(CLEAN))
    check(g["allowed"], "SUBMITTED proposal may be re-submitted (retry after a 500)")
    cf = gateway.confirm(pid)
    check(cf["status"] == "UNCONFIRMED" and "0 rows" in cf["evidence"], f"confirm before the write: {cf}")
    httpx.post(f"{base}/stub/invoices", json={**CLEAN, "amount": 1240.0}, timeout=5).raise_for_status()
    g = gateway.guard_submit(run, "invoice", form(CLEAN))
    check(not g["allowed"] and g["rule_id"] == "duplicate-submit" and g["proposal_id"] == pid, f"duplicate submit blocked: {g}")
    check(gateway.proposals(run)[0]["status"] == "DONE", "duplicate submit marks the proposal DONE")
    cf = gateway.confirm(pid)
    check(cf["status"] == "DONE" and "GET /api/ledger?invoice_no=INV-1042 -> 1 row" in cf["evidence"], f"confirm DONE: {cf}")
    check(gateway.propose(run, "invoice", CLEAN, "inbox:3")["status"] == "DONE", "re-propose in the same run keeps DONE")
    check(gateway.propose("flow-2", "invoice", CLEAN, "inbox:3")["status"] == "SKIPPED", "a later run sees it SKIPPED")

    note = {"to": "alex@company.test", "subject": "INV-1042 posted", "body": "Acme INV-1042 for 1240.00 is in."}
    nid = gateway.propose(run, "notification", note)["proposal_id"]
    check(gateway.confirm(nid)["status"] == "UNCONFIRMED", "notification confirm before send")
    check(gateway.guard_submit(run, "notification", form(note))["allowed"], "matching notification form allowed")
    httpx.post(f"{base}/stub/outbox", json=note, timeout=5).raise_for_status()
    check(gateway.propose(run, "notification", note)["status"] == "DONE", "same run re-proposing a submitted, landed write -> DONE")
    cf = gateway.confirm(nid)
    check(cf["status"] == "DONE" and "/api/outbox?to=alex@company.test -> 1 message" in cf["evidence"], f"notification DONE: {cf}")
    check(gateway.confirm("P-missing")["status"] == "UNCONFIRMED", "unknown proposal UNCONFIRMED")

    u = gateway.propose(run, "invoice", UMBRELLA, "inbox:7")
    check([p["proposal_id"] for p in gateway.pending(run)] == [u["proposal_id"]], "pending lists the PARKED proposal")
    blocked = gateway.guard_submit(run, "invoice", form(UMBRELLA))
    check(not blocked["allowed"] and blocked["rule_id"] == "over-threshold", f"PARKED form submit blocked: {blocked}")
    d = gateway.decide(u["proposal_id"], "approve", by="tester", always=True)
    check(d["status"] == "APPROVED" and d["decided_by"] == "tester" and isinstance(d["fields"], dict), f"decide approve: {d}")
    check(gateway.decide(u["proposal_id"], "approve")["status"] == "APPROVED", "second approve is a no-op")
    ov = policy.load_overrides()
    check(len(ov) == 1 and ov[0]["kind"] == "invoice" and ov[0]["rule_id"] == "over-threshold"
          and ov[0]["match"] == {"vendor": "Umbrella Corp"} and ov[0]["limit"] == 8200.0 and ov[0]["by"] == "tester"
          and ov[0]["ts"], f"always-allow wrote an override: {ov}")
    check(gateway.propose("flow-3", "invoice", UMBRELLA, "inbox:7")["status"] == "APPROVED", "approved proposal returned unchanged")
    check(gateway.guard_submit("flow-3", "invoice", form(UMBRELLA))["allowed"], "APPROVED proposal may be submitted")
    os.environ["RECEIPTS_APP_URL"] = "http://127.0.0.1:9"
    try:
        down = gateway.propose("flow-3", "invoice", UMBRELLA, "inbox:7")
    finally:
        os.environ["RECEIPTS_APP_URL"] = base
    check(down["status"] == "SUBMITTED", f"app outage while re-proposing does not undo the approval: {down}")
    nxt = gateway.propose(run, "invoice", UMBRELLA | {"po": "PO-7"}, "inbox:7")
    check(nxt["status"] == "ALLOWED", f"next Umbrella proposal under the limit ALLOWED: {nxt}")
    hi = policy.over_threshold({"vendor": "Umbrella Corp", "amount": "9000"}, {})
    check(hi is not None and hi[0] == "PARKED", "above the override limit still PARKED")
    hi = policy.over_threshold({"vendor": "Globex Inc", "amount": "8200"}, {})
    check(hi is not None and hi[0] == "PARKED", "override is per vendor")

    e = gateway.propose(run, "notification", {"to": "someone@gmail.com", "subject": "Done", "body": "x"})
    r = gateway.decide(e["proposal_id"], "reject")
    check(r["status"] == "REJECTED" and r["decided_by"] == "human", "decide reject")
    check(gateway.propose(run, "notification", {"to": "someone@gmail.com", "subject": "Done", "body": "x"})["status"]
          == "REJECTED", "rejected proposal stays REJECTED")
    check(gateway.pending(run) == [] and gateway.pending() == [], "nothing pending after decisions")
    for bad, why in ((nid, "non-PARKED"), ("P-missing", "unknown id")):
        try:
            gateway.decide(bad, "approve")
            fail(f"decide on {why} proposal should raise")
        except ValueError:
            check(True, f"decide on {why} proposal raises ValueError")

    logs: list = []
    ctx = SimpleNamespace(run_id="flow-d", log=lambda type, **d: logs.append((type, d)))

    @gateway.tool("echo", "read")
    def echo(ctx, path: str = "/", text: str = "") -> str:
        return f"OK: {text} " + "x" * 5000

    @gateway.tool("boom", "irreversible")
    def boom(ctx) -> str:
        raise RuntimeError("kaboom")

    check(gateway.dispatch(ctx, "nope", {}).startswith("ERROR: unknown tool nope"), "unknown tool")
    check(gateway.dispatch(ctx, "boom", {}) == "ERROR: RuntimeError: kaboom", "raising tool -> ERROR text")
    check(gateway.dispatch(ctx, "echo", {"bogus": 1}).startswith("ERROR: TypeError"), "bad args -> ERROR text")
    check(gateway.dispatch(ctx, "echo", "notadict").startswith("ERROR:"), "non-dict args -> ERROR text")
    for p in ("/chaos", "/api/reset", "/API/Chaos/", "/inbox/../chaos", "//chaos?x=1", "/%63haos",
              "http://evil.example/inbox"):
        res = gateway.dispatch(ctx, "echo", {"path": p})
        check(res == f"BLOCKED: path {p} is off-limits (rule out-of-scope)", f"path {p} blocked")
    check(gateway.dispatch(ctx, "echo", {"url": "/api/chaos/log"}).startswith("BLOCKED"), "url arg checked too")
    check(gateway.dispatch(ctx, "echo", {"path": "/inbox/3", "text": "hi"}).startswith("OK: hi"), "allowed path runs")
    check(gateway.dispatch(SimpleNamespace(run_id="flow-d"), "echo", {"path": f"{base}/inbox"}).startswith("OK"),
          "same-host absolute URL allowed; ctx without log works")
    noisy = SimpleNamespace(run_id="flow-d", log=lambda *a, **k: 1 / 0)
    check(gateway.dispatch(noisy, "boom", {}).startswith("ERROR"), "a failing ctx.log does not escape")
    acts = gateway.actions("flow-d")
    first = acts[0]
    check((first["tool"], first["tier"], first["result_status"]) == ("nope", "", "ERROR"), "unknown tool recorded")
    ok = [x for x in acts if x["result_status"] == "OK"][0]
    check(ok["tool"] == "echo" and ok["tier"] == "read" and ok["args"] == {"path": "/inbox/3", "text": "hi"}
          and len(ok["detail"]) <= gateway.DETAIL_MAX, f"tool args/tier logged, detail truncated: {ok['args']}")
    check(sum(x["result_status"] == "BLOCKED" for x in acts) == 8, "every blocked call recorded")
    check(any(x["tool"] == "boom" and x["tier"] == "irreversible" for x in acts), "irreversible tier recorded")
    check(logs and all(t == "gateway" for t, _ in logs), f"ctx.log got gateway events: {len(logs)}")

    def hammer(n: int) -> None:
        c = SimpleNamespace(run_id="flow-c")
        for i in range(25):
            gateway.dispatch(c, "echo", {"text": f"{n}-{i}"})
    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    child = subprocess.run([PY, "-c", "import gateway, types; c = types.SimpleNamespace(run_id='flow-c'); "
                            "[gateway.dispatch(c, 'x', {}) for _ in range(50)]"],
                           cwd=ROOT, env={**os.environ}, capture_output=True, text=True, timeout=120)
    check(child.returncode == 0 and len(gateway.actions("flow-c")) == 250,
          f"concurrent threads + second process all recorded ({len(gateway.actions('flow-c'))}) {child.stderr[-300:]}")

    check({p["run_id"] for p in gateway.proposals()} == {run, "flow-2", "flow-3"}, "re-propose moved proposals to the new run")
    check(len(gateway.proposals()) == 5 and all(isinstance(p["fields"], dict) for p in gateway.proposals()),
          "proposals() lists all, fields decoded")
    keys = ["proposal_id", "run_id", "kind", "fields", "source", "ref", "status", "rule_id", "reason",
            "created", "updated", "decided_by"]
    check(list(gateway.proposals(run)[0]) == keys, "proposal dict keys per contract")
    check(len(gateway.actions(run)) > 0, "run actions present")
    gateway.reset_db()
    check(gateway.proposals() == [] and gateway.pending() == [] and gateway.actions(run) == []
          and gateway.actions("flow-d") == [], "reset_db empties every table")
    print("GATEWAY FLOW VERIFIED")


def selftest(base: str, env: dict[str, str]) -> None:
    r = subprocess.run([PY, "gateway.py"], cwd=ROOT, env={**os.environ, **env, "RECEIPTS_APP_URL": base},
                       capture_output=True, text=True, timeout=180)
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    check(r.returncode == 0, f"gateway.py exited {r.returncode}: {r.stderr[-1500:]}")
    check(lines and lines[-1] == "GATEWAY SELFTEST OK", f"last line GATEWAY SELFTEST OK: {lines[-1:]}")
    print("GATEWAY SELFTEST VERIFIED")


def main() -> None:
    modes = {"rules": rules, "flow": flow, "selftest": selftest}
    if len(sys.argv) != 2 or sys.argv[1] not in modes:
        fail(f"usage: check_gateway.py {'|'.join(modes)}")
    env = isolated_env()
    with stub_app() as base:
        modes[sys.argv[1]](base, env)


if __name__ == "__main__":
    main()
