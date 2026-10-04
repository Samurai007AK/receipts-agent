"""Referee: independently proves (or disproves) that a task is done.

Deliberately independent of the agent: it imports nothing from gateway, policy,
browser or agent, uses no LLM, and opens its own fresh HTTP client for every
evaluation. It re-queries the system of record and judges each contract clause
on what it observes there.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import httpx

ROOT = Path(__file__).resolve().parent
CLAUSE_KINDS = ("row_count", "http_field_equals", "http_field_contains", "outbox_contains", "file_contains")
REQUIRED = {
    "row_count": ("path", "equals"),
    "http_field_equals": ("path", "field", "equals"),
    "http_field_contains": ("path", "field", "contains"),
    "outbox_contains": ("to", "text"),
    "file_contains": ("path", "text"),
}
HTTP_KINDS = ("row_count", "http_field_equals", "http_field_contains")
EXCUSING = ("SKIPPED", "BLOCKED", "REJECTED", "PARKED")
EXCERPT = 300
TIMEOUT = 10.0

_MONEY = re.compile(r"^(-)?\s*(?:usd|us\$|\$)?\s*(-)?\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?\s*(?:usd)?$")
_NUMERIC_DATES = ("%Y-%m-%d", "%d/%m/%Y")
# Month names are matched by hand, not with strptime %B, so parsing never depends on the process locale.
_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"), 1)}
_MONTHS.update({name[:3]: i for name, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_D_MONTH_Y = re.compile(r"^(\d{1,2}) ([a-z]+)\.? (\d{4})$")
_MONTH_D_Y = re.compile(r"^([a-z]+)\.? (\d{1,2}),? (\d{4})$")
_MISSING = object()


# ---------------------------------------------------------------- normalization

def _norm(value: object) -> str:
    """Canonical comparison form (reimplemented locally for independence)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return "%.2f" % value
    text = " ".join(str(value).split()).lower()
    m = _MONEY.match(text)
    if m:
        sign = "-" if (m.group(1) or m.group(2)) else ""
        return "%.2f" % float(sign + m.group(3).replace(",", "") + (m.group(4) or ""))
    return _iso_date(text) or text


def _iso_date(text: str) -> str | None:
    for fmt in _NUMERIC_DATES:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    m = _D_MONTH_Y.match(text)
    if m:
        day, month, year = m.groups()
    elif m := _MONTH_D_Y.match(text):
        month, day, year = m.groups()
    else:
        return None
    if month not in _MONTHS:
        return None
    try:
        return date(int(year), _MONTHS[month], int(day)).isoformat()
    except ValueError:
        return None


def _text(value: object) -> str:
    """Loose text form for substring checks: whitespace collapsed, case folded."""
    return " ".join(str(value).split()).casefold()


def _excerpt(text: str) -> str:
    return text if len(text) <= EXCERPT else text[: EXCERPT - 3] + "..."


# ---------------------------------------------------------------- validation

