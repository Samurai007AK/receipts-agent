"""Browser tools for the Receipts agent: a Playwright (sync, Chromium) session on the company app.

Registers four gateway tools (CONTRACT section 4): `goto` and `read_page` (read), `click` and
`fill_form` (write). Every result starts with one `<STATUS>: <summary>` line, then detection lines
(`DETECTED: ...`), then a compact text snapshot of the page for the LLM. A click on a submit control
inside a `form[data-record]` is guarded: the form's values must match an allowed proposal
(`gateway.guard_submit`) or nothing is clicked; after a successful submit the write is confirmed
against the system of record (`gateway.confirm`) and reported as `VERIFIED: <evidence>`.
`TOOL_SCHEMAS` holds the OpenAI function-calling schemas for the four tools.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Response, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

import gateway
import policy

ACTION_TIMEOUT_MS = 8_000
NAV_TIMEOUT_MS = 15_000
VIDEO_SIZE = {"width": 1280, "height": 800}
SNAPSHOT_MAX = 2_500
TEXT_MAX = 1_400
LINKS_MAX = 25
VALUE_MAX = 160
LOGIN_PATH = "/login"

# One DOM walk -> plain data; Python formats it. Visible = rendered with a box and not visibility:hidden
# (closed <details> content has no box, so it is excluded; its <summary> is listed as a button).
_SCAN_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const shown = el => el.getClientRects().length > 0
    && (!el.checkVisibility || el.checkVisibility({visibilityProperty: true}));
  const rel = href => {
    try { const u = new URL(href, location.href);
          return u.origin === location.origin ? u.pathname + u.search + u.hash : u.href; }
    catch (e) { return href || ''; }
  };
  const nameOf = el => clean(el.getAttribute('aria-label')
    || (el.tagName === 'INPUT' ? el.value : el.innerText) || el.getAttribute('title') || '');
  const isSubmit = el => (el.tagName === 'BUTTON' || el.tagName === 'INPUT')
    && (el.type === 'submit' || el.type === 'image');
  const NOT_FIELDS = ['hidden', 'submit', 'button', 'reset', 'image', 'fieldset', 'output'];

  const alerts = [];
  for (const el of document.querySelectorAll('[role=alert], .error')) {
    const t = shown(el) ? clean(el.innerText) : '';
    if (t && !alerts.includes(t)) alerts.push(t);
  }

  // Main text without forms and alerts (both are reported separately).
  const root = document.querySelector('main') || document.body;
  const parts = [];
  const SKIP = 'form, [role=alert], .error, script, style, template, noscript, nav, header';
  const walk = node => {
    for (const ch of node.childNodes) {
      if (ch.nodeType === 3) { const t = clean(ch.textContent); if (t) parts.push(t); continue; }
      if (ch.nodeType !== 1 || ch.matches(SKIP) || !shown(ch)) continue;
      if (ch.querySelector(SKIP)) { walk(ch); continue; }
      const t = ch.innerText.split('\n').map(l => clean(l.replace(/\t+/g, ' | '))).filter(Boolean).join(' / ');
      if (t) parts.push(t);
    }
  };
  if (root) walk(root);

  const links = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    if (!shown(a)) continue;
    const href = rel(a.href), text = nameOf(a) || href, key = text + '\n' + href;
    if (!seen.has(key)) { seen.add(key); links.push([text, href]); }
  }

  const submits = new Set(), forms = [];
  for (const f of document.forms) {
    if (!shown(f)) continue;
    const fields = [], buttons = [];
    for (const el of f.elements) {
      if (isSubmit(el) && shown(el)) { submits.add(el); buttons.push(nameOf(el) || 'Submit'); continue; }
      if (!el.name || el.tagName === 'BUTTON' || NOT_FIELDS.includes(el.type) || !shown(el)) continue;
      const label = clean((el.labels && el.labels.length && el.labels[0].innerText)
        || el.getAttribute('aria-label') || el.placeholder || el.name);
      const field = {label, name: el.name, type: el.type, value: el.value, placeholder: el.placeholder || ''};
      if (el.tagName === 'SELECT') {
        field.options = Array.from(el.options).map(o => clean(o.label));
        field.value = el.selectedOptions.length ? clean(el.selectedOptions[0].label) : '';
      }
      if (el.type === 'checkbox' || el.type === 'radio') field.value = el.checked ? 'checked' : 'unchecked';
      if (el.type === 'password') field.value = el.value ? '***' : '';  // CONTRACT: secrets never reach the LLM
      fields.push(field);
    }
    forms.push({record: f.getAttribute('data-record') || '', action: rel(f.action), fields, buttons});
  }

  const other = [];
  const BTN = 'button, input[type=submit], input[type=button], input[type=reset], input[type=image], summary, [role=button]';
  for (const el of document.querySelectorAll(BTN)) {
    if (submits.has(el) || !shown(el)) continue;
    const t = nameOf(el);
    if (t && !other.includes(t)) other.push(t);
  }
  const here = location.protocol.startsWith('http') ? location.pathname + location.search : location.href;
  return {path: here, title: clean(document.title), alerts,
          text: parts.join(' / '), links, forms, other,
          signedIn: !!document.querySelector('a[href$="/logout"]')};
}
"""

