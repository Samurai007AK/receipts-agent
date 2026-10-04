"""Receipts gateway: the only door between the agent and the world.

A generic engine with no domain knowledge: a tool registry, `dispatch` (scope check,
call, ledger), and the propose -> guard_submit -> confirm / decide lifecycle backed by
SQLite (WAL, safe for the agent and the console at once). Every domain rule lives in
policy.py. `python gateway.py` runs an assert-based self-check against RECEIPTS_APP_URL
(the stub or a freshly reset real app) on a temporary database.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

import policy

VERDICTS = ("ALLOWED", "SKIPPED", "BLOCKED", "PARKED", "APPROVED", "REJECTED", "SUBMITTED", "DONE")
TIERS = ("read", "write", "irreversible", "control")
TOOLS: dict[str, dict] = {}
SUBMITTABLE = ("ALLOWED", "APPROVED", "SUBMITTED")
HUMAN_KEPT = ("APPROVED", "REJECTED", "SUBMITTED")  # with decided_by set: a human decision re-proposing must not undo
SEVERITY = {"BLOCKED": 2, "PARKED": 1}
DETAIL_MAX = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, tool TEXT, tier TEXT,
    args TEXT, result_status TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS actions_run ON actions(run_id);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY, run_id TEXT, kind TEXT, fields TEXT, source TEXT, ref TEXT,
    status TEXT, rule_id TEXT, reason TEXT, created REAL, updated REAL, decided_by TEXT);
CREATE INDEX IF NOT EXISTS proposals_run ON proposals(run_id);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT, ts REAL, event TEXT);
"""
_READY: set[str] = set()