def validate_contract(clauses: list[dict]) -> list[str]:
    """Return human-readable problems with a contract; [] means it is valid."""
    if not isinstance(clauses, list) or not clauses:
        return ["The contract must be a non-empty list of clauses."]
    errors: list[str] = []
    for i, clause in enumerate(clauses, 1):
        if not isinstance(clause, dict):
            errors.append(f"Clause {i}: must be an object, got {type(clause).__name__}.")
            continue
        kind = clause.get("kind")
        if kind not in CLAUSE_KINDS:
            errors.append(f"Clause {i}: unknown kind {kind!r}; expected one of {', '.join(CLAUSE_KINDS)}.")
            continue
        where = f"Clause {i} ({kind})"
        missing = [k for k in REQUIRED[kind] if k not in clause]
        if missing:
            errors.append(f"{where}: missing {', '.join(repr(k) for k in missing)}.")
            continue
        for key in ("ref", "description"):
            if key in clause and not isinstance(clause[key], str):
                errors.append(f"{where}: {key!r} must be text.")
        if kind in HTTP_KINDS:
            path = clause["path"]
            if not isinstance(path, str) or not path.startswith("/api/"):
                errors.append(f"{where}: 'path' must be text starting with /api/, got {path!r}.")
        if kind == "row_count":
            eq = clause["equals"]
            if isinstance(eq, bool) or not isinstance(eq, int) or eq < 0:
                errors.append(f"{where}: 'equals' must be a whole number of rows (0 or more), got {eq!r}.")
        if kind in ("http_field_equals", "http_field_contains"):
            field = clause["field"]
            if not isinstance(field, str) or not field.strip():
                errors.append(f"{where}: 'field' must be non-empty text.")
            key = "equals" if kind == "http_field_equals" else "contains"
            val = clause[key]
            if val is None or isinstance(val, (list, dict)) or (isinstance(val, str) and not val.strip()):
                errors.append(f"{where}: {key!r} must be a non-empty text or number value, got {val!r}.")
        if kind == "outbox_contains":
            for key in ("to", "text"):
                if not isinstance(clause[key], str) or not clause[key].strip():
                    errors.append(f"{where}: {key!r} must be non-empty text.")
            if isinstance(clause["to"], str) and clause["to"].strip() and "@" not in clause["to"]:
                errors.append(f"{where}: 'to' must be an email address, got {clause['to']!r}.")
        if kind == "file_contains":
            for key in ("path", "text"):
                if not isinstance(clause[key], str) or not clause[key].strip():
                    errors.append(f"{where}: {key!r} must be non-empty text.")
    return errors


# ---------------------------------------------------------------- plain English

_NOUNS = {  # api segment -> (singular, plural, heading)
    "ledger": ("ledger row", "ledger rows", "Ledger"),
    "outbox": ("outbox message", "outbox messages", "Outbox"),
    "inbox": ("inbox email", "inbox emails", "Inbox email"),
    "vendors": ("vendor", "vendors", "Vendors"),
    "snapshot": ("snapshot entry", "snapshot entries", "Snapshot"),
}


def _describe_path(path: str) -> tuple[str, str, str, str]:
    """-> (singular, plural, heading, subject) for an /api/ path."""
    parts = urlsplit(path)
    segs = [unquote(s) for s in parts.path.split("/") if s][1:]  # drop "api"
    base = segs[0] if segs else ""
    singular, plural, heading = _NOUNS.get(base, (f"row from {parts.path}", f"rows from {parts.path}", parts.path))
    query = dict(parse_qsl(parts.query))
    if "invoice_no" in query:
        subject = query["invoice_no"]
    else:
        subject = " ".join([f"#{s}" if s.isdigit() else s for s in segs[1:]] + [v for v in query.values() if v])
    return singular, plural, heading, subject


def _show(value: object) -> str:
    """Display a value the way a person would write it (money with separators, ISO dates)."""
    norm = _norm(value)
    if re.fullmatch(r"-?\d+\.\d{2}", norm) and (isinstance(value, (int, float)) and not isinstance(value, bool)
                                                or _MONEY.match(" ".join(str(value).split()).lower())):
        return f"{float(norm):,.2f}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", norm):
        return norm
    return " ".join(str(value).split())


def to_english(clause: dict) -> str:
    """One plain sentence a non-technical reader can check off."""
    try:
        kind = clause.get("kind")
        if kind == "outbox_contains":
            return f"A message to {clause.get('to')} mentions {clause.get('text')}"
        if kind == "file_contains":
            return f"The file {clause.get('path')} contains {clause.get('text')}"
        if kind in HTTP_KINDS:
            singular, plural, heading, subject = _describe_path(str(clause.get("path", "")))
            if kind == "row_count":
                n = clause.get("equals")
                tail = f" for {subject}" if subject else ""
                if n == 0:
                    return f"No {plural}{tail}"
                return f"Exactly 1 {singular}{tail}" if n == 1 else f"Exactly {n} {plural}{tail}"
            head = f"{heading} {subject}".strip()
            field = str(clause.get("field", "")).replace("_", " ")
            if kind == "http_field_equals":
                return f"{head}: {field} equals {_show(clause.get('equals'))}"
            return f"{head}: {field} includes {_show(clause.get('contains'))}"
        return f"Unrecognised check ({kind})"
    except Exception as exc:  # never let presentation break a run
        return f"Unreadable check ({type(exc).__name__})"


