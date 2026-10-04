"""Receipts policy: the deterministic, domain-specific rules the gateway enforces.

Everything that knows about invoices, vendors, money, dates, mail and the data catalog lives here;
gateway.py is a generic engine that calls into this module. Rules reach the company
app only through its read-only JSON API, with short timeouts.
"""
from __future__ import annotations

import difflib
import json
import os
import posixpath
import re
import tempfile
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from email.utils import getaddresses, parseaddr
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

import httpx

APP_URL: str = os.environ.get("RECEIPTS_APP_URL", "http://127.0.0.1:8000")
BLOCKED_PATHS = ("/chaos", "/api/chaos", "/api/reset")
APPROVAL_THRESHOLD = 5000.0
INTERNAL_DOMAIN = "company.test"
HTTP_TIMEOUT = 5.0
CENT = Decimal("0.01")


def app_url() -> str:
    """Company app base URL, read at call time so checks can set the env after import."""
    # APP_URL is the import-time value; every request re-reads RECEIPTS_APP_URL.
    return os.environ.get("RECEIPTS_APP_URL", APP_URL).rstrip("/")


def _get(path: str, params: dict | None = None, base: str | None = None) -> Any:
    r = httpx.get(f"{(base or app_url()).rstrip('/')}{path}", params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _clean(value: Any) -> str:
    return " ".join(str(value if value is not None else "").split())


# ---------------------------------------------------------------- money and dates

_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july",
                "august", "september", "october", "november", "december")
MONTHS = {**{m: i for i, m in enumerate(_MONTH_NAMES, 1)},
          **{m[:3]: i for i, m in enumerate(_MONTH_NAMES, 1)}, "sept": 9}
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_MON = r"([a-z]{3,9})\.?"
# (pattern, group indexes of (year, month, day))
_DATE_FORMS = (
    (r"(\d{4})-(\d{1,2})-(\d{1,2})", (1, 2, 3)),
    (r"(\d{1,2})/(\d{1,2})/(\d{4})", (3, 2, 1)),
    (_DAY + r"\s+" + _MON + r",?\s+(\d{4})", (3, 2, 1)),
    (_MON + r"\s+" + _DAY + r",?\s+(\d{4})", (3, 1, 2)),
)
_DATE_FULL = [(re.compile(p, re.I), o) for p, o in _DATE_FORMS]
_DATE_SCAN = [(re.compile(r"(?<![\w/-])" + p + r"(?!\d)", re.I), o) for p, o in _DATE_FORMS]

_CUR = r"(?:US\$|\$|USD|EUR|€|£|GBP)"
_MONEY_FULL = re.compile(rf"(-)?\s*{_CUR}?\s*(-)?\s*(\d{{1,3}}(?:,\d{{3}})+|\d+)(\.\d+)?\s*(?:USD|EUR|GBP)?", re.I)
_MONEY_SCAN = re.compile(
    rf"(?<![\w.,-])(-\s?)?({_CUR}\s?)?(-\s?)?(\d{{1,3}}(?:,\d{{3}})+|\d+)(\.\d{{2}})?(?!\d|[.,]\d)(\s?(?:USD|EUR|GBP)\b)?",
    re.I)


def _iso(m: re.Match, order: tuple[int, int, int]) -> str | None:
    y, mo, d = (m.group(i) for i in order)
    month = int(mo) if mo.isdigit() else MONTHS.get(mo.lower())
    if not month:
        return None
    try:
        return date(int(y), month, int(d)).isoformat()
    except ValueError:
        return None


def parse_date(value: Any) -> str | None:
    """ISO date for a whole value in ISO, DD/MM/YYYY, 'D Month YYYY' or 'Month D, YYYY'; else None."""
    s = _clean(value)
    for rx, order in _DATE_FULL:
        if (m := rx.fullmatch(s)) and (iso := _iso(m, order)):
            return iso
    return None


def dates_in(text: str) -> list[str]:
    """Every date found in free text, as ISO strings, in order of appearance."""
    hits = sorted((m.start(), iso) for rx, order in _DATE_SCAN
                  for m in rx.finditer(text or "") if (iso := _iso(m, order)))
    return list(dict.fromkeys(iso for _, iso in hits))