def tool(name: str, tier: str) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Decorator: register fn(ctx, **args) -> str as an agent tool."""
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {TIERS}, got {tier!r}")

    def register(fn: Callable[..., str]) -> Callable[..., str]:
        TOOLS[name] = {"fn": fn, "tier": tier}
        return fn
    return register


def norm(value: Any) -> str:
    """Canonical form of a field value (CONTRACT 2.4)."""
    # CONTRACT: what counts as "the same money/date" is domain knowledge, so it is implemented in policy.norm.
    return policy.norm(value)


# ---------------------------------------------------------------- storage

@contextlib.contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """One short-lived connection per call: safe across threads (console) and processes (agent)."""
    path = Path(os.environ.get("RECEIPTS_DB", "receipts.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    if not path.exists():
        _READY.discard(key)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)  # timeout = busy wait for the other process
    con.row_factory = sqlite3.Row
    try:
        if key not in _READY:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(_SCHEMA)
            _READY.add(key)
        yield con
    finally:
        con.close()


@contextlib.contextmanager
def _tx() -> Iterator[sqlite3.Connection]:
    """Write transaction; takes the write lock up front so read-then-write never deadlocks."""
    with _db() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")


def _row_to_proposal(row: sqlite3.Row) -> dict:
    p = dict(row)
    p["fields"] = json.loads(p["fields"] or "{}")
    return p


def _load(con: sqlite3.Connection, pid: str) -> dict | None:
    row = con.execute("SELECT * FROM proposals WHERE proposal_id=?", (pid,)).fetchone()
    return _row_to_proposal(row) if row else None


def _journal(con: sqlite3.Connection, pid: str, event: str) -> None:
    con.execute("INSERT INTO journal (proposal_id, ts, event) VALUES (?,?,?)", (pid, time.time(), event))


def _action(con: sqlite3.Connection, run_id: str, name: str, tier: str, args: Any, status: str, detail: str) -> None:
    con.execute("INSERT INTO actions (run_id, ts, tool, tier, args, result_status, detail) VALUES (?,?,?,?,?,?,?)",
                (run_id, time.time(), name, tier, json.dumps(args, default=str, ensure_ascii=False),
                 status, detail[:DETAIL_MAX]))


def _set_status(con: sqlite3.Connection, pid: str, status: str, event: str) -> None:
    con.execute("UPDATE proposals SET status=?, updated=? WHERE proposal_id=?", (status, time.time(), pid))
    _journal(con, pid, event)


# ---------------------------------------------------------------- dispatch

def _status_word(result: str) -> str:
    words = result.split(None, 1)
    return words[0].rstrip(":") if words else ""


def _off_limits(args: dict) -> str | None:
    for key in ("path", "url"):
        value = args.get(key)
        if isinstance(value, str) and not policy.path_allowed(value):
            return value
    return None


def dispatch(ctx: Any, name: str, args: dict) -> str:
    """Run one agent tool call through scope checks and the ledger. Never raises."""
    run_id = str(getattr(ctx, "run_id", "") or "")
    args = {} if args is None else args
    entry = TOOLS.get(name)
    tier = entry["tier"] if entry else ""
    by_gateway = True
    try:
        if not isinstance(args, dict):
            result = f"ERROR: arguments for {name} must be a JSON object, got {type(args).__name__}"
        elif entry is None:
            result = f"ERROR: unknown tool {name} (available: {', '.join(sorted(TOOLS)) or 'none'})"
        elif (bad := _off_limits(args)) is not None:
            result = f"BLOCKED: path {bad} is off-limits (rule out-of-scope)"
        else:
            by_gateway = False
            out = entry["fn"](ctx, **args)
            result = "OK" if out is None else str(out)
    except Exception as e:  # a tool bug must come back to the LLM as text, not kill the run
        result, by_gateway = f"ERROR: {type(e).__name__}: {e}", True
    status = _status_word(result)
    try:
        with _tx() as con:
            _action(con, run_id, name, tier, args, status, result)
    except Exception as e:  # the ledger failing must not take the agent down; say so loudly
        print(f"gateway: could not record action {name}: {type(e).__name__}: {e}", file=sys.stderr)
    log = getattr(ctx, "log", None)
    if by_gateway and callable(log):
        # CONTRACT: the gateway logs only outcomes it produced itself (unknown tool, out-of-scope,
        # tool exception) as a "gateway" event; normal tool results are the agent's to log.
        try:
            log("gateway", tool=name, tier=tier, status=status, detail=result[:300])
        except Exception:
            pass
    return result


# ---------------------------------------------------------------- proposals

def make_proposal_id(kind: str, fields: dict) -> str:
    """Idempotency key: same intent -> same id, across runs and processes."""
    canonical = json.dumps({k: norm(v) for k, v in fields.items()}, sort_keys=True)
    return "P-" + hashlib.sha256(f"{kind}|{canonical}".encode()).hexdigest()[:10]


def _ref(kind: str, fields: dict) -> str:
    fn = policy.REF.get(kind)
    return fn(fields) if fn else kind


def _source_ctx(source: str | None) -> dict:
    ctx = {"source": source, "source_text": "", "source_from": "", "app_url": policy.app_url()}
    m = re.fullmatch(r"\s*(?:inbox\s*:\s*)?(\d+)\s*", str(source or ""), re.I)
    if m:
        r = httpx.get(f"{ctx['app_url']}/api/inbox/{m.group(1)}", timeout=policy.HTTP_TIMEOUT)
        if r.status_code != 404:
            r.raise_for_status()
            mail = r.json()
            # CONTRACT: source_text is the email's subject and body; source_from its From address.
            ctx["source_text"] = f"{mail.get('subject', '')}\n{mail.get('body', '')}"
            ctx["source_from"] = mail.get("from", "")
    return ctx


def _judge(kind: str, fields: dict, source: str | None) -> tuple[str, str | None, str]:
    rules = policy.RULES.get(kind, [])
    if not rules:
        return "ALLOWED", None, f"no policy rules for kind {kind}"
    ctx = _source_ctx(source)
    hits = []
    for rule in rules:
        try:
            hit = rule(fields, ctx)
        except Exception as e:  # fail closed, with the cause
            hit = ("BLOCKED", "policy-unavailable",
                   f"policy rule {getattr(rule, '__name__', rule)} could not run ({type(e).__name__}: {e}); "
                   "the company app may be down, try again shortly")
        if hit:
            hits.append(hit)
    if not hits:
        return "ALLOWED", None, f"passed all {len(rules)} policy checks"
    worst = max(SEVERITY.get(h[0], 2) for h in hits)
    status, rule_id, _ = next(h for h in hits if SEVERITY.get(h[0], 2) == worst)
    return (status if status in SEVERITY else "BLOCKED"), rule_id, "; ".join(h[2] for h in hits)


def propose(run_id: str, kind: str, fields: dict, source: str | None = None) -> dict:
    """Judge an intended write before it happens; upsert and return the proposal dict."""
    kind = str(kind).strip().lower()
    fields = dict(fields or {})
    pid = make_proposal_id(kind, fields)
    ref = _ref(kind, fields)
    with _db() as con:
        stored = _load(con, pid)
    if source is None and stored:
        source = stored["source"]  # CONTRACT: same intent re-proposed without a source keeps the stored one
    force = False  # only "already in the system" may replace a human decision (see the upsert below)
    try:
        exists = kind in policy.EXISTS and policy.EXISTS[kind](fields)
        if exists and stored and stored["status"] in ("SUBMITTED", "DONE") and stored["run_id"] == run_id:
            # CONTRACT: this run submitted it and the record exists -> DONE, not SKIPPED (other runs see SKIPPED).
            verdict, force = ("DONE", stored["rule_id"], stored["reason"]), True
        elif exists:
            verdict, force = ("SKIPPED", "already-in-system", f"{ref} is already in the system of record; nothing to do"), True
        elif stored and stored["decided_by"] and stored["status"] in HUMAN_KEPT:
            verdict = stored["status"], stored["rule_id"], stored["reason"]
        else:
            verdict = _judge(kind, fields, source)
    except Exception as e:
        verdict = ("BLOCKED", "policy-unavailable",
                   f"policy check could not run ({type(e).__name__}: {e}); the company app may be down, try again shortly")
    status, rule_id, reason = verdict
    now = time.time()
    # CONTRACT: re-proposing moves the proposal to the current run (so its guard_submit finds it)
    # while a human decision (APPROVED/REJECTED and what followed) is kept unchanged.
    with _tx() as con:
        con.execute(
            """INSERT INTO proposals (proposal_id, run_id, kind, fields, source, ref, status, rule_id, reason,
                                      created, updated, decided_by)
               VALUES (:pid, :run_id, :kind, :fields, :source, :ref, :status, :rule_id, :reason, :now, :now, NULL)
               ON CONFLICT(proposal_id) DO UPDATE SET
                 run_id=excluded.run_id, fields=excluded.fields, ref=excluded.ref, updated=excluded.updated,
                 source=COALESCE(excluded.source, proposals.source),
                 status=CASE WHEN :force OR NOT {kept} THEN excluded.status ELSE proposals.status END,
                 rule_id=CASE WHEN :force OR NOT {kept} THEN excluded.rule_id ELSE proposals.rule_id END,
                 reason=CASE WHEN :force OR NOT {kept} THEN excluded.reason ELSE proposals.reason END"""
            .format(kept=f"(proposals.decided_by IS NOT NULL AND proposals.status IN {HUMAN_KEPT})"),
            {"pid": pid, "run_id": run_id, "kind": kind, "fields": json.dumps(fields, default=str), "source": source,
             "ref": ref, "status": status, "rule_id": rule_id, "reason": reason, "now": now, "force": force})
        p = _load(con, pid)
        _journal(con, pid, f"propose by {run_id} -> {p['status']}")
        _action(con, run_id, "gateway.propose", "control", {"kind": kind, "fields": fields, "source": source},
                p["status"], f"{pid} {p['rule_id'] or ''}: {p['reason'] or ''}")
    return {"proposal_id": pid, "status": p["status"], "rule_id": p["rule_id"], "reason": p["reason"], "ref": p["ref"]}


def _form_diff(pfields: dict, form: dict[str, tuple[Any, bool]]) -> list[str]:
    diffs = []
    for k, v in pfields.items():
        if k not in form:
            if norm(v):
                diffs.append(f"{k}: proposal has {v!r} but the form has no field {k!r}")
        elif norm(form[k][0]) != norm(v):
            diffs.append(f"{k}: form has {form[k][0]!r}, proposal has {v!r}")
    for k, (value, hidden) in form.items():
        if k not in pfields and not hidden and norm(value):
            diffs.append(f"{k}: form has {value!r} but it is not in the proposal")
    return diffs


def _guard_result(allowed: bool, pid: str | None, status: str, rule_id: str | None, reason: str) -> dict:
    return {"allowed": allowed, "proposal_id": pid, "status": status, "rule_id": rule_id, "reason": reason}


def _not_submittable(p: dict) -> dict:
    pid, status = p["proposal_id"], p["status"]
    # CONTRACT: a form that exactly matches a proposal which may not be submitted says why,
    # instead of a generic no-matching-proposal.
    if status == "DONE":
        return _guard_result(False, pid, "BLOCKED", "duplicate-submit",
                             f"{p['ref']} was already recorded (proposal {pid} is DONE); do not submit it again")
    if status == "PARKED":
        why = f"proposal {pid} is waiting for a human decision: {p['reason']}"
    elif status == "REJECTED":
        why = f"proposal {pid} was rejected by {p['decided_by'] or 'a human'}: {p['reason']}"
    else:
        why = f"proposal {pid} was {status}: {p['reason']}"
    return _guard_result(False, pid, "BLOCKED", p["rule_id"] or status.lower(), why)


def guard_submit(run_id: str, kind: str, form_fields: dict) -> dict:
    """Allow a record form submit only if it matches this run's ALLOWED/APPROVED/SUBMITTED proposal."""
    kind = str(kind).strip().lower()
    form = {name: (f.get("value"), bool(f.get("hidden"))) if isinstance(f, dict) else (f, False)
            for name, f in (form_fields or {}).items()}
    mine = [p for p in proposals(run_id) if p["kind"] == kind]
    exact = [p for p in mine if not _form_diff(p["fields"], form)]
    match = next((p for p in exact if p["status"] in SUBMITTABLE), None)

    if match is None and exact:
        result = _not_submittable(exact[-1])
    elif match is None:
        pool = [p for p in mine if p["status"] in SUBMITTABLE] or mine
        if not pool:
            reason = f"no proposal for kind {kind} in run {run_id}; call propose with these exact values first"
        else:
            best = min(pool, key=lambda p: len(_form_diff(p["fields"], form)))
            reason = (f"form does not match proposal {best['proposal_id']} ({best['status']}): "
                      + "; ".join(_form_diff(best["fields"], form)))
        result = _guard_result(False, None, "BLOCKED", "no-matching-proposal", reason)
    else:
        pid = match["proposal_id"]
        try:
            exists = kind in policy.EXISTS and policy.EXISTS[kind](match["fields"])
        except Exception as e:
            exists = None
            result = _guard_result(False, pid, "BLOCKED", "policy-unavailable",
                                   f"could not check for an existing record ({type(e).__name__}: {e}); try again shortly")
        if exists:
            with _tx() as con:
                _set_status(con, pid, "DONE", "duplicate-submit blocked: record already exists")
            result = _guard_result(False, pid, "BLOCKED", "duplicate-submit",
                                   f"{match['ref']} is already in the system of record; submitting again would duplicate it")
        elif exists is False:
            with _tx() as con:
                _set_status(con, pid, "SUBMITTED", "submit")
            result = _guard_result(True, pid, "SUBMITTED", None, f"form matches proposal {pid}")
    with _tx() as con:
        _action(con, run_id, "gateway.guard_submit", "control", {"kind": kind, "form": form_fields},
                result["status"], f"{result['proposal_id'] or ''} {result['rule_id'] or ''}: {result['reason']}")
    return result