# ---------------------------------------------------------------- evaluation

def _get(client: httpx.Client, path: str) -> tuple[object, dict, str | None]:
    """GET path -> (json_or_MISSING, evidence, problem). Never raises httpx errors."""
    evidence = {"request": f"GET {path}", "status": None, "body_excerpt": ""}
    try:
        r = client.get(path)
    except httpx.HTTPError as exc:
        return _MISSING, evidence, f"could not reach the app ({type(exc).__name__}: {exc})"
    evidence["status"] = r.status_code
    evidence["body_excerpt"] = _excerpt(r.text)
    if not r.is_success:
        return _MISSING, evidence, f"the app answered HTTP {r.status_code}"
    try:
        return r.json(), evidence, None
    except ValueError:
        return _MISSING, evidence, "the app's response was not JSON"


def _describe(data: object) -> str:
    return "an object" if isinstance(data, dict) else json.dumps(data, default=str)[:80]


def _field(data: object, field: str) -> tuple[object, str | None]:
    """Read a dotted field (list indexes allowed) -> (value, problem)."""
    if isinstance(data, list):
        if not data:
            return _MISSING, "no rows were returned"
        data = data[0]
    cur = data
    for part in field.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and re.fullmatch(r"-?\d+", part) and -len(cur) <= int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISSING, f"field {field!r} is not present in the response"
    return cur, None


def _check_http(client: httpx.Client, clause: dict) -> tuple[bool, object, dict]:
    data, evidence, problem = _get(client, clause["path"])
    if problem:
        return False, problem, evidence
    kind = clause["kind"]
    if kind == "row_count":
        if not isinstance(data, list):
            return False, f"expected a list of rows but the response was {_describe(data)}", evidence
        return len(data) == clause["equals"], len(data), evidence
    value, problem = _field(data, clause["field"])
    if problem:
        return False, problem, evidence
    if kind == "http_field_equals":
        return _norm(value) == _norm(clause["equals"]), value, evidence
    needle = clause["contains"]
    if isinstance(value, list):
        return any(_norm(v) == _norm(needle) for v in value), value, evidence
    if isinstance(value, str):
        return _text(needle) in _text(value), value, evidence
    return False, f"field {clause['field']!r} is {value!r}, not a list or text", evidence


def _check_outbox(client: httpx.Client, clause: dict) -> tuple[bool, object, dict]:
    to = clause["to"].strip()
    data, evidence, problem = _get(client, f"/api/outbox?to={quote(to, safe='@')}")
    if problem:
        return False, problem, evidence
    if not isinstance(data, list):
        return False, f"expected a list of messages but the response was {_describe(data)}", evidence
    # Re-filter locally: the referee does not trust the server-side filter to be right.
    msgs = [m for m in data if isinstance(m, dict) and str(m.get("to", "")).strip().casefold() == to.casefold()]
    needle = _text(clause["text"])
    for m in msgs:
        if needle in _text(m.get("subject", "")) or needle in _text(m.get("body", "")):
            evidence["body_excerpt"] = _excerpt(json.dumps(m, ensure_ascii=False))
            return True, f"message {m.get('id')} to {m.get('to')}: {m.get('subject')!r}", evidence
    return False, f"{len(msgs)} message(s) to {to}, none mention {clause['text']!r}", evidence


