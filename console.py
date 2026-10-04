"""Operator console for Receipts (docs/CONTRACT.md section 6).

Starts agent runs as child processes, streams their events.jsonl to a single inline web page,
relays contract approval and parked-action decisions, and shows the final receipt.
Run with: .venv\\Scripts\\python -m uvicorn console:app --port 8100
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import gateway

ROOT = Path(__file__).resolve().parent
APP_URL = os.environ.get("RECEIPTS_APP_URL", "http://127.0.0.1:8000").rstrip("/")
# Where a human's browser reaches the company app (differs from APP_URL behind a proxy, e.g. on a HF Space).
PUBLIC_APP_URL = os.environ.get("RECEIPTS_PUBLIC_APP_URL", APP_URL).rstrip("/")
RUNS = ROOT / os.environ.get("RECEIPTS_RUNS", "runs")
RUN_ID_RE = re.compile(r"[0-9A-Za-z_-]{1,64}")

PRESETS = [
    {"id": "A", "title": "Enter Acme's latest invoice",
     "task": "Find the latest invoice from Acme Corp, extract the amount and due date, enter it into our AP system, "
             "and tell me once it is done."},
    {"id": "B", "title": "Process the whole AP inbox",
     "task": "Process every unprocessed vendor invoice in the AP inbox into the AP system, "
             "then send me a short summary."},
    {"id": "C", "title": "Tag PII in the data catalog",
     "task": "Find who owns the customers table in the data catalog, tag its email column as PII, "
             "and let the owner know."},
]

PROCS: dict[str, subprocess.Popen] = {}
CRASH_LOGGED: set[str] = set()
LOCK = threading.Lock()

app = FastAPI(title="Receipts console")


class RunIn(BaseModel):
    task: str


class ContractIn(BaseModel):
    action: Literal["approve", "reject"]
    clauses: list[dict] | None = None


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    always: bool = False


def agent_cmd() -> list[str]:
    """The agent command prefix from RECEIPTS_AGENT_CMD (JSON list or shell string)."""
    raw = os.environ.get("RECEIPTS_AGENT_CMD", "").strip()
    if not raw:
        return [sys.executable, "agent.py"]
    if raw.startswith("["):
        return [str(part) for part in json.loads(raw)]
    # CONTRACT: a non-JSON value is shell-split; on Windows without POSIX escapes so backslash paths survive,
    # then one layer of surrounding quotes is removed from each token.
    parts = shlex.split(raw, posix=os.name != "nt")
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]


def run_dir(run_id: str) -> Path:
    """Directory of a known run, or 404."""
    if not RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(404, "no such run")
    d = RUNS / run_id
    if not d.is_dir() and run_id not in PROCS:
        raise HTTPException(404, "no such run")
    return d


def read_events(d: Path) -> list[dict]:
    """Complete lines of events.jsonl; a trailing partial line (writer mid-flush) is left for the next poll."""
    try:
        data = (d / "events.jsonl").read_bytes()
    except FileNotFoundError:
        return []
    events = []
    for line in data.split(b"\n")[:-1]:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            events.append({"ts": time.time(), "type": "error", "message": "unreadable line in events.jsonl"})
    return events


def log_tail(d: Path, limit: int = 300) -> str:
    try:
        text = (d / "agent.log").read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return ""
    return text[-limit:]


def append_event(d: Path, event: dict) -> None:
    path = d / "events.jsonl"
    prefix = ""
    if path.exists() and path.stat().st_size and not path.read_bytes().endswith(b"\n"):
        prefix = "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(prefix + json.dumps(event) + "\n")


def write_json_atomic(path: Path, data: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def page() -> str:
    return PAGE


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/presets")
def presets() -> list[dict]:
    return PRESETS


@app.post("/api/runs")
def start_run(body: RunIn) -> dict:
    task = body.task.strip()
    if not task:
        raise HTTPException(422, "task is empty")
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    d = RUNS / run_id
    d.mkdir(parents=True, exist_ok=True)
    try:
        cmd = agent_cmd() + [task, "--run-id", run_id, "--decide", "ui", "--approve-contract", "ui"]
    except ValueError as e:
        raise HTTPException(500, f"RECEIPTS_AGENT_CMD is not valid JSON: {e}") from e
    with (d / "agent.log").open("ab") as log:
        try:
            proc = subprocess.Popen(cmd, cwd=ROOT, env={"PYTHONIOENCODING": "utf-8", **os.environ},
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        except OSError as e:
            raise HTTPException(500, f"could not start agent {cmd[0]!r}: {e}") from e
    with LOCK:
        PROCS[run_id] = proc
    return {"run_id": run_id}


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str, after: int = 0) -> dict:
    d = run_dir(run_id)
    with LOCK:
        proc = PROCS.get(run_id)
        exited = proc is not None and proc.poll() is not None
        events = read_events(d)  # read after poll(): an exited writer has flushed everything
        done = any(e.get("type") == "run_finished" for e in events)
        if exited and not done and run_id not in CRASH_LOGGED:
            tail = log_tail(d)
            event = {"ts": time.time(), "type": "error",
                     "message": f"agent process exited with code {proc.returncode} before finishing"
                                + (f": {tail}" if tail else "")}
            append_event(d, event)
            events.append(event)
            CRASH_LOGGED.add(run_id)
    return {"events": events[max(after, 0):], "next": len(events), "finished": done or exited}


@app.post("/api/runs/{run_id}/contract")
def contract_decision(run_id: str, body: ContractIn) -> dict:
    d = run_dir(run_id)
    decision: dict = {"action": body.action}
    if body.clauses is not None:
        decision["clauses"] = body.clauses
    write_json_atomic(d / "contract_decision.json", decision)
    return {"ok": True, **decision}


@app.get("/api/runs/{run_id}/receipt")
def receipt(run_id: str) -> dict:
    d = run_dir(run_id)
    try:
        return json.loads((d / "receipt.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "no receipt yet") from None


@app.get("/api/parked")
def parked(run_id: str | None = None) -> list[dict]:
    return gateway.pending(run_id or None)


@app.post("/api/parked/{proposal_id}")
def decide(proposal_id: str, body: DecisionIn) -> dict:
    try:
        return gateway.decide(proposal_id, body.decision, by="human", always=body.always)
    except Exception as e:  # unknown or already-decided proposal: report, don't 500
        raise HTTPException(409, f"{type(e).__name__}: {e}") from e


@app.post("/api/reset")
def reset() -> dict:
    # CONTRACT: reset also stops agent runs this console started, so a live agent cannot write into fresh state.
    stopped = []
    with LOCK:
        for run_id, proc in PROCS.items():
            if proc.poll() is None:
                proc.terminate()
                stopped.append(run_id)
    for run_id in stopped:
        try:
            PROCS[run_id].wait(timeout=5)
        except subprocess.TimeoutExpired:
            PROCS[run_id].kill()
    try:
        r = httpx.post(f"{APP_URL}/api/reset", json={}, timeout=10)
        r.raise_for_status()
        app_status = "ok"
    except httpx.HTTPError as e:
        app_status = f"error: {type(e).__name__}: {e}"
    gateway.reset_db()
    return {"app": app_status, "gateway": "ok", "stopped_runs": stopped}


@app.get("/api/hazards")
def hazards() -> list:
    try:
        r = httpx.get(f"{APP_URL}/api/chaos/log", timeout=3)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"company app unreachable: {type(e).__name__}") from e
    if r.status_code != 200:
        return []  # an app without a chaos log simply has no hazards
    try:
        data = r.json()
    except ValueError:
        return []
    return data if isinstance(data, list) else []


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Receipts console</title>
<style>
:root{
  --bg:#f4f5f7;--card:#fff;--line:#e3e6eb;--ink:#141a23;--muted:#5d6675;--soft:#8a93a3;
  --green:#067647;--green-bg:#ecfdf3;--green-line:#abefc6;
  --amber:#b54708;--amber-bg:#fffaeb;--amber-line:#fedf89;
  --blue:#175cd3;--blue-bg:#eff8ff;--blue-line:#b2ddff;
  --red:#b42318;--red-bg:#fef3f2;--red-line:#fecdca;
  --purple:#6927da;--purple-bg:#f4f3ff;--purple-line:#d9d6fe;
  --orange:#c4320a;--orange-bg:#fff6ed;--orange-line:#f9dbaf;
  --mono:ui-monospace,"Cascadia Mono",Consolas,SFMono-Regular,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header.top{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 28px;background:#101828;color:#fff;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.logo{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,#12b76a,#0e9384);display:grid;place-items:center;font-weight:800;font-size:17px;flex:none}
h1{font-size:18px;margin:0;font-weight:700;letter-spacing:-.01em}
h1 span{font-weight:400;color:#c6ccd6}
.top-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.conn{font-size:12px;color:#c6ccd6;display:inline-flex;align-items:center;gap:6px;margin-right:4px}
.conn::before{content:"";width:8px;height:8px;border-radius:50%;background:#667085}
.conn.up::before{background:#12b76a}.conn.down::before{background:#f04438}
.btn{font:inherit;font-weight:600;font-size:13.5px;border-radius:8px;padding:8px 14px;border:1px solid var(--line);background:#fff;color:var(--ink);cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:6px;white-space:nowrap;transition:background .12s,border-color .12s}
.btn:hover{background:#f9fafb}
.btn:disabled{opacity:.45;cursor:not-allowed}
.btn.primary{background:#101828;color:#fff;border-color:#101828}.btn.primary:hover:not(:disabled){background:#283447}
.btn.go{background:var(--green);border-color:var(--green);color:#fff}.btn.go:hover:not(:disabled){background:#05603a}
.btn.danger{color:var(--red);border-color:var(--red-line)}.btn.danger:hover:not(:disabled){background:var(--red-bg)}
header .btn{background:transparent;color:#fff;border-color:#3b4658}
header .btn:hover{background:#1d2939}
main{max-width:1520px;margin:0 auto;padding:20px 28px 40px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.taskbar{padding:16px 18px;margin-bottom:18px}
.presets{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;align-items:center}
.presets .hint{font-size:12.5px;color:var(--soft);margin-right:4px}
.preset{font:inherit;font-size:13px;display:inline-flex;align-items:center;gap:8px;padding:5px 12px 5px 5px;border-radius:999px;border:1px solid var(--line);background:#f9fafb;color:var(--ink);cursor:pointer}
.preset b{display:grid;place-items:center;width:22px;height:22px;border-radius:50%;background:#101828;color:#fff;font-size:11.5px}
.preset:hover{border-color:#c4cad3;background:#fff}
.preset.sel{border-color:#101828;background:#fff;box-shadow:0 0 0 1px #101828 inset}
.task-row{display:flex;gap:12px;align-items:stretch}
textarea{flex:1;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:8px;resize:vertical;min-height:54px;color:var(--ink)}
textarea:focus{outline:none;border-color:#98a2b3;box-shadow:0 0 0 3px #eaecf0}
#run-btn{min-width:110px;font-size:15px}
.runline{display:flex;gap:10px;align-items:center;margin-top:12px;font-size:13px;color:var(--muted);flex-wrap:wrap;min-height:26px}
.runline:empty{display:none}
.mono{font-family:var(--mono);font-size:12.5px}
.badge{display:inline-flex;align-items:center;gap:6px;font-weight:600;font-size:11.5px;letter-spacing:.02em;padding:3px 10px;border-radius:999px;background:#f2f4f7;color:var(--muted);border:1px solid var(--line);white-space:nowrap}
.b-ok{background:var(--green-bg);color:var(--green);border-color:var(--green-line)}
.b-warn{background:var(--amber-bg);color:var(--amber);border-color:var(--amber-line)}
.b-bad{background:var(--red-bg);color:var(--red);border-color:var(--red-line)}
.b-park{background:var(--orange-bg);color:var(--orange);border-color:var(--orange-line)}
.b-info{background:var(--blue-bg);color:var(--blue);border-color:var(--blue-line)}
.b-live::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor;animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{50%{opacity:.25}}
.grid{display:grid;grid-template-columns:minmax(0,1.12fr) minmax(0,1fr);gap:18px;align-items:start}
@media (max-width:1080px){.grid{grid-template-columns:1fr}}
.side{display:flex;flex-direction:column;gap:18px;min-width:0}
#parks:empty{display:none}
.card-head{display:flex;align-items:center;gap:10px;padding:13px 18px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.card-head h2{font-size:15px;margin:0;font-weight:700}
.card-head .grow{flex:1}
.card-note{padding:10px 18px 0;margin:0;color:var(--muted);font-size:13px}
.moments{display:flex;align-items:center;gap:4px;font-size:11.5px;color:var(--soft)}
.moments .m{font:600 11px var(--mono);padding:2px 7px;border-radius:5px;background:#f2f4f7;color:var(--soft)}
.moments .m.on.hz{background:var(--purple);color:#fff}.moments .m.on.dt{background:var(--amber);color:#fff}
.moments .m.on.rc{background:var(--blue);color:#fff}.moments .m.on.vf{background:var(--green);color:#fff}
.toggle{font-size:12px;color:var(--muted);display:inline-flex;gap:5px;align-items:center;cursor:pointer}
.log{height:calc(100vh - 290px);min-height:500px;overflow:auto;padding:8px 10px 14px}
.empty{color:var(--soft);text-align:center;padding:48px 24px;font-size:13.5px}
.empty strong{display:block;color:var(--muted);font-size:15px;margin-bottom:4px}
.ev{display:grid;grid-template-columns:46px 96px minmax(0,1fr);gap:10px;align-items:baseline;padding:5px 8px;border-radius:8px;border-left:3px solid transparent;animation:fade .25s ease-out}
@keyframes fade{from{opacity:0;transform:translateY(3px)}}
.ev .t{color:var(--soft);font:11.5px var(--mono)}
.ev .lbl{font:600 10.5px/1.75 var(--mono);letter-spacing:.04em;padding:0 7px;border-radius:5px;justify-self:start;background:#f2f4f7;color:var(--muted);white-space:nowrap}
.ev .txt{word-break:break-word}
.ev .sub{color:var(--muted);font-size:12.5px;word-break:break-word}
.ev .tier{font:600 10px var(--mono);color:var(--orange);margin-left:6px;text-transform:uppercase}
.ev.k-obs .txt{color:var(--muted);font-size:13px}
.ev.k-llm{display:none}.log.with-llm .ev.k-llm{display:grid}
.ev.k-llm .txt{color:var(--soft);font-size:12.5px}
.ev.k-step .lbl{background:#101828;color:#fff}
.ev.k-ok .lbl,.ev.k-contract-ok .lbl{background:var(--green-bg);color:var(--green)}
.ev.k-bad{background:var(--red-bg);border-left-color:var(--red)}.ev.k-bad .lbl{background:var(--red);color:#fff}
.ev.k-obs-bad .lbl{background:var(--red-bg);color:var(--red)}
.ev.k-parked{background:var(--orange-bg);border-left-color:var(--orange)}.ev.k-parked .lbl{background:var(--orange);color:#fff}
.ev.k-blocked .lbl{background:var(--red);color:#fff}
.ev.k-contract .lbl,.ev.k-referee .lbl{background:var(--blue-bg);color:var(--blue)}
.ev.moment{padding:9px 8px;margin:5px 0;font-weight:500}
.ev.k-hazard{background:var(--purple-bg);border-left-color:var(--purple)}.ev.k-hazard .lbl{background:var(--purple);color:#fff}
.ev.k-detected{background:var(--amber-bg);border-left-color:var(--amber)}.ev.k-detected .lbl{background:var(--amber);color:#fff}
.ev.k-recovery{background:var(--blue-bg);border-left-color:var(--blue)}.ev.k-recovery .lbl{background:var(--blue);color:#fff}
.ev.k-verified{background:var(--green-bg);border-left-color:var(--green)}.ev.k-verified .lbl{background:var(--green);color:#fff}
.ev.k-final{background:var(--green-bg);border-left-color:var(--green);font-weight:600}.ev.k-final .lbl{background:var(--green);color:#fff}
.clauses{list-style:none;margin:0;padding:4px 18px}
.clauses li{display:grid;grid-template-columns:26px minmax(0,1fr);gap:10px;padding:11px 0;border-bottom:1px dashed var(--line)}
.clauses li:last-child{border-bottom:0}
.tick{width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:13px;background:#f2f4f7;color:var(--soft);border:1px solid var(--line)}
.clauses li.pass .tick{background:var(--green);color:#fff;border-color:var(--green)}
.clauses li.fail .tick{background:var(--red-bg);color:var(--red);border-color:var(--red-line)}
.clause-en{font-weight:500}
.clause-detail{font:12px var(--mono);color:var(--soft);margin-top:2px;word-break:break-all}
.clause-obs{font-size:12.5px;color:var(--red);margin-top:2px}
.clauses li.pass .clause-obs{color:var(--green)}
.results-label{padding:0 18px 4px;font-size:12px;color:var(--soft)}
.actions{display:flex;gap:10px;padding:12px 18px 16px;flex-wrap:wrap;align-items:center}
.actions .note{font-size:12px;color:var(--soft)}
.rbody .placeholder{padding:10px 0 2px}
.placeholder{padding:16px 18px 18px;color:var(--soft);font-size:13.5px;margin:0}
.park-card{border-color:var(--orange-line);overflow:hidden}
.park-card .card-head{background:var(--orange-bg);border-bottom-color:var(--orange-line)}
.park-title{font-size:16px;font-weight:700;padding:14px 18px 2px}
.park-reason{padding:4px 18px 8px;color:var(--ink)}
.kv{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:4px 16px;margin:4px 18px 4px;padding:10px 12px;background:#f9fafb;border:1px solid var(--line);border-radius:8px;font-size:13px}
.kv dt{color:var(--muted)}.kv dd{margin:0;font-family:var(--mono);font-size:12.5px;word-break:break-word}
#receipt-status.big{font-size:12.5px;padding:4px 12px}
.rbody{padding:6px 18px 18px}
.rsummary{margin:10px 0 4px;font-size:14px}
.rstats{font-size:12px;color:var(--soft);margin-bottom:6px}
.rsec{margin-top:14px}
.rsec h3{font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;margin:0 0 6px;display:flex;align-items:center;gap:8px}
.rsec h3 .count{font:600 11px var(--mono);padding:0 6px;border-radius:5px;background:#f2f4f7;color:var(--muted)}
.rsec.done h3{color:var(--green)}.rsec.exceptions h3{color:var(--amber)}.rsec.exceptions .ritem{border-left:3px solid var(--amber)}.rsec.notdone h3{color:var(--red)}.rsec.waiting h3{color:var(--orange)}.rsec.evidence h3{color:var(--blue)}.rsec.unexpected h3{color:var(--red)}
.ritem{border:1px solid var(--line);border-radius:8px;padding:9px 12px;margin-bottom:6px;background:#fff}
.rsec.done .ritem{border-left:3px solid var(--green)}
.rsec.notdone .ritem,.rsec.unexpected .ritem{border-left:3px solid var(--red)}
.rsec.waiting .ritem{border-left:3px solid var(--orange)}
.rsec.evidence .ritem{border-left:3px solid var(--blue)}
.rhead{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.rref{font-weight:600}
.rsub{font-size:12.5px;color:var(--muted);margin-top:3px;word-break:break-word}
.rule{font:600 11px var(--mono);padding:1px 7px;border-radius:5px;background:var(--red-bg);color:var(--red)}
.rsec.waiting .rule{background:var(--orange-bg);color:var(--orange)}
.none{font-size:13px;color:var(--soft);margin:0}
pre.excerpt{margin:6px 0 0;padding:8px 10px;background:#101828;color:#d0d5dd;border-radius:6px;font:11.5px/1.5 var(--mono);white-space:pre-wrap;word-break:break-all;max-height:7.5em;overflow:auto}
.req{font:12px var(--mono);color:var(--muted);margin-top:4px;word-break:break-all}
.req b{color:var(--green)}.req b.bad{color:var(--red)}
#toast{position:fixed;right:22px;bottom:22px;display:flex;flex-direction:column;gap:8px;z-index:10}
.toast{background:#101828;color:#fff;padding:10px 14px;border-radius:8px;font-size:13px;box-shadow:0 8px 24px rgba(16,24,40,.18);max-width:420px;animation:fade .2s ease-out}
.toast.bad{background:var(--red)}
</style>
</head>
<body>
<header class="top">
  <div class="brand"><div class="logo">R</div><h1>Receipts <span>— every task comes with proof</span></h1></div>
  <div class="top-actions">
    <span id="app-status" class="conn">Company app</span>
    <a id="chaos-link" class="btn" target="_blank" rel="noopener">Open chaos panel &#8599;</a>
    <button id="reset-btn" class="btn" type="button" title="Restore the company app's seed data and clear the gateway database">Reset</button>
  </div>
</header>
<main>
  <section class="card taskbar">
    <div class="presets" id="presets"><span class="hint">Try a preset</span></div>
    <div class="task-row">
      <textarea id="task" rows="2" placeholder="Describe a back-office task in plain English. The agent drafts a contract first; nothing counts as done until the referee proves it."></textarea>
      <button id="run-btn" class="btn primary" type="button">Run</button>
    </div>
    <div class="runline" id="runline"></div>
  </section>
  <div class="grid">
    <section class="card">
      <div class="card-head">
        <h2>Live event log</h2>
        <div class="moments" id="moments" title="Self-healing moments in this run"></div>
        <span class="grow"></span>
        <label class="toggle"><input type="checkbox" id="show-llm"> LLM calls</label>
      </div>
      <div id="log" class="log"></div>
    </section>
    <div class="side">
      <section class="card" id="contract-card">
        <div class="card-head"><h2>Contract</h2><span class="grow"></span><span id="contract-badge"></span></div>
        <div id="contract-body"></div>
      </section>
      <div id="parks" class="side"></div>
      <section class="card" id="receipt-card">
        <div class="card-head"><h2>Receipt</h2><span class="grow"></span><span id="receipt-status" class="badge big">PENDING</span></div>
        <div id="receipt-body" class="rbody"></div>
      </section>
    </div>
  </div>
</main>
<div id="toast"></div>
<script>
const APP_URL = __APP_URL__;
const $ = (s) => document.querySelector(s);
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat(Infinity)) if (k !== null && k !== undefined && k !== false && k !== '') e.append(k instanceof Node ? k : String(k));
  return e;
}
function toast(msg, bad) {
  const t = h('div', {class: 'toast' + (bad ? ' bad' : '')}, msg);
  $('#toast').append(t); setTimeout(() => t.remove(), bad ? 7000 : 3500);
}
async function api(method, url, body) {
  const r = await fetch(url, {method, headers: body ? {'content-type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
  let data = null; try { data = await r.json(); } catch (e) {}
  return {ok: r.ok, status: r.status, data};
}
function detail(res) { return (res.data && (typeof res.data.detail === 'string' ? res.data.detail : JSON.stringify(res.data.detail))) || ('HTTP ' + res.status); }

const OK_STATUSES = ['VERIFIED', 'ALREADY_DONE'];
function statusBadgeClass(s) {
  if (OK_STATUSES.includes(s)) return 'b-ok';
  if (s === 'VERIFIED_WITH_EXCEPTIONS') return 'b-warn';
  if (s === 'FAILED' || s === 'ERROR' || s === 'REJECTED') return 'b-bad';
  return '';
}
function statusRowKind(s) {
  if (OK_STATUSES.includes(s) || s === 'VERIFIED_WITH_EXCEPTIONS') return 'final';
  if (s === 'FAILED' || s === 'ERROR') return 'bad';
  return 'referee';
}
function propStatusKind(s) {
  return {ALLOWED: 'ok', APPROVED: 'ok', DONE: 'ok', SUBMITTED: 'ok', BLOCKED: 'blocked', REJECTED: 'blocked', PARKED: 'parked'}[s] || 'obs';
}
function fmtVal(v) { return (v !== null && typeof v === 'object') ? JSON.stringify(v) : String(v); }
function fmtFields(f) { return Object.values(f || {}).map(fmtVal).join(' · '); }
function argSummary(args) {
  if (!args || typeof args !== 'object') return '';
  const s = Object.entries(args).map(([k, v]) => (v !== null && typeof v === 'object') ? `${k}: ${Object.entries(v).map(([a, b]) => `${a}=${fmtVal(b)}`).join(', ')}` : `${k}: ${fmtVal(v)}`).join('  ·  ');
  return s.length > 160 ? s.slice(0, 157) + '…' : s;
}
function clauseDetail(c) {
  switch (c.kind) {
    case 'row_count': return `GET ${c.path} → ${c.equals} row(s)`;
    case 'http_field_equals': return `GET ${c.path} → ${c.field} = ${fmtVal(c.equals)}`;
    case 'http_field_contains': return `GET ${c.path} → ${c.field} contains ${fmtVal(c.contains)}`;
    case 'outbox_contains': return `outbox to ${c.to} mentions "${c.text}"`;
    case 'file_contains': return `file ${c.path} contains "${c.text}"`;
    default: return JSON.stringify(c);
  }
}
function clauseEnglish(c, i, english) { return (english && english[i]) || c.description || clauseDetail(c); }
function tsOf(v) {
  if (typeof v === 'number') return v > 1e12 ? v / 1000 : v;
  const p = Date.parse(v); return isNaN(p) ? null : p / 1000;
}
function fmtSecs(s) { s = Math.max(0, Math.round(+s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }

let S = fresh(null), gen = 0, presets = [], hazardsInFlight = false;
function fresh(run) {
  return {run, next: 0, finished: false, missing: false, startTs: null, lastTs: null, task: '',
          contract: null, contractState: 'none', results: null, resultsLabel: '', receipt: null, receiptMiss: 0,
          wantReceipt: false, finishedStatus: null, error: null, hazardKeys: new Set(),
          counts: {hazard: 0, detected: 0, recovery: 0, verified: 0}, steps: 0, llm: 0, tokens: 0, parked: [], parkedSig: null,
          contractSig: null, receiptSig: null};
}

/* ---------- event log ---------- */
function addRow(type, kind, label, text, sub, ts, extra) {
  const log = $('#log');
  const empty = log.querySelector('.empty'); if (empty) empty.remove();
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  const moment = ['hazard', 'detected', 'recovery', 'verified'].includes(kind);
  const row = h('div', {class: `ev ev-${type} k-${kind}${moment ? ' moment' : ''}`, 'data-ts': ts},
    h('span', {class: 't'}, S.startTs !== null && ts !== null ? '+' + fmtSecs(ts - S.startTs) : ''),
    h('span', {class: 'lbl'}, label),
    h('div', {class: 'msg'}, h('div', {class: 'txt'}, text, extra), sub ? h('div', {class: 'sub'}, sub) : null));
  let before = null;  // keep rows in time order (hazards arrive from a separate feed)
  if (ts !== null) for (let n = log.lastElementChild; n; n = n.previousElementSibling) {
    if (+n.dataset.ts > ts) before = n; else break;
  }
  log.insertBefore(row, before);
  if (atBottom) log.scrollTop = log.scrollHeight;
}
function onEvent(ev) {
  const ts = typeof ev.ts === 'number' ? ev.ts : Date.now() / 1000;
  if (S.startTs === null) S.startTs = ts;
  S.lastTs = ts;
  const n = (k) => ev[k] === undefined || ev[k] === null ? '' : ev[k];
  switch (ev.type) {
    case 'run_started':
      S.task = ev.task || '';
      if (!$('#task').value) $('#task').value = S.task;
      addRow('run_started', 'contract', 'START', 'Run started', S.task, ts); break;
    case 'contract_proposed': {
      S.contract = {clauses: ev.clauses || [], english: ev.english || []};
      S.contractState = 'proposed'; S.results = null;
      const c = S.contract.clauses.length;
      addRow('contract_proposed', 'contract', 'CONTRACT', `Drafted a contract with ${c} check${c === 1 ? '' : 's'} — waiting for your approval`, null, ts); break;
    }
    case 'contract_approved':
      S.contractState = 'approved';
      addRow('contract_approved', 'ok', 'CONTRACT', `Contract approved by ${ev.by || 'operator'}`, null, ts); break;
    case 'contract_rejected':
      S.contractState = 'rejected';
      addRow('contract_rejected', 'bad', 'CONTRACT', 'Contract rejected — the agent will not act', null, ts); break;
    case 'referee_pre': {
      S.results = ev.results || [];
      S.resultsLabel = ev.all_passed ? 'Checked before acting: already true' : 'Checked before acting: not true yet (red before green)';
      const k = S.results.filter(r => r.passed).length;
      addRow('referee_pre', 'referee', 'REFEREE', ev.all_passed ? 'Every check already passes — nothing to do' : `Before acting: ${k}/${S.results.length} checks pass (red before green)`, null, ts); break;
    }
    case 'step':
      S.steps++;
      addRow('step', 'step', `STEP ${n('n')}`, ev.tool, argSummary(ev.args), ts,
             ['write', 'irreversible'].includes(ev.tier) ? h('span', {class: 'tier'}, ev.tier) : null); break;
    case 'observation': {
      const st = ev.status || '';
      const kind = ['ERROR', 'BLOCKED'].includes(st) ? 'obs-bad' : st === 'PARKED' ? 'parked' : 'obs';
      addRow('observation', kind === 'parked' ? 'obs' : kind, st || 'RESULT', ev.summary || '', null, ts); break;
    }
    case 'detected':
      S.counts.detected++; addRow('detected', 'detected', 'DETECTED', ev.what || '', null, ts); break;
    case 'recovery':
      S.counts.recovery++; addRow('recovery', 'recovery', 'RECOVERY', `Recovering with ${ev.tool || 'a retry'}`, null, ts); break;
    case 'verified':
      S.counts.verified++; addRow('verified', 'verified', 'VERIFIED', ev.what || '', null, ts); break;
    case 'proposal':
      addRow('proposal', propStatusKind(ev.status), 'GATEWAY', `${ev.kind} ${ev.ref || ''} → ${ev.status}`,
             [ev.rule_id ? `rule ${ev.rule_id}` : '', ev.reason || ''].filter(Boolean).join(' — '), ts); break;
    case 'parked':
      addRow('parked', 'parked', 'PARKED', `Waiting on you: ${ev.kind} ${ev.ref || ''}`, ev.rule_id ? `rule ${ev.rule_id} — decide on the card at the right` : null, ts); break;
    case 'decision':
      addRow('decision', ev.decision === 'approve' ? 'ok' : 'blocked', 'DECISION',
             `${ev.proposal_id} ${ev.decision === 'approve' ? 'approved' : 'rejected'} by ${ev.by || 'operator'}`, null, ts); break;
    case 'llm':
      S.llm++; S.tokens += (+ev.prompt_tokens || 0) + (+ev.completion_tokens || 0);
      addRow('llm', 'llm', 'LLM', `${ev.model} · ${n('prompt_tokens')} → ${n('completion_tokens')} tokens${ev.wait_s ? ` · waited ${ev.wait_s}s for rate limit` : ''}`, null, ts); break;
    case 'referee': {
      S.results = ev.results || []; S.resultsLabel = `Referee round ${n('round')}: ${ev.status}`;
      const k = S.results.filter(r => r.passed).length;
      addRow('referee', statusRowKind(ev.status), 'REFEREE', `Round ${n('round')}: ${ev.status}`, `${k}/${S.results.length} contract checks pass`, ts); break;
    }
    case 'receipt':
      S.wantReceipt = true;
      addRow('receipt', statusRowKind(ev.status), 'RECEIPT', `Receipt written: ${ev.status}`, ev.path || null, ts); break;
    case 'run_finished':
      S.finishedStatus = ev.status;
      addRow('run_finished', statusRowKind(ev.status), 'FINISHED', `Run finished: ${ev.status}`,
             `${n('steps')} steps · ${n('llm_calls')} LLM calls · ${n('tokens')} tokens · ${fmtSecs(ev.seconds)}`, ts); break;
    case 'error':
      S.error = ev.message || 'error';
      addRow('error', 'bad', 'ERROR', S.error, null, ts); break;
    default: {
      const {ts: _t, type: _y, ...rest} = ev;
      addRow(String(ev.type), 'obs', String(ev.type || 'EVENT').toUpperCase(), argSummary(rest), null, ts);
    }
  }
}
function onHazard(hz) {
  const key = JSON.stringify(hz);
  if (S.hazardKeys.has(key)) return;
  const ts = tsOf(hz.ts);
  if (ts !== null && ts < S.startTs - 2) return;  // chaos from before this run
  S.hazardKeys.add(key); S.counts.hazard++;
  addRow('hazard', 'hazard', 'HAZARD', `Chaos injected: ${hz.hazard || 'hazard'}`, hz.detail || null, ts === null ? Date.now() / 1000 : ts);
}
function renderMoments() {
  const c = S.counts, el = $('#moments');
  if (!S.run) { el.replaceChildren(); return; }
  const chip = (cls, label, v) => h('span', {class: `m ${cls}${v ? ' on' : ''}`}, `${label} ${v}`);
  el.replaceChildren(chip('hz', 'HAZARD', c.hazard), '→', chip('dt', 'DETECTED', c.detected), '→', chip('rc', 'RECOVERY', c.recovery), '→', chip('vf', 'VERIFIED', c.verified));
}
function logPlaceholder() {
  $('#log').replaceChildren(h('div', {class: 'empty'}, h('strong', {}, 'No run yet'),
    'Pick a preset or describe a task, then press Run. Every step, hazard and recovery shows up here as it happens.'));
}

/* ---------- run line ---------- */
function runStatus() {
  if (S.missing) return ['Run not found', 'b-bad'];
  if (S.finished) {
    const st = (S.receipt && S.receipt.status) || S.finishedStatus || (S.error ? 'ERROR' : 'FINISHED');
    return [st, statusBadgeClass(st)];
  }
  if (S.contractState === 'proposed') return ['Needs your approval', 'b-warn b-live'];
  if (S.parked.length) return ['Waiting on you', 'b-park b-live'];
  return ['Working', 'b-info b-live'];
}
function renderRunline() {
  const el = $('#runline');
  if (!S.run) { el.replaceChildren(); return; }
  const [label, cls] = runStatus();
  const end = S.finished ? S.lastTs : Date.now() / 1000;
  const bits = [`${S.steps} steps`];
  if (S.llm) bits.push(`${S.llm} LLM calls · ${S.tokens.toLocaleString()} tokens`);
  if (S.startTs !== null) bits.push(`${fmtSecs(end - S.startTs)} elapsed`);
  el.replaceChildren(h('span', {class: 'badge ' + cls, id: 'run-status'}, label),
    h('span', {class: 'mono'}, `run ${S.run}`), h('span', {}, bits.join(' · ')),
    S.task ? h('span', {style: 'color:var(--soft);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:520px'}, `“${S.task}”`) : null);
  $('#run-btn').disabled = !!S.run && !S.finished;
}

/* ---------- contract ---------- */
function renderContract() {
  const body = $('#contract-body'), badge = $('#contract-badge');
  const sig = JSON.stringify([S.run, !!S.contract, S.contractState, S.finished, S.results, S.receipt && S.receipt.status]);
  if (sig === S.contractSig) return;
  S.contractSig = sig;
  if (!S.contract) {
    badge.replaceChildren();
    body.replaceChildren(h('p', {class: 'placeholder'}, S.run && !S.finished
      ? 'The agent is drafting a contract: the checklist the independent referee will verify.'
      : 'Before touching anything, the agent writes a contract: a plain-English checklist that an independent referee checks against the real systems.'));
    return;
  }
  const badges = {proposed: ['Awaiting your approval', 'badge b-warn b-live'], sending: ['Sending…', 'badge'],
                  approved: ['Approved', 'badge b-ok'], rejected: ['Rejected', 'badge b-bad']};
  const [bl, bc] = badges[S.contractState] || ['', ''];
  badge.replaceChildren(bl ? h('span', {class: bc}, bl) : '');
  let results = S.results, label = S.resultsLabel;
  if (S.receipt && Array.isArray(S.receipt.contract) && S.receipt.contract.length) { results = S.receipt.contract; label = `Final referee verdict: ${S.receipt.status}`; }
  const items = S.contract.clauses.map((c, i) => {
    const r = results && results[i];
    const cls = r ? (r.passed ? 'pass' : 'fail') : '';
    return h('li', {class: cls},
      h('span', {class: 'tick'}, r ? (r.passed ? '✓' : '✗') : '○'),
      h('div', {}, h('div', {class: 'clause-en'}, clauseEnglish(c, i, S.contract.english)),
        h('div', {class: 'clause-detail'}, clauseDetail(c)),
        r && r.observed !== undefined ? h('div', {class: 'clause-obs'}, `observed: ${fmtVal(r.observed)}`) : null));
  });
  const parts = [h('ul', {class: 'clauses', id: 'clauses'}, items)];
  if (label) parts.push(h('div', {class: 'results-label'}, label));
  if (S.contractState === 'proposed' && !S.finished) {
    parts.push(h('div', {class: 'actions'},
      h('button', {class: 'btn go', id: 'contract-approve', type: 'button', onclick: () => contractAction('approve')}, 'Approve contract'),
      h('button', {class: 'btn danger', id: 'contract-reject', type: 'button', onclick: () => contractAction('reject')}, 'Reject'),
      h('span', {class: 'note'}, 'The run is not done until every check passes.')));
  }
  body.replaceChildren(...parts);
}
async function contractAction(action) {
  const run = S.run;
  S.contractState = 'sending'; renderContract();
  const res = await api('POST', `/api/runs/${encodeURIComponent(run)}/contract`, {action});
  if (run !== S.run) return;
  if (!res.ok) { S.contractState = 'proposed'; renderContract(); toast(`Could not send contract decision: ${detail(res)}`, true); return; }
  toast(action === 'approve' ? 'Contract approved — the agent can start work.' : 'Contract rejected.');
}

/* ---------- park cards ---------- */
function renderParks() {
  const box = $('#parks');
  box.replaceChildren(...S.parked.map((p) => {
    const card = h('section', {class: 'card park-card', 'data-proposal': p.proposal_id});
    const btns = [];
    const act = (decision, always) => () => decide(p.proposal_id, decision, always, btns);
    btns.push(h('button', {class: 'btn go', type: 'button', onclick: act('approve', false)}, 'Approve once'),
              h('button', {class: 'btn', type: 'button', title: 'Approve, and remember this exception for the same rule next time', onclick: act('approve', true)}, 'Always allow'),
              h('button', {class: 'btn danger', type: 'button', onclick: act('reject', false)}, 'Reject'));
    card.append(
      h('div', {class: 'card-head'}, h('span', {class: 'badge b-park b-live'}, 'Waiting on you'), h('span', {class: 'badge'}, p.kind), h('span', {class: 'grow'}), h('span', {class: 'rule mono'}, p.rule_id || '')),
      h('div', {class: 'park-title'}, p.ref || p.proposal_id),
      h('div', {class: 'park-reason'}, p.reason || ''),
      h('dl', {class: 'kv'}, Object.entries(p.fields || {}).map(([k, v]) => [h('dt', {}, k), h('dd', {}, fmtVal(v))]),
        p.source ? [h('dt', {}, 'source'), h('dd', {}, p.source)] : null,
        h('dt', {}, 'proposal'), h('dd', {}, p.proposal_id)),
      h('div', {class: 'actions'}, btns));
    return card;
  }));
}
async function decide(pid, decision, always, btns) {
  btns.forEach(b => b.disabled = true);
  const res = await api('POST', `/api/parked/${encodeURIComponent(pid)}`, {decision, always});
  if (!res.ok) { btns.forEach(b => b.disabled = false); toast(`Decision failed: ${detail(res)}`, true); return; }
  toast(decision === 'reject' ? `Rejected ${pid}.` : always ? `Approved ${pid} and remembered the exception.` : `Approved ${pid} once.`);
  S.parkedSig = null; await pollParked(gen);
}

/* ---------- receipt ---------- */
function proposalItem(p, ruleFirst) {
  return h('div', {class: 'ritem'},
    h('div', {class: 'rhead'}, h('span', {class: 'rref'}, `${p.kind} ${p.ref || ''}`), h('span', {class: 'badge ' + ({DONE: 'b-ok', PARKED: 'b-park'}[p.status] || 'b-bad')}, p.status),
      ruleFirst && p.rule_id ? h('span', {class: 'rule'}, p.rule_id) : null),
    h('div', {class: 'rsub'}, fmtFields(p.fields)),
    p.reason && ruleFirst ? h('div', {class: 'rsub'}, p.reason) : null);
}
function section(cls, title, list, render, emptyText) {
  return h('div', {class: 'rsec ' + cls}, h('h3', {}, title, h('span', {class: 'count'}, String(list.length))),
    list.length ? list.map(render) : h('p', {class: 'none'}, emptyText));
}
function renderReceipt() {
  const badge = $('#receipt-status'), body = $('#receipt-body');
  const rc = S.receipt;
  const sig = JSON.stringify([S.run, !!rc, S.finished, S.error]);
  if (sig === S.receiptSig) return;
  S.receiptSig = sig;
  if (!rc) {
    badge.textContent = S.finished ? 'NO RECEIPT' : 'PENDING';
    badge.className = 'badge big' + (S.finished ? ' b-bad' : '');
    body.replaceChildren(h('p', {class: 'placeholder'}, S.finished
      ? `The run ended without a receipt${S.error ? `: ${S.error}` : '.'}`
      : 'Nothing is done until the referee says so. The receipt lists what was done, what was deliberately not done, what waits on you, and the evidence.'));
    return;
  }
  badge.textContent = rc.status; badge.className = 'badge big ' + statusBadgeClass(rc.status);
  const st = rc.stats || {};
  const contract = rc.contract || [];
  const parts = [
    rc.summary ? h('p', {class: 'rsummary'}, rc.summary) : null,
    h('div', {class: 'rstats'}, [`run ${rc.run_id}`, st.steps !== undefined ? `${st.steps} steps` : '', st.llm_calls !== undefined ? `${st.llm_calls} LLM calls` : '',
      st.seconds !== undefined ? `${fmtSecs(st.seconds)}` : ''].filter(Boolean).join(' · ')),
    section('done', 'Done', rc.done || [], p => proposalItem(p, false), 'Nothing was changed.'),
    section('notdone', 'Deliberately NOT done', rc.not_done || [], p => proposalItem(p, true), 'Nothing was refused or skipped.'),
    section('waiting', 'Waiting on you', rc.waiting || [], p => proposalItem(p, true), 'Nothing is waiting on you.'),
  ];
  if ((rc.exceptions || []).length) parts.push(section('exceptions', 'Exceptions', rc.exceptions, x => h('div', {class: 'ritem'},
    h('div', {class: 'rhead'}, h('span', {class: 'rref'}, x.proposal_id || ''), x.status ? h('span', {class: 'badge b-warn'}, x.status) : null, x.rule_id ? h('span', {class: 'rule'}, x.rule_id) : null),
    h('div', {class: 'rsub'}, x.reason || ''), x.clause ? h('div', {class: 'req'}, clauseDetail(x.clause)) : null), ''));
  if ((rc.unexpected_changes || []).length) parts.push(section('unexpected', 'Unexpected changes', rc.unexpected_changes, u => h('div', {class: 'ritem'},
    h('div', {class: 'rhead'}, h('span', {class: 'rref'}, `${u.table} ${u.ref}`), h('span', {class: 'badge b-bad'}, u.change)),
    h('div', {class: 'rsub'}, fmtFields(u.row))), ''));
  parts.push(section('evidence', 'Evidence', contract, (c) => {
    const ev = c.evidence || {};
    return h('div', {class: 'ritem'},
      h('div', {class: 'rhead'}, h('span', {class: 'badge ' + (c.passed ? 'b-ok' : 'b-bad')}, c.passed ? 'PASS' : 'FAIL'), h('span', {class: 'rref'}, c.english || (c.clause ? clauseDetail(c.clause) : ''))),
      h('div', {class: 'req'}, ev.request || '', ev.status !== undefined ? [' → ', h('b', {class: String(ev.status).startsWith('2') ? '' : 'bad'}, String(ev.status))] : null,
        c.observed !== undefined ? `   observed: ${fmtVal(c.observed)}` : ''),
      ev.body_excerpt ? h('pre', {class: 'excerpt'}, ev.body_excerpt) : null);
  }, 'No contract checks.'));
  body.replaceChildren(...parts);
}

/* ---------- polling ---------- */
async function pollEvents(g) {
  const r = await fetch(`/api/runs/${encodeURIComponent(S.run)}/events?after=${S.next}`);
  if (g !== gen) return;
  if (r.status === 404) { S.missing = true; S.finished = true; return; }
  if (!r.ok) throw new Error('events HTTP ' + r.status);
  const d = await r.json();
  if (g !== gen) return;
  for (const ev of d.events) onEvent(ev);
  S.next = d.next; S.finished = d.finished;
}
async function pollParked(g) {
  if (!S.run) return;
  const r = await fetch(`/api/parked?run_id=${encodeURIComponent(S.run)}`);
  if (g !== gen || !r.ok) return;
  const list = await r.json();
  if (g !== gen) return;
  const sig = JSON.stringify(list.map(p => [p.proposal_id, p.status]));
  if (sig === S.parkedSig) return;
  S.parkedSig = sig; S.parked = list; renderParks();
}
async function pollHazards(g) {
  const r = await fetch('/api/hazards');
  $('#app-status').className = 'conn ' + (r.status === 502 ? 'down' : 'up');
  $('#app-status').title = r.status === 502 ? 'Company app unreachable at ' + APP_URL : 'Company app at ' + APP_URL;
  if (g !== gen || !r.ok || !S.run || S.startTs === null) return;
  for (const hz of await r.json()) if (g === gen) onHazard(hz);
}
async function loadReceipt(g) {
  const r = await fetch(`/api/runs/${encodeURIComponent(S.run)}/receipt`);
  if (g !== gen) return;
  if (r.ok) S.receipt = await r.json(); else S.receiptMiss++;
}
function renderAll() { renderRunline(); renderMoments(); renderContract(); renderReceipt(); }
async function tick(g) {
  if (g !== gen) return;
  try {
    await pollEvents(g);
    if (!S.missing && !hazardsInFlight) {  // not awaited: a slow or down company app must not stall the event stream
      hazardsInFlight = true;
      pollHazards(g).catch(() => {}).finally(() => { hazardsInFlight = false; });
    }
    if (!S.missing) await pollParked(g);
    if (!S.missing && !S.receipt && (S.wantReceipt || S.finished)) await loadReceipt(g);
  } catch (e) { if (g === gen) $('#app-status').title = 'Console poll failed: ' + e.message; }
  if (g !== gen) return;
  renderAll();
  const done = S.finished && (S.missing || S.receipt || S.receiptMiss >= 3);
  if (!done) setTimeout(() => tick(g), 1000);
}
function startRun(id) {
  gen++; S = fresh(id);
  $('#log').replaceChildren(h('div', {class: 'empty'}, h('strong', {}, 'Starting…'), 'Waiting for the agent’s first event.'));
  $('#parks').replaceChildren();
  renderAll(); tick(gen);
}
function runFromHash() {
  const m = location.hash.match(/^#run=([0-9A-Za-z_-]+)$/);
  if (m) startRun(m[1]);
}

/* ---------- controls ---------- */
async function run() {
  const task = $('#task').value.trim();
  if (!task) { $('#task').focus(); toast('Describe a task first, or pick a preset.', true); return; }
  $('#run-btn').disabled = true;
  const res = await api('POST', '/api/runs', {task});
  if (!res.ok) { $('#run-btn').disabled = false; toast(`Could not start the agent: ${detail(res)}`, true); return; }
  history.replaceState(null, '', '#run=' + res.data.run_id);
  startRun(res.data.run_id);
}
async function reset() {
  const btn = $('#reset-btn'); btn.disabled = true;
  const res = await api('POST', '/api/reset');
  btn.disabled = false;
  if (!res.ok) { toast(`Reset failed: ${detail(res)}`, true); return; }
  gen++; S = fresh(null);
  history.replaceState(null, '', location.pathname);
  $('#parks').replaceChildren(); logPlaceholder(); renderAll();
  $('#run-btn').disabled = false;
  const d = res.data;
  if (d.app !== 'ok') toast(`Gateway cleared, but the company app did not reset (${d.app}).`, true);
  else toast(`Reset: company app restored to seed data, gateway cleared${d.stopped_runs.length ? `, ${d.stopped_runs.length} run stopped` : ''}.`);
}
async function init() {
  $('#chaos-link').href = APP_URL + '/chaos';
  $('#run-btn').addEventListener('click', run);
  $('#reset-btn').addEventListener('click', reset);
  $('#task').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) run(); });
  $('#task').addEventListener('input', () => document.querySelectorAll('.preset').forEach(b => b.classList.toggle('sel', b.dataset.task === $('#task').value)));
  $('#show-llm').addEventListener('change', (e) => $('#log').classList.toggle('with-llm', e.target.checked));
  window.addEventListener('hashchange', runFromHash);
  logPlaceholder(); renderAll();
  try {
    presets = (await api('GET', '/api/presets')).data || [];
    $('#presets').append(...presets.map(p => h('button', {class: 'preset', type: 'button', 'data-id': p.id, 'data-task': p.task, title: p.task,
      onclick: (e) => { $('#task').value = p.task; $('#task').dispatchEvent(new Event('input')); $('#task').focus(); }}, h('b', {}, p.id), p.title)));
  } catch (e) { toast('Could not load presets.', true); }
  if (location.hash) runFromHash();
  else pollHazards(gen).catch(() => {});
}
init();
</script>
</body>
</html>
""".replace("__APP_URL__", json.dumps(PUBLIC_APP_URL).replace("</", "<\\/"))