def confirm(proposal_id: str) -> dict:
    """Check the system of record; DONE with evidence if the write landed, else UNCONFIRMED."""
    with _db() as con:
        p = _load(con, proposal_id)
    if p is None:
        return {"status": "UNCONFIRMED", "evidence": f"unknown proposal {proposal_id}"}
    kind, fields = p["kind"], p["fields"]
    if kind not in policy.EXISTS:
        return {"status": "UNCONFIRMED", "evidence": f"no existence check for kind {kind}"}
    try:
        exists = policy.EXISTS[kind](fields)
        evidence = policy.EVIDENCE[kind](fields) if kind in policy.EVIDENCE else f"{p['ref']} exists: {exists}"
    except Exception as e:
        return {"status": "UNCONFIRMED", "evidence": f"could not check ({type(e).__name__}: {e})"}
    if not exists:
        return {"status": "UNCONFIRMED", "evidence": evidence}
    with _tx() as con:
        _set_status(con, proposal_id, "DONE", f"confirmed: {evidence}")
    return {"status": "DONE", "evidence": evidence}


def decide(proposal_id: str, decision: str, by: str = "human", always: bool = False) -> dict:
    """Human decision on a PARKED proposal. Raises ValueError for an unknown id, bad decision or non-PARKED proposal."""
    decision = str(decision).strip().lower()
    if decision not in ("approve", "reject"):
        raise ValueError(f"decision must be 'approve' or 'reject', got {decision!r}")
    new = "APPROVED" if decision == "approve" else "REJECTED"
    with _db() as con:
        p = _load(con, proposal_id)
    if p is None:
        raise ValueError(f"unknown proposal {proposal_id}")
    if p["status"] != "PARKED":
        # CONTRACT: deciding an already-decided proposal again is a no-op (double clicks); others are errors.
        if p["decided_by"]:
            return p
        raise ValueError(f"proposal {proposal_id} is {p['status']}; only PARKED proposals can be decided")
    if always and decision == "approve":
        match, limit = policy.override_for(p["kind"], p["rule_id"], p["fields"])
        policy.add_override(p["kind"], p["rule_id"], match, limit, by)
    with _tx() as con:
        cur = con.execute("UPDATE proposals SET status=?, decided_by=?, updated=? WHERE proposal_id=? AND status='PARKED'",
                          (new, by, time.time(), proposal_id))
        if cur.rowcount:
            _journal(con, proposal_id, f"{decision} by {by}" + (" (always allow)" if always and decision == "approve" else ""))
            _action(con, p["run_id"], "gateway.decide", "control",
                    {"proposal_id": proposal_id, "decision": decision, "by": by, "always": always}, new, p["reason"] or "")
        return _load(con, proposal_id)