def _check_file(clause: dict) -> tuple[bool, object, dict]:
    # file evidence has no HTTP status, so status is None and request reads "READ <path>".
    # Matching is case-insensitive with whitespace collapsed, like the other "contains" checks.
    rel = clause["path"]
    evidence = {"request": f"READ {rel}", "status": None, "body_excerpt": ""}
    path = ROOT / rel  # an absolute path replaces ROOT, by pathlib's rules
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"could not read {rel} ({type(exc).__name__}: {exc.strerror or exc})", evidence
    flat, needle = " ".join(content.split()), _text(clause["text"])
    at = flat.casefold().find(needle)
    start = max(0, at - 100) if at >= 0 else 0
    evidence["body_excerpt"] = _excerpt(flat[start:])
    if at < 0:
        return False, f"{rel} ({len(content)} chars) does not contain {clause['text']!r}", evidence
    return True, f"{rel} contains {clause['text']!r}", evidence


def evaluate(clauses: list[dict], base_url: str) -> list[dict]:
    """Judge every clause against the live system. Never raises."""
    results: list[dict] = []
    if not isinstance(clauses, list):
        clauses = [clauses]
    with httpx.Client(base_url=base_url, timeout=TIMEOUT, follow_redirects=True) as client:
        for clause in clauses:
            evidence = {"request": "", "status": None, "body_excerpt": ""}
            try:
                errors = validate_contract([clause])
                if errors:
                    passed, observed = False, "invalid clause: " + " ".join(errors)
                elif clause["kind"] == "outbox_contains":
                    passed, observed, evidence = _check_outbox(client, clause)
                elif clause["kind"] == "file_contains":
                    passed, observed, evidence = _check_file(clause)
                else:
                    passed, observed, evidence = _check_http(client, clause)
            except Exception as exc:  # the referee reports, it never crashes the run
                passed, observed = False, f"referee error: {type(exc).__name__}: {exc}"
            results.append({"clause": clause, "passed": bool(passed), "observed": observed, "evidence": evidence})
    return results


# ---------------------------------------------------------------- side-effect audit

def snapshot(base_url: str) -> dict:
    """GET /api/snapshot with a fresh client. Raises on failure: a missing baseline must not look like 'no changes'."""
    with httpx.Client(base_url=base_url, timeout=TIMEOUT) as client:
        r = client.get("/api/snapshot")
        r.raise_for_status()
        data = r.json()
    if not isinstance(data, dict):
        raise ValueError(f"/api/snapshot returned {type(data).__name__}, expected an object")
    return data


def _row_ref(table: str, row: object) -> str:
    if not isinstance(row, dict):
        return str(row)
    if table == "invoices":
        return str(row.get("invoice_no", ""))
    if table == "outbox":
        return "notify:" + str(row.get("to", ""))
    return str(row.get("id", ""))


def _keyed(rows: list) -> dict:
    """Rows keyed by id (content when there is no id), so edits show as 'changed'."""
    out: dict = {}
    for row in rows:
        if isinstance(row, dict) and "id" in row:
            out[("id", json.dumps(row["id"], default=str))] = row
        else:
            out[("row", json.dumps(row, sort_keys=True, default=str))] = row
    return out


def _columns(table: str, value: dict) -> dict[str, object]:
    """Flatten a catalog-style {table: {column: info} | [column...]} into {"table.column": info}."""
    out: dict[str, object] = {}
    for name, cols in value.items():
        if isinstance(cols, dict):
            pairs = cols.items()
        elif isinstance(cols, list):
            pairs = [((c.get("name", i) if isinstance(c, dict) else c), c) for i, c in enumerate(cols)]
        else:
            pairs = []
            out[str(name)] = cols
        for col, info in pairs:
            out[f"{name}.{col}"] = info
    return out