# What a click target is: a submit control of which form, a navigating link, and the form's values.
_TARGET_JS = r"""
el => {
  const isSubmit = (el.tagName === 'BUTTON' || el.tagName === 'INPUT') && (el.type === 'submit' || el.type === 'image');
  const form = isSubmit ? el.form : null;
  const out = {submit: !!form, record: form ? (form.getAttribute('data-record') || '') : '',
               fields: {}, invalid: '', href: ''};
  if (form) {
    for (const f of form.elements) {
      if (!f.name || f.disabled || f.tagName === 'BUTTON'
          || ['submit', 'button', 'reset', 'image', 'fieldset', 'output'].includes(f.type)) continue;
      if ((f.type === 'checkbox' || f.type === 'radio') && !f.checked) continue;
      out.fields[f.name] = {value: f.value, hidden: f.type === 'hidden'};
    }
    if (!form.noValidate && !el.formNoValidate && !form.checkValidity()) {
      const bad = form.querySelector(':invalid');
      const label = bad && bad.labels && bad.labels.length ? bad.labels[0].innerText.trim() : (bad ? bad.name : '');
      out.invalid = (label ? label + ': ' : '') + (bad ? bad.validationMessage : 'form is invalid');
    }
  }
  const a = el.closest('a[href]');
  if (a) {
    const raw = a.getAttribute('href') || '';
    if (!raw.startsWith('#') && !/^javascript:/i.test(raw) && a.target !== '_blank') out.href = a.href;
  }
  return out;
}
"""

# The current values of the form that holds `el` (for fill_form's result).
_FORM_VALUES_JS = r"""
el => {
  const form = el.form || el.closest('form');
  if (!form) return null;
  const values = [];
  for (const f of form.elements) {
    if (!f.name || f.tagName === 'BUTTON'
        || ['hidden', 'submit', 'button', 'reset', 'image', 'fieldset', 'output'].includes(f.type)) continue;
    let v = f.value;
    if (f.tagName === 'SELECT') v = f.selectedOptions.length ? f.selectedOptions[0].label : '';
    if (f.type === 'checkbox' || f.type === 'radio') v = f.checked ? 'checked' : 'unchecked';
    if (f.type === 'password') v = f.value ? '***' : '';
    values.push([f.name, v]);
  }
  const action = new URL(form.action, location.href);
  return {record: form.getAttribute('data-record') || '',
          action: action.origin === location.origin ? action.pathname + action.search : action.href, values};
}
"""

_FIELD_KIND_JS = "el => el.tagName === 'SELECT' ? 'select' : (el.type === 'checkbox' || el.type === 'radio' ? 'check' : 'text')"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _q(value: Any) -> str:
    """Quote a value for display: one line, bounded."""
    return '"' + _cut(str(value).replace("\n", "\\n").replace('"', '\\"'), VALUE_MAX) + '"'