def pending(run_id: str | None = None) -> list[dict]:
    """PARKED proposals, oldest first."""
    return [p for p in proposals(run_id) if p["status"] == "PARKED"]


def proposals(run_id: str | None = None) -> list[dict]:
    """All proposals (optionally of one run), oldest first, fields decoded."""
    where, params = ("WHERE run_id=?", (run_id,)) if run_id is not None else ("", ())
    with _db() as con:
        rows = con.execute(f"SELECT * FROM proposals {where} ORDER BY created, proposal_id", params).fetchall()
    return [_row_to_proposal(r) for r in rows]


def actions(run_id: str) -> list[dict]:
    """The run's action ledger in order, args decoded."""
    with _db() as con:
        rows = con.execute("SELECT * FROM actions WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    return [{**dict(r), "args": json.loads(r["args"] or "null")} for r in rows]


def reset_db() -> None:
    """Delete every row in every table."""
    with _tx() as con:
        for table in ("actions", "proposals", "journal"):
            con.execute(f"DELETE FROM {table}")


# ---------------------------------------------------------------- self-check

def _selftest() -> None:
    import tempfile
    from types import SimpleNamespace

    tmp = Path(tempfile.mkdtemp(prefix="gateway-selftest-"))
    os.environ["RECEIPTS_DB"] = str(tmp / "receipts.db")
    os.environ["RECEIPTS_OVERRIDES"] = str(tmp / "overrides.json")
    base = policy.app_url()
    assert httpx.get(f"{base}/api/health", timeout=5).json().get("ok"), f"app at {base} is not healthy"

    assert norm(" Acme   Corp ") == "acme corp"
    assert norm("$1,240.00") == norm("USD 1240") == norm(1240) == "1240.00"
    assert norm("2 November 2026") == norm("November 2, 2026") == norm("02/11/2026") == "2026-11-02"
    assert norm("31/02/2026") == "31/02/2026" and norm("INV-1042") == "inv-1042" and norm(None) == ""

    clean = {"vendor": "Acme Corp", "invoice_no": "INV-1042", "amount": "1240.00", "due_date": "2026-11-02"}
    a = propose("st-1", "invoice", clean, "inbox:3")
    b = propose("st-1", "invoice", {**clean, "amount": "$1,240.00", "due_date": "02/11/2026"}, "3")
    assert a["status"] == "ALLOWED" and a["proposal_id"] == b["proposal_id"] and a["ref"] == "INV-1042", (a, b)
    bad = propose("st-1", "invoice", {**clean, "amount": "1420"}, "inbox:3")
    assert bad["status"] == "BLOCKED" and bad["rule_id"] == "not-in-source" and "1420.00" in bad["reason"], bad
    assert propose("st-1", "invoice", {**clean, "invoice_no": "INV-9"})["rule_id"] == "not-in-source"
    assert propose("st-1", "invoice", clean)["status"] == "ALLOWED"  # re-propose keeps the stored source
    zorg = propose("st-1", "invoice", {"vendor": "Zorg Ltd", "invoice_no": "ZRG-1", "amount": 9000,
                                       "due_date": "2026-10-10"}, "inbox:3")
    assert zorg["status"] == "BLOCKED" and "not on the approved vendor list" in zorg["reason"], zorg
    seen = propose("st-1", "invoice", {"vendor": "Acme Corp", "invoice_no": "INV-0988", "amount": 980,
                                       "due_date": "2026-09-30"}, "inbox:1")
    assert seen["status"] == "SKIPPED" and seen["rule_id"] == "already-in-system", seen

    note = {"to": "someone@gmail.com", "subject": "INV-1042", "body": "Posted."}
    ext = propose("st-1", "notification", note)
    assert ext["status"] == "PARKED" and ext["rule_id"] == "external-recipient", ext
    assert [p["proposal_id"] for p in pending("st-1")] == [ext["proposal_id"]]
    assert decide(ext["proposal_id"], "reject")["status"] == "REJECTED"
    assert propose("st-1", "notification", note)["status"] == "REJECTED" and pending() == []

    form = {k: {"value": v, "hidden": False} for k, v in clean.items()} | {"next": {"value": "/", "hidden": True}}
    assert guard_submit("st-other", "invoice", form)["rule_id"] == "no-matching-proposal"
    off = guard_submit("st-1", "invoice", {**form, "amount": {"value": "1420.00", "hidden": False}})
    assert not off["allowed"] and "amount" in off["reason"], off
    ok = guard_submit("st-1", "invoice", {**form, "amount": {"value": "$1,240.00", "hidden": False}})
    assert ok["allowed"] and ok["status"] == "SUBMITTED" and ok["proposal_id"] == a["proposal_id"], ok
    assert confirm(a["proposal_id"])["status"] == "UNCONFIRMED"

    @tool("selftest_echo", "read")
    def _echo(ctx: Any, path: str = "/", text: str = "") -> str:
        return f"OK: {text}"

    @tool("selftest_boom", "write")
    def _boom(ctx: Any) -> str:
        raise RuntimeError("kaboom")

    ctx = SimpleNamespace(run_id="st-1")
    assert dispatch(ctx, "nope", {}).startswith("ERROR: unknown tool nope")
    assert dispatch(ctx, "selftest_boom", {}) == "ERROR: RuntimeError: kaboom"
    assert dispatch(ctx, "selftest_echo", {"path": "/chaos"}) == "BLOCKED: path /chaos is off-limits (rule out-of-scope)"
    assert dispatch(ctx, "selftest_echo", {"path": "/inbox", "text": "hi"}) == "OK: hi"
    logged = [(x["tool"], x["tier"], x["result_status"]) for x in actions("st-1") if not x["tool"].startswith("gateway.")]
    assert logged == [("nope", "", "ERROR"), ("selftest_boom", "write", "ERROR"),
                      ("selftest_echo", "read", "BLOCKED"), ("selftest_echo", "read", "OK")], logged

    reset_db()
    assert proposals() == [] and actions("st-1") == []
    print("GATEWAY SELFTEST OK")


if __name__ == "__main__":
    _selftest()