def _decimal(number: str) -> Decimal | None:
    try:
        d = Decimal(number)
    except InvalidOperation:
        return None
    return d.quantize(CENT, ROUND_HALF_UP) if d.is_finite() else None


def parse_money(value: Any) -> Decimal | None:
    """Amount (2 decimals) for a whole money-like value ('$1,240.00', 'USD 2,310.50', '1240', 1240.0); else None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return _decimal(str(value))
    m = _MONEY_FULL.fullmatch(_clean(value))
    if not m:
        return None
    amount = _decimal(m.group(3).replace(",", "") + (m.group(4) or ""))
    return -amount if amount is not None and (m.group(1) or m.group(2)) else amount


def money_in(text: str) -> list[Decimal]:
    """Every money-looking figure in free text: has a currency marker, cents, or thousands commas.

    Bare integers (years, days, invoice numbers) are not money.
    """
    found = []
    for m in _MONEY_SCAN.finditer(text or ""):
        neg, cur, neg2, num, cents, cur2 = m.groups()
        if not (cur or cur2 or cents or "," in num):
            continue
        amount = _decimal(num.replace(",", "") + (cents or ""))
        if amount is not None:
            found.append(-amount if (neg or neg2) else amount)
    return list(dict.fromkeys(found))


def norm(value: Any) -> str:
    """Canonical form: money -> '%.2f', dates -> ISO, else whitespace-collapsed lowercase."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float, Decimal)):
        amount = _decimal(str(value))
        return format(amount, ".2f") if amount is not None else str(value).lower()
    s = _clean(value).lower()
    if iso := parse_date(s):
        return iso
    amount = parse_money(s)
    return format(amount, ".2f") if amount is not None else s


# ---------------------------------------------------------------- scope

def path_allowed(path: str) -> bool:
    """False for the chaos/reset controls (any spelling) and for absolute URLs to another host."""
    parts = urlsplit(str(path).strip())
    if parts.netloc and parts.netloc.lower() != urlsplit(app_url()).netloc.lower():
        return False
    p = re.sub(r"/+", "/", "/" + unquote(parts.path).replace("\\", "/"))
    p = posixpath.normpath(p).lower()
    return not any(p == b or p.startswith(b + "/") for b in BLOCKED_PATHS)


# ---------------------------------------------------------------- overrides

def _overrides_path() -> Path:
    return Path(os.environ.get("RECEIPTS_OVERRIDES", "policy_overrides.json"))


def load_overrides() -> list[dict]:
    """Human 'always allow' decisions. Missing file -> []; a corrupt file raises (never silently ignored)."""
    path = _overrides_path()
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8") or "[]")
    if not isinstance(data, list):
        raise ValueError(f"{path} must hold a JSON list of overrides")
    return data


def add_override(kind: str, rule_id: str, match: dict, limit: float | None, by: str) -> None:
    """Append an override and write the file atomically."""
    path = _overrides_path()
    entries = load_overrides()
    entries.append({"kind": kind, "rule_id": rule_id, "match": match,
                    "limit": None if limit is None else float(limit), "by": by,
                    "ts": datetime.now().isoformat(timespec="seconds")})
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _domain(address: str) -> str:
    addr = parseaddr(address)[1] or address.strip()
    return addr.rpartition("@")[2].strip().lower() if "@" in addr else ""


def override_for(kind: str, rule_id: str, fields: dict) -> tuple[dict, float | None]:
    """(match, limit) an 'always allow' of this proposal stores, and that later proposals are matched against."""
    # the gateway is domain-free, so policy decides what "always allow" covers:
    # over-threshold -> this vendor up to this amount; credit-note -> this vendor;
    # external-recipient -> this recipient domain; anything else -> these exact fields.
    vendor = _clean(fields.get("vendor"))
    if (kind, rule_id) == ("invoice", "over-threshold"):
        amount = parse_money(fields.get("amount"))
        return {"vendor": vendor}, None if amount is None else float(amount)
    if (kind, rule_id) == ("invoice", "credit-note"):
        return {"vendor": vendor}, None
    if (kind, rule_id) == ("notification", "external-recipient"):
        return {"domain": ",".join(_external_domains(_clean(fields.get("to"))))}, None
    return {k: norm(v) for k, v in fields.items()}, None