def diff(before: dict, after: dict) -> list[dict]:
    """Row-level changes between two snapshots: [{"table", "ref", "change", "row"}]."""
    changes: list[dict] = []
    tables = list(after) + [t for t in before if t not in after]
    for table in tables:
        b, a = before.get(table), after.get(table)
        if isinstance(b, dict) or isinstance(a, dict):
            bm, am = _columns(table, b or {}), _columns(table, a or {})
            for ref in [k for k in am if k not in bm]:
                changes.append({"table": table, "ref": ref, "change": "added", "row": am[ref]})
            for ref in [k for k in bm if k not in am]:
                changes.append({"table": table, "ref": ref, "change": "removed", "row": bm[ref]})
            for ref in [k for k in am if k in bm and am[k] != bm[k]]:
                changes.append({"table": table, "ref": ref, "change": "changed", "row": am[ref]})
        elif isinstance(b, list) or isinstance(a, list):
            bm, am = _keyed(b or []), _keyed(a or [])
            for key, row in am.items():
                if key not in bm:
                    changes.append({"table": table, "ref": _row_ref(table, row), "change": "added", "row": row})
                elif bm[key] != row:
                    changes.append({"table": table, "ref": _row_ref(table, row), "change": "changed", "row": row})
            for key, row in bm.items():
                if key not in am:
                    changes.append({"table": table, "ref": _row_ref(table, row), "change": "removed", "row": row})
        elif b != a:
            changes.append({"table": table, "ref": table, "change": "changed", "row": a})
    return changes


def _ref_hits(ref: object, clause: dict, fields: tuple[str, ...]) -> bool:
    """Is `ref` named by this clause: equal to its ref, or appearing (as a whole token) in one of `fields`?"""
    r = str(ref or "").strip().casefold()
    if not r or not isinstance(clause, dict):
        return False  # an empty ref would 'appear' in everything
    if str(clause.get("ref", "")).strip().casefold() == r:
        return True
    # outbox refs are "notify:<to>"; an outbox clause names them through its `to` address.
    if r.startswith("notify:") and str(clause.get("to", "")).strip().casefold() == r[len("notify:"):]:
        return True
    token = re.compile(rf"(?<![\w-]){re.escape(r)}(?![\w-])")
    for key in fields:
        if clause.get(key) is None:
            continue
        hay = str(clause[key]).casefold()
        if token.search(hay) or token.search(unquote(hay)):
            return True
    return False


def unexpected_changes(changes: list[dict], clauses: list[dict], allowed_refs=()) -> list[dict]:
    """Changes not named by any clause nor listed in allowed_refs."""
    allowed = {str(a).strip().casefold() for a in (allowed_refs or ()) if str(a).strip()}
    clauses = clauses or []
    fields = ("path", "to", "text", "equals", "contains")
    return [ch for ch in changes
            if str(ch.get("ref", "")).strip().casefold() not in allowed
            and not any(_ref_hits(ch.get("ref"), c, fields) for c in clauses)]


def verdict(results: list[dict], proposals: list[dict], unexpected: list[dict]) -> dict:
    """VERIFIED / VERIFIED_WITH_EXCEPTIONS / FAILED from referee results and the gateway's proposals."""
    # "failures" holds the failed results no proposal excuses; "unexpected" is added so a
    # FAILED caused only by side effects still says why. Excused failures are listed in "exceptions".
    exceptions: list[dict] = []
    failures: list[dict] = []
    for res in results:
        if res.get("passed") is True:
            continue
        clause = res.get("clause") or {}
        related = [p for p in proposals if isinstance(p, dict) and _ref_hits(p.get("ref"), clause, ("path",))]
        excuse = next((p for p in related if str(p.get("status", "")).upper() in EXCUSING), None)
        # a matching proposal that was allowed/approved/submitted/done means the agent did attempt
        # this work, so an earlier blocked attempt with the same ref cannot excuse the failure.
        attempted = any(str(p.get("status", "")).upper() not in EXCUSING for p in related)
        if excuse is None or attempted:
            failures.append(res)
        else:
            exceptions.append({"clause": clause, "proposal_id": excuse.get("proposal_id"),
                               "status": excuse.get("status"), "rule_id": excuse.get("rule_id"),
                               "reason": excuse.get("reason")})
    if failures or unexpected:
        status = "FAILED"
    elif exceptions:
        status = "VERIFIED_WITH_EXCEPTIONS"
    else:
        status = "VERIFIED"
    return {"status": status, "exceptions": exceptions, "failures": failures, "unexpected": list(unexpected)}
