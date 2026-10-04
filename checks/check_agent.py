"""Offline unit check for agent.py: no Groq, no browser.

Runs agent.run() in-process against checks/stub_company.py with a scripted fake LLM and a fake
browser tool set, and drives agent.ModelPool with a fake Groq client and a fake clock.
Prints AGENT UNIT VERIFIED last, only after every assertion passed.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from checks._servers import ROOT, check, fail, isolated_env, stub_app  # noqa: E402

EVENT_TYPES = {"run_started", "contract_proposed", "contract_approved", "contract_rejected", "referee_pre", "step",
               "observation", "detected", "recovery", "verified", "proposal", "parked", "decision", "llm", "referee",
               "receipt", "run_finished", "error",
               "gateway"}  # gateway.dispatch logs its own refusals as type "gateway"
ACME = json.loads((ROOT / "contracts" / "acme_invoice.json").read_text(encoding="utf-8"))
INVOICE = {"vendor": "Acme Corp", "invoice_no": "INV-1042", "amount": "1240.00", "due_date": "2026-11-02"}
UMBRELLA = {"vendor": "Umbrella Corp", "invoice_no": "INV-UC-5521", "amount": "8200.00", "due_date": "2026-11-15"}
NOTE = {"to": "alex@company.test", "subject": "INV-1042 entered", "body": "Acme Corp invoice INV-1042 is in the AP system."}


# ---------------------------------------------------------------- fakes

class ScriptedLLM:
    """Plays a list of turns. A turn is a list of (tool, args), a text reply, or fn(messages) -> either."""

    models = ["fake-llm"]

    def __init__(self, turns: list) -> None:
        self.turns, self.seen, self.choices = list(turns), [], []

    def chat(self, messages: list[dict], tools: list[dict], max_tokens: int = 0, tool_choice: str = "auto") -> dict:
        import agent
        self.seen.append((copy.deepcopy(messages), [t["function"]["name"] for t in tools]))
        self.choices.append(tool_choice)
        if not self.turns:
            fail(f"fake LLM ran out of turns (call {len(self.seen)})")
        turn = self.turns.pop(0)
        if callable(turn):
            turn = turn(messages)
        calls = ([{"id": f"c{len(self.seen)}-{i}", "name": n, "arguments": json.dumps(a)} for i, (n, a) in enumerate(turn)]
                 if isinstance(turn, list) else [])
        return {"content": turn if isinstance(turn, str) else "", "tool_calls": calls, "model": "fake-llm",
                "usage": {"prompt_tokens": agent.est_tokens(messages, tools), "completion_tokens": 25}, "wait_s": 0}


class FakeBrowser:
    def __init__(self, base_url: str, headless: bool = True, video_dir: str | None = None) -> None:
        self.base, self.started, self.closed, self.page, self.form = base_url, False, False, "/", {}
        self.logged_in = False
        FAKE.instances.append(self)

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def snapshot(self) -> str:
        form = {"/invoices/new": ("invoice", INVOICE), "/notify": ("notification", NOTE)}.get(self.page)
        lines = [f"PAGE {self.page} (HTTP 200)"]
        if form:
            lines.append(f"FORM record={form[0]} action={self.page}")
            lines += [f"  - {k.title()} [{k}] = \"{self.form.get(k, '')}\"" for k in form[1]] + ["  buttons: Save"]
        return "\n".join(lines)


def _schema(name: str, props: dict) -> dict:
    return {"type": "function", "function": {"name": name, "description": name,
                                             "parameters": {"type": "object", "properties": props, "required": list(props)}}}


FAKE = SimpleNamespace(Browser=FakeBrowser, instances=[], TOOL_SCHEMAS=[
    _schema("goto", {"path": {"type": "string"}}), _schema("read_page", {}),
    _schema("click", {"target": {"type": "string"}}), _schema("fill_form", {"fields": {"type": "object"}})])


def register_fake_browser_tools() -> None:
    import gateway

    @gateway.tool("goto", "read")
    def goto(ctx, path: str) -> str:
        b = ctx.browser
        if not b.logged_in:
            b.page = "/login"
            return f"OK: PAGE /login?next={path} (HTTP 200) - Sign in\nDETECTED: session_expired (redirected to login)"
        b.page, b.form = path, {}
        return f"OK: PAGE {path} (HTTP 200)"

    @gateway.tool("read_page", "read")
    def read_page(ctx) -> str:
        return "OK: " + ctx.browser.snapshot()

    @gateway.tool("fill_form", "write")
    def fill_form(ctx, fields: dict) -> str:
        ctx.browser.form.update(fields)
        return f"OK: filled {len(fields)} fields"

    @gateway.tool("click", "write")
    def click(ctx, target: str) -> str:
        b = ctx.browser
        if target == "Sign in":
            b.logged_in = b.form.get("password") == "receipts123"
            b.page = "/"
            return "OK: PAGE / (HTTP 200) - Company portal" if b.logged_in else "ERROR: wrong password"
        b.page = "/done"
        kind, hook = {"Save": ("invoice", "/stub/invoices"), "Send": ("notification", "/stub/outbox")}.get(target, (None, None))
        if kind is None:
            return f'ERROR: no clickable element named "{target}"\nDETECTED: element_not_found'
        guard = gateway.guard_submit(ctx.run_id, kind, {k: {"value": v, "hidden": False} for k, v in b.form.items()})
        if not guard["allowed"]:
            return f"BLOCKED: {guard['reason']} (rule {guard['rule_id']})"
        row = dict(b.form) | ({"amount": float(b.form["amount"])} if kind == "invoice" else {})
        httpx.post(ctx.app_url + hook, json=row, timeout=10).raise_for_status()
        done = gateway.confirm(guard["proposal_id"])
        return "OK: PAGE /done (HTTP 200)" + (f"\nVERIFIED: {done['evidence']}" if done["status"] == "DONE" else "")


# ---------------------------------------------------------------- helpers

def events(env: dict, run_id: str) -> list[dict]:
    path = Path(env["RECEIPTS_RUNS"]) / run_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def receipt(env: dict, run_id: str) -> dict:
    return json.loads((Path(env["RECEIPTS_RUNS"]) / run_id / "receipt.json").read_text(encoding="utf-8"))


def schema_ok(evs: list[dict]) -> bool:
    return all(isinstance(e.get("ts"), float) and e.get("type") in EVENT_TYPES for e in evs)


def http_error(status: int, headers: dict | None = None, body: dict | None = None):
    import openai
    try:
        import httpx2 as hx  # openai 3.x ships its own httpx fork
    except ImportError:
        hx = httpx
    body = body or {"message": "error"}
    resp = hx.Response(status, headers=headers or {}, request=hx.Request("POST", "https://groq.test/chat"), json=body)
    cls = {429: openai.RateLimitError, 400: openai.BadRequestError}.get(status, openai.APIStatusError)
    return cls(f"Error code: {status} - {json.dumps(body)}", response=resp, body=body)


def completion(name: str = "finish") -> SimpleNamespace:
    tc = SimpleNamespace(id="t1", function=SimpleNamespace(name=name, arguments='{"summary": "ok"}'))
    msg = SimpleNamespace(content="", tool_calls=[tc])
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")],
                           usage=SimpleNamespace(prompt_tokens=120, completion_tokens=12))


class FakeGroq:
    """create(**kw) stand-in: per-model queues of errors; records every call."""

    def __init__(self, errors: dict[str, list], headers: dict | None = None) -> None:
        self.errors, self.headers, self.calls = errors, headers or {}, []

    def __call__(self, **kw):
        self.calls.append(kw)
        queue = self.errors.get(kw["model"], [])
        if queue:
            raise queue.pop(0)
        return completion(), dict(self.headers)


def pool(create: FakeGroq):
    import agent
    clock, sleeps = [1000.0], []

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock[0] += s
    return agent.ModelPool(create=create, sleep=sleep, clock=lambda: clock[0]), sleeps


# ---------------------------------------------------------------- checks

def check_pure() -> None:
    import agent
    a = agent.parse_args(["do it", "--contract", "c.json", "--yes", "--approve-contract", "ui", "--decide", "reject",
                          "--run-id", "r1", "--headed", "--video", "--max-steps", "7", "--contract-only"])
    check((a.task, a.contract, a.yes, a.approve_contract, a.decide, a.run_id, a.headed, a.video, a.max_steps, a.contract_only)
          == ("do it", "c.json", True, "ui", "reject", "r1", True, True, 7, True), "CLI parses every flag")
    d = agent.parse_args(["t"])
    check((d.contract, d.yes, d.approve_contract, d.decide, d.max_steps, d.contract_only) == (None, False, None, None, 40, False),
          "CLI defaults (max-steps 40)")
    for bad in (["t", "--decide", "maybe"], ["t", "--approve-contract", "email"], ["t", "--max-steps", "0"]):
        try:
            agent.parse_args(bad)
            fail(f"bad CLI accepted: {bad}")
        except SystemExit:
            pass
    check(True, "CLI rejects bad choices")
    check(re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", agent.new_run_id()), "run id is YYYYMMDD-HHMMSS-hhhh")
    check([agent.parse_duration(x) for x in ("7.66s", "1m26.4s", "120ms", "2", "1h", None, "soon")]
          == [7.66, 86.4, 0.12, 2.0, 3600.0, None, None], "Groq duration formats parse")
    check(agent._mask({"fields": {"username": "clerk", "password": "x"}}) == {"fields": {"username": "clerk", "password": "********"}},
          "passwords are masked in logged args")

    big = "PAGE /x (HTTP 200)\nALERT: something\nDETECTED: server_error (HTTP 500)\n" + "TEXT " + "lorem ipsum " * 260
    msgs = [{"role": "system", "content": "S" * 2000}, {"role": "user", "content": "TASK: x"}]
    for i in range(30):
        msgs.append({"role": "assistant", "content": "thinking " * 80,
                     "tool_calls": [{"id": f"id{i}", "type": "function", "function": {"name": "goto", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"id{i}", "content": f"#{i} " + big})
    before = agent.est_tokens(msgs)
    out = agent.compact(msgs, 3200)
    check(before > 20000 and agent.est_tokens(out) <= 3200, f"history elision: {before} -> {agent.est_tokens(out)} tokens (budget 3200)")
    check(out[:2] == msgs[:2] and msgs[-1]["content"].startswith("#29"), "system prompt and task survive; input not mutated")
    tools = [m for m in out if m["role"] == "tool"]
    check(tools[-1]["content"] == msgs[-1]["content"] and tools[-2]["content"] == msgs[-3]["content"], "last 2 tool results kept in full")
    check(any("omitted" in str(m.get("content")) for m in out), "dropped turns are announced")
    light = agent.compact(msgs[:12], 100000)
    check(light[3]["content"].splitlines()[:3] == ["#0 PAGE /x (HTTP 200)", "ALERT: something", "DETECTED: server_error (HTTP 500)"]
          and len(light[3]["content"]) < 300, "older tool results collapse to first line + key lines")
    for i, m in enumerate(out):
        if m["role"] == "tool":
            j = i - 1
            while out[j]["role"] == "tool":
                j -= 1
            ids = [c["id"] for c in out[j].get("tool_calls", [])]
            if m["tool_call_id"] not in ids:
                fail(f"tool result {m['tool_call_id']} lost its assistant call after elision")
    check(True, "every kept tool result still follows the call that made it")


def check_pool() -> None:
    import agent
    m0, m1, m2 = agent.MODELS
    g = FakeGroq({m0: [http_error(429, {"retry-after": "7.66s"})]})
    p, sleeps = pool(g)
    r = p.chat([{"role": "user", "content": "hi"}], [], max_tokens=100)
    check(r["model"] == m1 and sleeps == [] and [c["model"] for c in g.calls] == [m0, m1],
          "429 on one model rotates to the next without sleeping")
    check(g.calls[0]["reasoning_effort"] == "low" and g.calls[1]["reasoning_effort"] == "none"
          and g.calls[0]["max_tokens"] <= 1200, "gpt-oss gets reasoning_effort=low, qwen none; max_tokens <= 1200")
    r = p.chat([{"role": "user", "content": "again"}], [], max_tokens=100)
    check(r["model"] == m1 and sleeps == [], "a cooling model is skipped until its retry-after passes")

    g = FakeGroq({m0: [http_error(429, {"retry-after": "2"})], m1: [http_error(429, {"x-ratelimit-reset-tokens": "1m26.4s"})],
                  m2: [http_error(429, body={"message": "Rate limit reached. Please try again in 3.5s."})]})
    p, sleeps = pool(g)
    r = p.chat([{"role": "user", "content": "hi"}], [], max_tokens=100)
    check(len(sleeps) == 1 and 2.0 <= sleeps[0] < 3.0 and r["model"] == m0 and r["wait_s"] >= 2.0,
          f"sleeps once, only when every model is cooling, for the shortest cooldown ({sleeps})")

    g = FakeGroq({}, headers={"x-ratelimit-remaining-tokens": "300", "x-ratelimit-limit-tokens": "8000"})
    p, sleeps = pool(g)
    p.chat([{"role": "user", "content": "hi"}], [], max_tokens=700)
    r = p.chat([{"role": "user", "content": "hi"}], [], max_tokens=700)
    check(r["model"] == m1 and sleeps == [], "tokens-per-minute pacing moves to a model with headroom before a 429")

    g = FakeGroq({m: [http_error(429, {"retry-after": "3600"})] for m in agent.MODELS})
    p, sleeps = pool(g)
    try:
        p.chat([{"role": "user", "content": "hi"}], [])
        fail("daily cap did not raise")
    except agent.LLMUnavailable as e:
        check(sleeps == [] and "rate-limited" in str(e), "daily-cap style limits stop with LLMUnavailable instead of sleeping")

    tuf = http_error(400, body={"message": "Failed to call a function.", "code": "tool_use_failed"})
    g = FakeGroq({m0: [tuf]})
    p, _ = pool(g)
    r = p.chat([{"role": "user", "content": "hi"}], [])
    check(len(g.calls) == 2 and "not a valid tool call" in g.calls[1]["messages"][-1]["content"]
          and r["tool_calls"][0]["name"] == "finish", "tool_use_failed is retried with a corrective message")
    g = FakeGroq({m0: [http_error(400, body={"message": "x", "code": "tool_use_failed"}) for _ in range(3)]})
    p, _ = pool(g)
    r = p.chat([{"role": "user", "content": "hi"}], [])
    check(r["tool_calls"][0]["name"] == "finish" and g.calls[-1]["model"] != m0,
          "a model that keeps producing malformed calls hands the turn to another model")
    parse = http_error(400, body={"message": "Parsing failed. The model generated output that could not be parsed.",
                                  "type": "invalid_request_error", "failed_generation": "<|call|>"})
    g = FakeGroq({m: [parse for _ in range(5)] for m in agent.MODELS})
    p, _ = pool(g)
    try:
        p.chat([{"role": "user", "content": "hi"}], [])
        fail("malformed generations retried forever")
    except Exception as e:
        check(len(g.calls) == 5 and "Parsing failed" in str(e),
              "'Parsing failed' generations are retried across models, at most 4 times, then raised")
    g = FakeGroq({m0: [http_error(413, body={"message": "Request too large"})]})
    p, _ = pool(g)
    try:
        p.chat([{"role": "user", "content": "hi"}], [])
        fail("413 not surfaced")
    except agent.TooLarge:
        check(True, "413 surfaces as TooLarge so the caller elides harder")


def check_contract_only(env: dict) -> None:
    import agent
    turns = [
        [("http_get", {"path": "/api/inbox/3"})],
        lambda m: (m[-1]["role"] == "tool" and "INV-1042" in m[-1]["content"] or fail("http_get result not fed back"))
        and [("submit_contract", {"clauses": [{"kind": "row_count", "path": "/ledger", "equals": 1}]})],
        lambda m: ("invalid" in m[-1]["content"] or fail("validation errors not fed back"))
        and [("submit_contract", {"clauses": [{"kind": "row_count", "path": "/api/nope", "equals": 1}]})],
        lambda m: ("HTTP 404" in m[-1]["content"] or fail("dead endpoint not reported"))
        and [("submit_contract", {"clauses": ACME[:3] + [{**ACME[3], "text": "Invoice INV-1042 has been entered into the AP system"}]})],
        lambda m: ("short distinctive key" in m[-1]["content"] or fail("sentence-long outbox text not rejected"))
        and [("submit_contract", {"clauses": ACME})],
    ]
    llm = ScriptedLLM(turns)
    code = agent.run(["Enter the Acme invoice", "--contract-only", "--run-id", "t-contract"], llm=llm)
    first = llm.seen[0][0]
    check(code == 0, "--contract-only exits 0")
    check("GET /api/ledger?vendor=&invoice_no=" in first[1]["content"] and "alex@company.test" in first[1]["content"]
          and "/api/health" not in first[1]["content"], "contract prompt lists GET endpoints with params and the user's email")
    check(llm.seen[0][1] == ["http_get", "submit_contract"], "contract phase offers only http_get and submit_contract")
    out = Path(env["RECEIPTS_RUNS"]) / "t-contract"
    check(json.loads((out / "contract.json").read_text(encoding="utf-8")) == ACME, "contract.json holds the validated contract")
    ev = events(env, "t-contract")
    check(schema_ok(ev) and [e["type"] for e in ev if e["type"] != "llm"] == ["run_started", "contract_proposed", "run_finished"]
          and ev[-1]["status"] == "CONTRACT_ONLY" and len(next(e for e in ev if e["type"] == "contract_proposed")["english"]) == 4,
          "contract-only events: run_started, contract_proposed, run_finished CONTRACT_ONLY")
    check(sum(e["type"] == "llm" for e in ev) == 5 and not (out / "receipt.json").exists(), "5 LLM calls logged; no receipt, no action")


def check_already_done(env: dict, base: str) -> None:
    import agent
    httpx.post(f"{base}/stub/invoices", json={**INVOICE, "amount": 1240.0}, timeout=10).raise_for_status()
    httpx.post(f"{base}/stub/outbox", json=NOTE, timeout=10).raise_for_status()
    llm = ScriptedLLM([])
    code = agent.run(["Enter the Acme invoice", "--contract", str(ROOT / "contracts" / "acme_invoice.json"), "--yes",
                      "--run-id", "t-already", "--decide", "reject"], llm=llm, browser_mod=FAKE)
    r, ev = receipt(env, "t-already"), events(env, "t-already")
    check(code == 0 and r["status"] == "ALREADY_DONE", "red-before-green: already-true contract -> ALREADY_DONE, exit 0")
    check(not llm.seen and not FAKE.instances and not any(e["type"] == "step" for e in ev),
          "ALREADY_DONE takes no step, calls no LLM, opens no browser")
    check(schema_ok(ev) and next(e for e in ev if e["type"] == "referee_pre")["all_passed"] is True
          and all(c["passed"] for c in r["contract"]), "referee_pre all_passed and the receipt shows every clause passing")
    httpx.post(f"{base}/api/reset", json={}, timeout=10).raise_for_status()


def check_scripted_run(env: dict, base: str, tmp: Path) -> None:
    import agent
    import gateway
    clauses = ACME[:2] + [ACME[3], {"kind": "row_count", "path": "/api/ledger?invoice_no=INV-UC-5521", "equals": 1,
                                    "ref": "INV-UC-5521", "description": "Umbrella invoice entered"}]
    cpath = tmp / "scripted_contract.json"
    cpath.write_text(json.dumps(clauses), encoding="utf-8")

    def after_feedback(m: list[dict]) -> list:
        if "NOT true yet" not in m[-1]["content"] or "alex@company.test" not in m[-1]["content"]:
            fail(f"referee failure not fed back: {m[-1]['content'][:200]}")
        if "Notify form fields" not in m[0]["content"] or "INV-1042" not in m[0]["content"]:
            fail("site memory / notes missing from the system prompt")
        return [("goto", {"path": "/notify"}), ("propose", {"kind": "notification", "fields": NOTE}),
                ("fill_form", {"fields": NOTE}), ("click", {"target": "Send"})]

    turns = [
        [("goto", {"path": "/invoices/new"})],
        [("fill_form", {"fields": {"username": "clerk", "password": "receipts123"}}), ("click", {"target": "Sign in"})],
        [("goto", {"path": "/invoices/new"}), ("propose", {"kind": "invoice", "fields": INVOICE, "source": "inbox:3"}),
         ("fill_form", {"fields": INVOICE}), ("click", {"target": "Save"})],
        lambda m: ("ALLOWED" in str(m) or fail("propose guidance missing"))
        and [("propose", {"kind": "invoice", "fields": UMBRELLA, "source": "inbox:7"}), ("goto", {"path": "/invoices/new"}),
             ("propose", {"kind": "invoice", "fields": {"Invoice No.": "INV-UC-5521"}, "source": "inbox:7"}),
             ("propose", {"kind": "invoice", "fields": UMBRELLA, "source": "inbox:7"}), ("no_such_tool", {})],
        "I think I am done.",
        lambda m: ("Use a tool" in m[-1]["content"] or fail("no nudge after a reply without tool calls"))
        and [("remember_site", {"note": "Notify form fields: to, subject, body"}), ("note", {"key": "invoice", "value": "INV-1042"}),
             ("finish", {"summary": "Entered INV-1042."})],
        after_feedback,
        [("finish", {"summary": "Entered INV-1042 and told Alex; the Umbrella invoice was rejected.",
                     "site_notes": ["Invoice form submit button is Save"]})],
    ]
    llm = ScriptedLLM(turns)
    code = agent.run(["Enter the Acme invoice and tell me", "--contract", str(cpath), "--yes", "--decide", "reject",
                      "--run-id", "t-run", "--max-steps", "12"], llm=llm, browser_mod=FAKE)
    r, ev = receipt(env, "t-run"), events(env, "t-run")
    types = [e["type"] for e in ev]
    check(code == 0 and r["status"] == "VERIFIED_WITH_EXCEPTIONS", f"scripted run ends VERIFIED_WITH_EXCEPTIONS, exit 0 (got {r['status']}, {code})")
    check(set(llm.choices) == {"required"}, "every work call is sent with tool_choice=required")
    obs = [e["summary"] for e in ev if e["type"] == "observation" and e["tool"] == "propose" and e["status"] == "ERROR"]
    check(len(obs) == 2 and "no FORM record=invoice" in obs[0] and "Use its field names exactly: vendor, invoice_no" in obs[1],
          "propose is refused (before the gateway) off the form page or with keys that are not the form's fields")
    check(len(r["not_done"]) == 1, "refused proposals never reach the gateway or the receipt")
    check(not llm.turns and FAKE.instances[-1].started and FAKE.instances[-1].closed, "every scripted turn used; browser started and closed")
    tool_names = set(llm.seen[0][1])
    check({"goto", "click", "fill_form", "read_page", "http_get", "note", "propose", "wait_for_decisions", "remember_site", "finish"}
          <= tool_names, "work loop offers browser tools + agent tools")
    check(schema_ok(ev), "every event has a float ts and a contract type")
    for t in ("run_started", "contract_proposed", "contract_approved", "referee_pre", "step", "observation", "detected",
              "recovery", "verified", "proposal", "parked", "decision", "llm", "referee", "receipt", "run_finished"):
        check(t in types, f"event {t} emitted")
    check(types.index("receipt") < types.index("run_finished") == len(types) - 1, "receipt then run_finished last")
    rec = next(e for e in ev if e["type"] == "recovery")
    check(rec["tool"] == "fill_form" and rec["n"] == 2, "recovery logged for the first call after a detection")
    login = next(e for e in ev if e["type"] == "step" and e["tool"] == "fill_form")
    check(login["args"]["fields"]["password"] == "********" and login["tier"] == "write", "login password masked in step event")
    steps = [e for e in ev if e["type"] == "step"]
    check([e["n"] for e in steps] == list(range(1, len(steps) + 1)) and r["stats"]["steps"] == len(steps), "steps numbered 1..n")
    refs = [e for e in ev if e["type"] == "referee"]
    check([e["status"] for e in refs] == ["FAILED", "VERIFIED_WITH_EXCEPTIONS"] and [e["round"] for e in refs] == [1, 2],
          "referee round 1 FAILED (no message yet) fed back; round 2 passes")
    dec = [e for e in ev if e["type"] == "decision"]
    check(len(dec) == 1 and dec[0]["decision"] == "reject" and dec[0]["by"] == "auto-reject", "parked item auto-rejected once")
    check(any(e["type"] == "gateway" and e["tool"] == "no_such_tool" for e in ev), "unknown tool refused by the gateway, run continues")
    nd = {p["ref"]: p for p in r["not_done"]}
    check(nd.get("INV-UC-5521", {}).get("status") == "REJECTED" and nd["INV-UC-5521"]["rule_id"] == "over-threshold",
          "receipt not_done lists INV-UC-5521 REJECTED with rule over-threshold")
    check(sorted(p["ref"] for p in r["done"]) == ["INV-1042", "notify:alex@company.test"] and r["waiting"] == [],
          "receipt done lists the invoice and the notification")
    check(len(r["exceptions"]) == 1 and r["exceptions"][0]["status"] == "REJECTED" and r["unexpected_changes"] == [],
          "exception explains the rejected clause; no unexpected changes")
    check(all(c["passed"] for c in r["contract"][:3]) and not r["contract"][3]["passed"] and r["contract"][0]["english"]
          and r["contract"][0]["evidence"]["request"].startswith("GET /api/ledger"), "receipt contract has english, observed, evidence")
    st = r["stats"]
    check(st["llm_calls"] == 8 == types.count("llm") and st["models"] == ["fake-llm"] and st["prompt_tokens"] > 0,
          "receipt stats count LLM calls and tokens")
    check(r["notes"] == {"invoice": "INV-1042"} and "told Alex" in r["summary"], "notes and the LLM summary kept separately")
    md = (Path(env["RECEIPTS_RUNS"]) / "t-run" / "receipt.md").read_text(encoding="utf-8")
    check("Deliberately NOT done" in md and "over-threshold" in md and "✓" in md and "✗" in md and "GET /api/outbox" in md,
          "receipt.md is readable: checklist, not-done with rule, evidence")
    check(len(httpx.get(f"{base}/api/ledger", params={"invoice_no": "INV-1042"}, timeout=10).json()) == 1
          and not httpx.get(f"{base}/api/ledger", params={"invoice_no": "INV-UC-5521"}, timeout=10).json(),
          "system of record: INV-1042 once, rejected invoice absent")
    check(any(p["status"] == "REJECTED" for p in gateway.proposals("t-run")), "gateway holds the human-style decision")

    mem = agent.load_memory()
    check([m["note"] for m in mem] == ["Notify form fields: to, subject, body", "Invoice form submit button is Save"] and isinstance(mem[0]["ts"], float)
          and Path(env["RECEIPTS_MEMORY"]).exists(), "remember_site and finish(site_notes) persisted {note, ts} to RECEIPTS_MEMORY")
    check(not agent.remember("Notify form fields: to, subject, body"), "duplicate site notes are not stored twice")
    for i in range(12):
        agent.remember(f"quirk {i}")
    prompt = agent.work_system_prompt(clauses, agent.load_workspace(), {}, agent.load_memory())
    check("quirk 11" in prompt and "quirk 2" in prompt and "quirk 1\n" not in prompt and "Notify form fields" not in prompt
          and "clerk" in prompt and "Exactly 1 ledger row for INV-1042" in prompt,
          "next system prompt carries the latest 10 site notes, vault and the contract in English")


def check_budget(env: dict) -> None:
    import agent
    clauses = [{"kind": "row_count", "path": "/api/ledger?invoice_no=INV-NEVER", "equals": 1, "ref": "INV-NEVER"}]
    cpath = Path(env["RECEIPTS_RUNS"]).parent / "never.json"
    cpath.write_text(json.dumps(clauses), encoding="utf-8")
    llm = ScriptedLLM([[("read_page", {})]] * 3)
    code = agent.run(["x", "--contract", str(cpath), "--yes", "--run-id", "t-budget", "--max-steps", "3", "--decide", "wait"],
                     llm=llm, browser_mod=FAKE)
    r, ev = receipt(env, "t-budget"), events(env, "t-budget")
    check(code == 1 and r["status"] == "FAILED" and any(e["type"] == "referee" for e in ev) and ev[-1]["status"] == "FAILED",
          "step budget exhausted -> referee still runs, FAILED receipt, exit 1")

    class Boom:
        models = ["boom"]

        def chat(self, *a, **k):
            raise RuntimeError("model exploded")
    code = agent.run(["x", "--contract", str(cpath), "--yes", "--run-id", "t-error"], llm=Boom(), browser_mod=FAKE)
    r, ev = receipt(env, "t-error"), events(env, "t-error")
    check(code == 2 and r["status"] == "ERROR" and any(e["type"] == "error" for e in ev) and ev[-1]["type"] == "run_finished"
          and FAKE.instances[-1].closed, "an exception still writes an ERROR receipt + run_finished, exit 2, browser closed")


def main() -> None:
    env = isolated_env()
    os.environ.update(env)
    os.environ.pop("RECEIPTS_CRASH_AFTER_SUBMIT", None)
    with stub_app(env=env) as base:
        os.environ["RECEIPTS_APP_URL"] = base
        import agent  # noqa: F401  (registers the agent tools)
        register_fake_browser_tools()
        check_pure()
        check_pool()
        check_contract_only(env)
        check_already_done(env, base)
        check_scripted_run(env, base, Path(env["RECEIPTS_RUNS"]).parent)
        check_budget(env)
    print("AGENT UNIT VERIFIED")


if __name__ == "__main__":
    main()