def _overridden(kind: str, rule_id: str, fields: dict, amount: Decimal | None = None) -> bool:
    match = override_for(kind, rule_id, fields)[0]
    want = {k: norm(v) for k, v in match.items()}
    for o in load_overrides():
        if (o.get("kind"), o.get("rule_id")) != (kind, rule_id):
            continue
        if {k: norm(v) for k, v in (o.get("match") or {}).items()} != want:
            continue
        limit = o.get("limit")
        # limit None means "no amount cap".
        if limit is None or amount is None or Decimal(str(limit)) >= amount:
            return True
    return False


# ---------------------------------------------------------------- invoice rules

def _vendors(ctx: dict) -> list[dict]:
    if "_vendors" not in ctx:  # one fetch per proposal, shared by the vendor and sender rules
        ctx["_vendors"] = _get("/api/vendors", base=ctx.get("app_url"))
    return ctx["_vendors"]


def _where(ctx: dict) -> str:
    return f"source email {ctx.get('source')}"


def not_in_source(fields: dict, ctx: dict) -> tuple[str, str, str] | None:
    """Every invoice figure must be copied from the source email, never invented."""
    text, src = ctx.get("source_text") or "", ctx.get("source")
    if not text:
        why = f"source {src!r} is not an email in the inbox" if src else "no source email given"
        return ("BLOCKED", "not-in-source",
                f"{why}; propose again with source='inbox:<id>' of the email this invoice came from")
    problems = []
    inv = _clean(fields.get("invoice_no"))
    if not inv:
        problems.append("invoice_no is missing (fields: vendor, invoice_no, amount, due_date)")
    elif not re.search(rf"(?<![\w-]){re.escape(inv)}(?![\w-])", text, re.I):
        problems.append(f"invoice_no {inv} not found in {_where(ctx)}")

    raw = fields.get("amount")
    amount = parse_money(raw)
    if raw is None or _clean(raw) == "":
        problems.append("amount is missing")
    elif amount is None:
        problems.append(f"amount {raw!r} is not a number")
    else:
        found = money_in(text)
        # compared by absolute value, so a credit entered as -450.00 still matches "$450.00".
        if abs(amount) not in {abs(f) for f in found}:
            seen = ", ".join(format(f, ".2f") for f in found) or "no amounts"
            problems.append(f"amount {amount:.2f} not found in {_where(ctx)} (found: {seen})")

    raw_due = fields.get("due_date")
    due = parse_date(raw_due)
    if raw_due is None or _clean(raw_due) == "":
        problems.append("due_date is missing")
    elif due is None:
        problems.append(f"due_date {raw_due!r} is not a date (use YYYY-MM-DD)")
    elif due not in (found_dates := dates_in(text)):
        problems.append(f"due_date {due} not found in {_where(ctx)} (found: {', '.join(found_dates) or 'no dates'})")
    return ("BLOCKED", "not-in-source", "; ".join(problems)) if problems else None


def vendor_not_allowlisted(fields: dict, ctx: dict) -> tuple[str, str, str] | None:
    names = [v["name"] for v in _vendors(ctx)]
    vendor = _clean(fields.get("vendor"))
    lowered = [n.lower() for n in names]
    if vendor.lower() in lowered:
        return None
    allowed = ", ".join(names) or "none"
    if not vendor:
        return ("BLOCKED", "vendor-not-allowlisted", f"vendor is missing (approved vendors: {allowed})")
    closest = difflib.get_close_matches(vendor.lower(), lowered, n=1, cutoff=0)
    hint = f'closest allowed vendor: "{names[lowered.index(closest[0])]}"; ' if closest else ""
    return ("BLOCKED", "vendor-not-allowlisted",
            f'vendor "{vendor}" is not on the approved vendor list ({hint}approved: {allowed}); '
            "payables may only be created for approved vendors")