# One Playwright driver per process: the sync API cannot be started twice in one thread, and two
# Browser objects (say, the agent's and a recorder) must coexist. Stopped when the last one closes.
_DRIVER: dict[str, Any] = {"pw": None, "users": 0}


def _driver_acquire() -> Any:
    if _DRIVER["pw"] is None:
        _DRIVER["pw"] = sync_playwright().start()
    _DRIVER["users"] += 1
    return _DRIVER["pw"]


def _driver_release() -> None:
    _DRIVER["users"] -= 1
    if _DRIVER["users"] <= 0 and _DRIVER["pw"] is not None:
        pw, _DRIVER["pw"], _DRIVER["users"] = _DRIVER["pw"], None, 0
        pw.stop()


def _is_auth_path(url: str) -> bool:
    """A deliberate trip to the login or logout page."""
    return urlsplit(url).path.rstrip("/") in (LOGIN_PATH, "/logout")


def _first_line(exc: BaseException) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0]


class Browser:
    """One Chromium page on the company app, plus what the tools need to remember about it."""

    def __init__(self, base_url: str, headless: bool = True, video_dir: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.video_dir = video_dir
        self.page = None
        self.status: int | None = None   # HTTP status of the last main-frame document response
        self.logged_in = False           # last app page we saw had a "Sign out" link
        self._pw = self._browser = self._context = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self.page is not None:
            return
        self._pw = _driver_acquire()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        opts: dict[str, Any] = {}
        if self.video_dir:
            opts = {"record_video_dir": str(self.video_dir), "record_video_size": VIDEO_SIZE, "viewport": VIDEO_SIZE}
        self._context = self._browser.new_context(**opts)
        self._context.set_default_timeout(ACTION_TIMEOUT_MS)
        self._context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        self.page = self._context.new_page()
        self.page.on("response", self._on_response)

    def close(self) -> None:
        """Close the context first so a recorded video is finalized, then the browser and driver."""
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    closer.close()
                except Exception:
                    pass  # already gone (crashed or closed); nothing left to release
        if self._pw is not None:
            try:
                _driver_release()
            except Exception:
                pass
        self.page = self._pw = self._browser = self._context = None

    def _on_response(self, response: Response) -> None:
        # Redirect hops fire too; the final page's response comes last, so its status wins.
        page = self.page
        if page is not None and response.request.is_navigation_request() and response.frame == page.main_frame:
            self.status = response.status

    def _ensure(self) -> Any:
        if self.page is None:
            self.start()
        return self.page

    # ------------------------------------------------------------ reading

    def _scan(self) -> dict:
        return self._ensure().evaluate(_SCAN_JS)

    def _path(self) -> str:
        u = urlsplit(self._ensure().url)
        return (u.path or "/") + (f"?{u.query}" if u.query else "") if u.scheme in ("http", "https") else self.page.url

    def _on_login(self) -> bool:
        return urlsplit(self._ensure().url).path == LOGIN_PATH

    def snapshot(self) -> str:
        return self._format(self._scan())

    def _format(self, d: dict) -> str:
        status = self.status if self.status is not None else "?"
        head = [f"PAGE {d['path']} (HTTP {status}) — {d['title'] or '(untitled)'}"]
        head += [f"ALERT: {_cut(a, 300)}" for a in d["alerts"]]
        tail = []
        links = d["links"]
        if links:
            shown = " | ".join(f"{_cut(t, 60)} -> {h}" for t, h in links[:LINKS_MAX])
            more = f" | (+{len(links) - LINKS_MAX} more)" if len(links) > LINKS_MAX else ""
            tail.append(f"LINKS: {shown}{more}")
        for f in d["forms"]:
            tail.append(" ".join(["FORM", *([f"record={f['record']}"] if f["record"] else []), f"action={f['action']}"]))
            for x in f["fields"]:
                line = f"  - {_cut(x['label'], 60)} [{x['name']}] = {_q(x['value'])}"
                if x["placeholder"]:
                    line += f" (placeholder {_cut(x['placeholder'], 60)})"
                if "options" in x:
                    line += " options: " + _cut(" | ".join(x["options"]), 300)
                tail.append(line)
            tail.append("  buttons: " + (", ".join(f["buttons"]) if f["buttons"] else "(none visible)"))
        if d["other"]:
            tail.append("OTHER BUTTONS: " + _cut(" | ".join(d["other"]), 400))
        # TEXT is the elastic part: it gets whatever room the structure leaves.
        room = SNAPSHOT_MAX - len("\n".join(head + tail)) - len("\nTEXT: ")
        text = _cut(d["text"], min(TEXT_MAX, max(room, 200))) if d["text"] else ""
        out = "\n".join(head + ([f"TEXT: {text}"] if text else []) + tail)
        return out if len(out) <= SNAPSHOT_MAX + 300 else out[:SNAPSHOT_MAX] + "\n…(snapshot truncated)"

    def _detect(self, d: dict, expect_login: bool) -> list[str]:
        """Detection lines for the page we just landed on; also tracks whether we are signed in.

        expect_login: the login page was the expected outcome (we were on it already, or asked for
        /login or /logout), so landing there is not a session problem.
        """
        lines = []
        if self.status is not None and self.status >= 500:
            lines.append(f"DETECTED: server_error (HTTP {self.status})")
        if self._on_login():
            if not expect_login:
                if self.logged_in:
                    lines.append("DETECTED: session_expired (redirected to login)")
                else:
                    lines.append("NOTE: login required (sign in with the username and password you were given)")
            elif d["alerts"] and self.status is not None and 400 <= self.status < 500:
                # CONTRACT: a rejected sign-in (401 + alert) is reported like any other rejected form.
                lines.append("DETECTED: validation_error: " + _cut("; ".join(d["alerts"]), 400))
            self.logged_in = False
        else:
            if d["alerts"] and (self.status is None or self.status < 500):
                lines.append("DETECTED: validation_error: " + _cut("; ".join(d["alerts"]), 400))
            if d["signedIn"]:
                self.logged_in = True
        return lines

    # ------------------------------------------------------------ tools

    def goto(self, path: str) -> str:
        page = self._ensure()
        raw = str(path or "").strip() or "/"
        parts = urlsplit(raw)
        if parts.scheme or parts.netloc:
            base = urlsplit(self.base_url)
            if (parts.scheme.lower(), parts.netloc.lower()) != (base.scheme.lower(), base.netloc.lower()):
                return f"ERROR: {raw} is not on {self.base_url}; pass a path such as /inbox"
            raw = urlunsplit(("", "", parts.path or "/", parts.query, parts.fragment))
        if not raw.startswith("/"):
            raw = "/" + raw
        expect_login = self._on_login() or _is_auth_path(raw)
        try:
            page.goto(self.base_url + raw, wait_until="load")
        except PlaywrightError as e:
            return f"ERROR: could not open {raw}: {_first_line(e)}"
        d = self._scan()
        # CONTRACT: a page that loaded is OK even with HTTP 4xx/5xx; the DETECTED lines carry the problem.
        lines = [f"OK: {self._path()} (HTTP {self.status})", *self._detect(d, expect_login)]
        return "\n".join([*lines, self._format(d)])

    def _resolve(self, target: str) -> Locator | None:
        page = self._ensure()
        exact_text = re.compile(rf"^\s*{re.escape(target)}\s*$")
        candidates = (
            page.get_by_role("button", name=target, exact=True),
            page.get_by_role("link", name=target, exact=True),
            page.locator("summary").filter(has_text=exact_text),
            page.get_by_role("button", name=target),
            page.get_by_role("link", name=target),
            page.get_by_text(target, exact=True),
        )
        for loc in candidates:
            loc = loc.filter(visible=True)
            if loc.count():
                return loc.first
        return None

    def click(self, target: str, run_id: str) -> str:
        page = self._ensure()
        target = str(target or "").strip().strip("\"'").strip()
        el = self._resolve(target) if target else None
        if el is None:
            d = self._scan()
            # CONTRACT: the "list of visible button/link names" is two lines, AVAILABLE BUTTONS and AVAILABLE LINKS.
            buttons = [b for f in d["forms"] for b in f["buttons"]] + d["other"]
            return "\n".join([
                f'ERROR: no clickable element named "{target}"',
                "DETECTED: element_not_found",
                "AVAILABLE BUTTONS: " + (_cut(" | ".join(buttons), 400) or "(none)"),
                "AVAILABLE LINKS: " + (_cut(" | ".join(t for t, _ in d["links"]), 600) or "(none)"),
            ])
        info = el.evaluate(_TARGET_JS)
        name = _cut(re.sub(r"\s+", " ", el.inner_text(timeout=ACTION_TIMEOUT_MS) or "").strip()
                    or el.get_attribute("value") or target, 60)
        if info["href"] and not policy.path_allowed(info["href"]):
            return f"BLOCKED: path {self._rel(info['href'])} is off-limits (rule out-of-scope)"

        guard = None
        if info["submit"] and info["record"]:
            guard = gateway.guard_submit(run_id, info["record"], info["fields"])
            if not guard["allowed"]:
                return f"BLOCKED: {guard['reason']} (rule {guard['rule_id']})"
        if info["submit"] and info["invalid"]:
            el.click()
            return f"ERROR: the browser refused to submit the form: {info['invalid']}\n{self.snapshot()}"

        expect_login = self._on_login() or bool(info["href"]) and _is_auth_path(info["href"])
        navigates = info["submit"] or bool(info["href"])
        if navigates:
            try:
                with page.expect_navigation(wait_until="load"):
                    el.click()
            except PlaywrightTimeout:
                return f'ERROR: clicked "{name}" but no page loaded within {NAV_TIMEOUT_MS // 1000}s\n{self.snapshot()}'
            if guard and os.environ.get("RECEIPTS_CRASH_AFTER_SUBMIT") == "1":
                os._exit(3)  # kill-and-rerun test: the write landed, the agent never confirmed it
            status_line = f'OK: clicked "{name}" → {self._path()} (HTTP {self.status})'
        else:
            el.click()
            page.wait_for_load_state("load")
            status_line = f'OK: clicked "{name}" (no navigation; now on {self._path()})'

        d = self._scan()
        lines = [status_line, *(self._detect(d, expect_login) if navigates else [])]
        if guard:
            ok = (self.status is not None and 200 <= self.status < 300 and not self._on_login() and not d["alerts"])
            if ok:
                done = gateway.confirm(guard["proposal_id"])
                if done["status"] == "DONE":
                    lines.append(f"VERIFIED: {done['evidence']}")
                else:
                    lines.append(f"NOTE: submitted but not found in the system of record yet: {done['evidence']}")
            else:
                # CONTRACT: when the submit is not confirmed, a NOTE line says so (no VERIFIED line).
                lines.append("NOTE: the submit did not go through; nothing was recorded by this click")
        return "\n".join([*lines, self._format(d)])

    def _rel(self, href: str) -> str:
        u = urlsplit(href)
        return (u.path or "/") + (f"?{u.query}" if u.query else "")

    def _field(self, key: str) -> Locator | None:
        page = self._ensure()
        name_sel = '[name="' + key.replace("\\", "\\\\").replace('"', '\\"') + '"]'
        candidates = (
            page.get_by_label(key, exact=True),
            page.locator(name_sel),
            page.get_by_label(re.compile(rf"^\s*{re.escape(key)}\s*$", re.I)),
        )
        for loc in candidates:
            loc = loc.filter(visible=True)
            if loc.count():
                return loc.first
        return None

    def fill_form(self, fields: dict) -> str:
        self._ensure()
        if isinstance(fields, str):  # some models send the object JSON-encoded
            try:
                fields = json.loads(fields)
            except ValueError:
                pass
        if not isinstance(fields, dict) or not fields:
            return 'ERROR: fields must be a non-empty object such as {"Vendor": "Acme Corp"}'
        filled: list[Locator] = []
        missing: list[str] = []
        failed: list[str] = []
        for key, value in fields.items():
            el = self._field(str(key).strip())
            if el is None:
                missing.append(str(key))
                continue
            text = "" if value is None else str(value)
            try:
                kind = el.evaluate(_FIELD_KIND_JS)
                if kind == "select":
                    try:
                        el.select_option(label=text, timeout=2_000)
                    except PlaywrightError:
                        el.select_option(value=text, timeout=2_000)
                elif kind == "check":
                    el.set_checked(text.strip().lower() in ("1", "true", "yes", "on", "checked", "x"))
                else:
                    el.fill(text)
                filled.append(el)
            except PlaywrightError as e:
                failed.append(f'"{key}" ({_first_line(e)})')

        lines = []
        problems = []
        if missing:
            avail = ", ".join(f"{x['label']} [{x['name']}]" for f in self._scan()["forms"] for x in f["fields"])
            problems.append("no field " + ", ".join(f'"{m}"' for m in missing) + f"; available: {avail or '(no visible fields)'}")
        if failed:
            problems.append("could not fill " + "; ".join(failed))
        if problems:
            lines.append("ERROR: " + "; ".join(problems) + f" (filled {len(filled)} other field{'s' * (len(filled) != 1)})")
        else:
            lines.append(f"OK: filled {len(filled)} field{'s' * (len(filled) != 1)}")
        form = filled[-1].evaluate(_FORM_VALUES_JS) if filled else None
        if form:
            lines.append(" ".join(["FORM", *([f"record={form['record']}"] if form["record"] else []),
                                   f"action={form['action']}"]))
            lines += [f"  {n} = {_q(v)}" for n, v in form["values"]]
        return "\n".join(lines)


def _guarded(fn: Any) -> Any:
    """Turn Playwright failures into a compact ERROR line plus the page, instead of a stack-trace dump."""
    def run(ctx: Any, **args: Any) -> str:
        try:
            return fn(ctx, **args)
        except PlaywrightError as e:
            try:
                page = ctx.browser.snapshot()
            except Exception:
                page = "(page unavailable)"
            return f"ERROR: {type(e).__name__}: {_first_line(e)}\n{page}"
    run.__name__, run.__doc__ = fn.__name__, fn.__doc__
    return run


@gateway.tool("goto", "read")
@_guarded
def goto(ctx: Any, path: str) -> str:
    """Open a page of the company app by path (or same-origin URL)."""
    return ctx.browser.goto(path)


@gateway.tool("read_page", "read")
@_guarded
def read_page(ctx: Any) -> str:
    """Snapshot of the current page."""
    return ctx.browser.snapshot()


@gateway.tool("click", "write")
@_guarded
def click(ctx: Any, target: str) -> str:
    """Click a visible button, link or menu by its name; record-form submits are guarded."""
    return ctx.browser.click(target, str(getattr(ctx, "run_id", "") or ""))


@gateway.tool("fill_form", "write")
@_guarded
def fill_form(ctx: Any, fields: dict) -> str:
    """Fill form fields by label or name."""
    return ctx.browser.fill_form(fields)


TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "goto",
        "description": "Open a page of the company app, e.g. /inbox, /inbox/3, /invoices, /invoices/new, /notify. "
                       "Returns the status line, any DETECTED problems and a snapshot of the page.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path on the company app, starting with /"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "read_page",
        "description": "Snapshot of the current page: PAGE, ALERT, TEXT, LINKS, each FORM with its fields "
                       "(label [name] = value) and buttons, OTHER BUTTONS.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "fill_form",
        "description": "Type values into form fields, keyed by the field label or name from the snapshot. "
                       "Does not submit. Returns the form's current values.",
        "parameters": {"type": "object", "properties": {
            "fields": {"type": "object", "additionalProperties": {"type": "string"},
                       "description": 'Label or name -> value, e.g. {"Vendor": "Acme Corp", "Amount": "1240.00"}'}},
            "required": ["fields"]}}},
    {"type": "function", "function": {
        "name": "click",
        "description": "Click a visible button, link or menu (e.g. 'More actions') by its exact visible name. "
                       "Submitting a record form is only allowed when its values match a proposal you made "
                       "with propose; otherwise it returns BLOCKED and nothing is submitted. A successful "
                       "submit is checked against the system of record and reported as VERIFIED.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string", "description": "Visible name of the button, link or menu"}},
            "required": ["target"]}}},
]