def sender_mismatch(fields: dict, ctx: dict) -> tuple[str, str, str] | None:
    sender = ctx.get("source_from") or ""
    if not sender:
        return None  # no source email: not-in-source already blocks
    sdom = _domain(sender)
    vendor = _clean(fields.get("vendor"))
    vendors = _vendors(ctx)

    def owns(v: dict) -> bool:
        d = str(v.get("domain", "")).lower()
        return bool(d) and (sdom == d or sdom.endswith("." + d))

    registered = next((v for v in vendors if v["name"].lower() == vendor.lower()), None)
    if registered and owns(registered):
        return None
    if registered:
        reason = f"{_where(ctx)} was sent from {sdom}, but {registered['name']} is registered with domain {registered['domain']}"
    else:
        reason = f'{_where(ctx)} was sent from {sdom}, but "{vendor}" has no registered domain'
    owner = next((v for v in vendors if owns(v)), None)
    if owner:
        reason += f" ({sdom} belongs to {owner['name']})"
    return ("BLOCKED", "sender-mismatch", reason + "; an invoice must come from the vendor's own domain")


def credit_note(fields: dict, ctx: dict) -> tuple[str, str, str] | None:
    hit = re.search(r"credit\s+(memo|note)", ctx.get("source_text") or "", re.I)
    amount = parse_money(fields.get("amount"))
    if not hit and not (amount is not None and amount < 0):
        return None
    if _overridden("invoice", "credit-note", fields):
        return None
    what = f"{_where(ctx)} is a {hit.group(0).upper()}" if hit else f"amount {amount:.2f} is negative"
    return ("PARKED", "credit-note", f"{what}, not a payable invoice; a human must decide how to book it")


def over_threshold(fields: dict, ctx: dict) -> tuple[str, str, str] | None:
    amount = parse_money(fields.get("amount"))
    if amount is None or amount <= Decimal(str(APPROVAL_THRESHOLD)):
        return None
    if _overridden("invoice", "over-threshold", fields, amount):
        return None
    return ("PARKED", "over-threshold",
            f"amount {amount:.2f} for {_clean(fields.get('vendor'))} is above the approval threshold of "
            f"{APPROVAL_THRESHOLD:.2f}; it needs a human approval before it is entered")


# ---------------------------------------------------------------- notification rules

def _external_domains(to: str) -> list[str]:
    """Recipient domains other than INTERNAL_DOMAIN, for every address in `to` ('' = not an address)."""
    domains = {_domain(addr) for _, addr in getaddresses([to.replace(";", ",")]) if addr} or {""}
    return sorted(d for d in domains if d != INTERNAL_DOMAIN)


def external_recipient(fields: dict, ctx: dict) -> tuple[str, str, str] | None:
    to = _clean(fields.get("to"))
    external = _external_domains(to)
    if not external or _overridden("notification", "external-recipient", fields):
        return None
    where = f"domain {', '.join(external)}" if all(external) else "not an email address"
    return ("PARKED", "external-recipient",
            f"recipient {to or '(missing)'} is outside {INTERNAL_DOMAIN} ({where}); "
            "external mail needs a human approval")


# ---------------------------------------------------------------- existence checks

def _invoice_lookup(fields: dict) -> tuple[bool, str]:
    inv, vendor = _clean(fields.get("invoice_no")), _clean(fields.get("vendor"))
    if not inv:
        return False, "no invoice_no to look up"
    rows = [r for r in _get("/api/ledger", {"invoice_no": inv})
            if str(r.get("invoice_no", "")).lower() == inv.lower() and str(r.get("vendor", "")).lower() == vendor.lower()]
    evidence = f"GET /api/ledger?invoice_no={inv} -> {len(rows)} row{'' if len(rows) == 1 else 's'} for {vendor}"
    if rows:
        r = rows[0]
        evidence += f" (id {r.get('id')}, amount {r.get('amount')}, due {r.get('due_date')}, status {r.get('status')})"
    return bool(rows), evidence


def _notification_lookup(fields: dict) -> tuple[bool, str]:
    to, subject = _clean(fields.get("to")), _clean(fields.get("subject"))
    if not to:
        return False, "no recipient to look up"
    msgs = [m for m in _get("/api/outbox", {"to": to}) if norm(m.get("subject")) == norm(subject)]
    evidence = f"GET /api/outbox?to={to} -> {len(msgs)} message{'' if len(msgs) == 1 else 's'} with subject {subject!r}"
    if msgs:
        evidence += f" (id {msgs[0].get('id')}, sent {msgs[0].get('sent_at')})"
    return bool(msgs), evidence


# ---------------------------------------------------------------- data catalog (column_tag)

def _catalog_column(fields: dict, base: str | None = None) -> tuple[dict | None, str]:
    """(column row from /api/catalog/columns, problem) for a column_tag proposal."""
    # the tag form carries the table as a hidden field, and the agent proposes only the fields its
    # snapshot shows (column, tag). Without a table the column is looked up across the catalog; it must be unique.
    table, column = _clean(fields.get("table")), _clean(fields.get("column"))
    if not column:
        return None, "column is missing (fields: column, tag)"
    rows = _get("/api/catalog/columns", {"table": table, "column": column}, base=base)
    if len(rows) == 1:
        return rows[0], ""
    if rows:
        tables = ", ".join(r["table"] for r in rows)
        return None, (f"column {column} exists in several tables ({tables}); "
                      "propose from that table's page with its table field")
    where = f"table {table}" if table else "any catalog table"
    return None, f"column {column} does not exist in {where}"


def _column_tag_lookup(fields: dict) -> tuple[bool, str]:
    row, problem = _catalog_column(fields)
    if row is None:
        return False, problem
    tag, tags = _clean(fields.get("tag")), [str(t) for t in row.get("tags") or []]
    evidence = (f"GET /api/catalog/columns?table={row['table']}&column={row['column']} -> "
                f"tags {', '.join(tags) or '(none)'}")
    return bool(tag) and norm(tag) in {norm(t) for t in tags}, evidence


def _column_ref(fields: dict) -> str:
    table, column = _clean(fields.get("table")), _clean(fields.get("column"))
    if not table:
        try:
            row, _ = _catalog_column(fields)
        except (httpx.HTTPError, ValueError, KeyError):
            row = None  # REF must never raise; the rules report an unreachable catalog
        table = row["table"] if row else ""
        column = row["column"] if row else column
    return f"{table}.{column}" if table else column


def unknown_column(fields: dict, ctx: dict) -> tuple[str, str, str] | None:
    row, problem = _catalog_column(fields, ctx.get("app_url"))
    if row is not None:
        return None
    return ("BLOCKED", "unknown-column",
            f"{problem}; only existing catalog columns can be tagged (see /api/catalog/columns)")


RULES: dict[str, list[Callable[[dict, dict], tuple[str, str, str] | None]]] = {
    "invoice": [not_in_source, vendor_not_allowlisted, sender_mismatch, credit_note, over_threshold],
    "notification": [external_recipient],
    "column_tag": [unknown_column],
}
EXISTS: dict[str, Callable[[dict], bool]] = {
    "invoice": lambda fields: _invoice_lookup(fields)[0],
    "notification": lambda fields: _notification_lookup(fields)[0],
    "column_tag": lambda fields: _column_tag_lookup(fields)[0],
}
# human-readable proof for gateway.confirm's "evidence".
EVIDENCE: dict[str, Callable[[dict], str]] = {
    "invoice": lambda fields: _invoice_lookup(fields)[1],
    "notification": lambda fields: _notification_lookup(fields)[1],
    "column_tag": lambda fields: _column_tag_lookup(fields)[1],
}
REF: dict[str, Callable[[dict], str]] = {
    "invoice": lambda fields: _clean(fields.get("invoice_no")),
    "notification": lambda fields: "notify:" + _clean(fields.get("to")),
    "column_tag": _column_ref,
}
