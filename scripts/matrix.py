"""Reliability matrix: run the agent on the Acme invoice task under each technical hazard, keep every receipt.

Run (from the repo root):
    .venv\\Scripts\\python scripts\\matrix.py [--seeds N] [--hazards h1,h2,...] [--out docs/evidence/matrix]
                                           [--table docs/reliability.md] [--model-note TEXT] [--resume]
    .venv\\Scripts\\python scripts\\matrix.py --selftest

For each hazard the company app is started once; before every run it is reset with that hazard's chaos
flags and the agent gets fresh isolated state (gateway DB, runs dir, site memory, overrides). Each run's
receipt.json, receipt.md and events.jsonl are copied to <out>/<run_id>/, and <out>/matrix.json is rewritten
after every run so a crash keeps partial results. The pass-rate table is built from the stored receipts:
a run passes only when its receipt status equals the expected status. The table is data, not a gate, so
the script exits 0 once the matrix is complete, whatever the pass rate.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from checks._servers import PY, ROOT, company_app, isolated_env, reset  # noqa: E402

TASK = ("Find the latest invoice from Acme Corp, extract the amount and due date, enter it into our AP system, "
        "and tell me once it is done.")
CONTRACT = ROOT / "contracts" / "acme_invoice.json"
EXPECTED = "VERIFIED"
TIMEOUT_S = 1800
EVIDENCE = ("receipt.json", "receipt.md", "events.jsonl")
TECHNICAL = {"save_500": 2, "session_expiry": True, "renamed_button": True, "date_rule": True}
HAZARDS: dict[str, dict] = {
    "baseline": {},
    "save_500": {"save_500": 2},
    "session_expiry": {"session_expiry": True},
    "renamed_button": {"renamed_button": True},
    "date_rule": {"date_rule": True},
    "all_technical": TECHNICAL,
}
DESCRIPTIONS = {
    "baseline": "no chaos; the plain task.",
    "save_500": "the first two record saves return HTTP 500 and write nothing.",
    "session_expiry": "the first record save drops the session and redirects to the login page.",
    "renamed_button": "Save/Send buttons are replaced by \"Submit for posting\" hidden under a \"More actions\" disclosure.",
    "date_rule": "the AP system suddenly demands due dates as DD/MM/YYYY and rejects ISO dates.",
    "all_technical": "all four technical hazards at once (save_500=2, session_expiry, renamed_button, date_rule).",
}
HEADER = "| Hazard | Runs | Passed | Pass rate | Median LLM calls | Median seconds |"


def run_id(hazard: str, seed: int) -> str:
    return f"matrix-{hazard}-{seed}"


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the agent and its browser children (a plain kill on Windows leaves Chromium running)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    proc.kill()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass


def run_agent(env: dict[str, str], base: str, rid: str) -> tuple[int | None, float, Path]:
    """Run agent.py once; return (exit code or None on timeout, wall seconds, log path).

    Output goes to a log file, not a pipe: a pipe kept open by a grandchild (the browser) would hang a
    timed-out run forever.
    """
    run_env = {**os.environ, **env, "RECEIPTS_APP_URL": base, "PYTHONIOENCODING": "utf-8"}
    run_env.pop("RECEIPTS_CRASH_AFTER_SUBMIT", None)
    cmd = [PY, "agent.py", TASK, "--contract", str(CONTRACT), "--yes", "--decide", "reject", "--run-id", rid]
    log = Path(env["RECEIPTS_RUNS"]).parent / "agent.log"
    started = time.time()
    with log.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=run_env, stdin=subprocess.DEVNULL, stdout=fh,
                                stderr=subprocess.STDOUT)
        try:
            code: int | None = proc.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            code = None
    return code, time.time() - started, log


def hazards_fired(base: str) -> int | None:
    try:
        r = httpx.get(f"{base}/api/chaos/log", timeout=10)
        r.raise_for_status()
        return len(r.json())
    except (httpx.HTTPError, ValueError):
        return None


def read_receipt(path: Path) -> tuple[str, int | None]:
    """(status, llm_calls) from a stored receipt; status MISSING / BAD_RECEIPT when it can't be used."""
    try:
        receipt = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return "MISSING", None
    except (OSError, ValueError):
        return "BAD_RECEIPT", None
    if not isinstance(receipt, dict) or not receipt.get("status"):
        return "BAD_RECEIPT", None
    stats = receipt.get("stats") if isinstance(receipt.get("stats"), dict) else {}
    calls = stats.get("llm_calls")
    return str(receipt["status"]), calls if isinstance(calls, int) and not isinstance(calls, bool) else None


def harvest(runs_dir: Path, out: Path, hazard: str, seed: int, rid: str, exit_code: int | None,
            seconds: float, fired: int | None) -> dict:
    """Copy the run's evidence into <out>/<rid>/ and build its matrix record from the stored receipt."""
    src, dst = runs_dir / rid, out / rid
    dst.mkdir(parents=True, exist_ok=True)
    for name in EVIDENCE:
        (dst / name).unlink(missing_ok=True)  # never let a previous matrix's receipt stand in for this run
        if (src / name).is_file():
            shutil.copy2(src / name, dst / name)
    status, llm_calls = read_receipt(dst / "receipt.json")
    if exit_code is None:
        status = "TIMEOUT"
    return {"hazard": hazard, "seed": seed, "run_id": rid, "expected": EXPECTED, "status": status,
            "exit_code": exit_code, "llm_calls": llm_calls, "seconds": round(seconds, 1), "hazards_fired": fired}


def sort_key(rec: dict) -> tuple:
    order = list(HAZARDS)
    h = rec["hazard"]
    return (order.index(h) if h in order else len(order), h, rec["seed"])


def save_matrix(out: Path, meta: dict, runs: list[dict]) -> None:
    """Atomically rewrite <out>/matrix.json."""
    out.mkdir(parents=True, exist_ok=True)
    path = out / "matrix.json"
    tmp = path.with_name("matrix.json.tmp")
    data = {**meta, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "runs": sorted(runs, key=sort_key)}
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def median(values: list, digits: int) -> str:
    nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not nums:
        return "-"
    return f"{round(statistics.median(nums), digits):g}" if digits else f"{statistics.median(nums):.0f}"


def row(label: str, recs: list[dict]) -> str:
    passed = sum(r["status"] == r["expected"] for r in recs)
    rate = f"{int(100 * passed / len(recs) + 0.5)}%" if recs else "-"
    return (f"| {label} | {len(recs)} | {passed} | {rate} | {median([r['llm_calls'] for r in recs], 1)} | "
            f"{median([r['seconds'] for r in recs], 0)} |")


def render_table(runs: list[dict], out: Path, model_note: str) -> str:
    runs = sorted(runs, key=sort_key)
    hazards = list(dict.fromkeys(r["hazard"] for r in runs))
    lines = [
        "# Reliability matrix",
        "",
        f"Method: each row runs the Acme invoice task (`{rel(CONTRACT)}`) once per seed against a freshly reset "
        f"company app with that hazard switched on and fresh agent state; a run passes only when its stored "
        f"receipt (`{rel(out)}/<run_id>/receipt.json`) reports the expected status (`{EXPECTED}`).",
        "",
    ]
    if model_note:
        lines += [f"Model: {model_note}", ""]
    lines += [HEADER, "|---|---:|---:|---:|---:|---:|"]
    lines += [row(h, [r for r in runs if r["hazard"] == h]) for h in hazards]
    lines += [row("**all**", runs), "", "## Notes", ""]
    for h in hazards:
        recs = [r for r in runs if r["hazard"] == h]
        fired = sum(r["hazards_fired"] or 0 for r in recs)
        lines.append(f"- **{h}**: {DESCRIPTIONS.get(h, 'custom hazard.')} Chaos log entries: {fired} over "
                     f"{len(recs)} runs.")
    failures = [r for r in runs if r["status"] != r["expected"]]
    lines.append("- Failures: none." if not failures else "- Failures:")
    lines += [f"  - `{r['run_id']}`: expected {r['expected']}, got {r['status']} (exit {r['exit_code']})"
              for r in failures]
    lines.append("- Medians skip runs without a value; seconds are wall-clock per agent process.")
    return "\n".join(lines) + "\n"


def write_outputs(out: Path, table_path: Path, meta: dict, runs: list[dict]) -> str:
    save_matrix(out, meta, runs)
    table = render_table(runs, out, meta.get("model_note") or "")
    table_path.parent.mkdir(parents=True, exist_ok=True)
    table_path.write_text(table, encoding="utf-8")
    return table


def load_resumable(out: Path) -> dict[str, dict]:
    """Records from an earlier matrix.json whose receipt is stored and whose status is a real outcome."""
    try:
        prev = json.loads((out / "matrix.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return {r["run_id"]: r for r in prev.get("runs", [])
            if r.get("status") not in ("MISSING", "TIMEOUT") and (out / r["run_id"] / "receipt.json").is_file()}


def run_matrix(hazards: list[str], seeds: int, out: Path, table_path: Path, model_note: str, resume: bool) -> None:
    meta = {"task": TASK, "contract": rel(CONTRACT), "model_note": model_note}
    kept = load_resumable(out) if resume else {}
    runs = dict(kept)
    for hazard in hazards:
        todo = [s for s in range(1, seeds + 1) if run_id(hazard, s) not in kept]
        if not todo:
            print(f"[{hazard}] all {seeds} runs already stored, skipping", flush=True)
            continue
        with company_app(env=isolated_env()) as base:
            for seed in todo:
                rid = run_id(hazard, seed)
                reset(base, HAZARDS[hazard])
                env = isolated_env()
                code, seconds, log = run_agent(env, base, rid)
                rec = harvest(Path(env["RECEIPTS_RUNS"]), out, hazard, seed, rid, code, seconds, hazards_fired(base))
                runs[rid] = rec
                save_matrix(out, meta, list(runs.values()))
                verdict = "pass" if rec["status"] == rec["expected"] else "FAIL"
                print(f"[{hazard} {seed}/{seeds}] {rid}: {rec['status']} ({verdict}) exit={code} "
                      f"llm_calls={rec['llm_calls']} {seconds:.0f}s fired={rec['hazards_fired']} log={log}",
                      flush=True)
    print(write_outputs(out, table_path, meta, list(runs.values())), flush=True)
    print(f"matrix: {rel(out / 'matrix.json')}  table: {rel(table_path)}", flush=True)


def selftest() -> None:
    """Offline: synthetic agent outputs -> harvest -> matrix.json + table -> re-parse and check the numbers."""
    tmp = Path(tempfile.mkdtemp(prefix="receipts-matrix-selftest-"))
    runs_dir, out, table_path = tmp / "agent-runs", tmp / "evidence", tmp / "reliability.md"
    # (hazard, seed, status or None for no receipt, exit code, llm_calls, seconds, fired)
    plan = [
        ("baseline", 1, "VERIFIED", 0, 8, 60.0, 0),
        ("baseline", 2, "VERIFIED", 0, 10, 70.0, 0),
        ("baseline", 3, "VERIFIED", 0, 9, 80.0, 0),
        ("save_500", 1, "VERIFIED", 0, 12, 90.0, 2),
        ("save_500", 2, "VERIFIED_WITH_EXCEPTIONS", 0, 14, 100.0, 2),  # exit 0, but not the expected status
        ("save_500", 3, "VERIFIED", 0, 11, 95.0, 2),
        ("date_rule", 1, "VERIFIED", 0, 13, 120.0, 1),
        ("date_rule", 2, None, 2, None, 30.0, 1),  # agent died before writing a receipt
    ]
    runs = []
    for hazard, seed, status, code, calls, secs, fired in plan:
        rid = run_id(hazard, seed)
        src = runs_dir / rid
        src.mkdir(parents=True)
        (src / "events.jsonl").write_text(json.dumps({"ts": 0.0, "type": "run_started", "run_id": rid}) + "\n",
                                          encoding="utf-8")
        if status:
            (src / "receipt.json").write_text(json.dumps({"run_id": rid, "status": status,
                                                          "stats": {"llm_calls": calls}}), encoding="utf-8")
            (src / "receipt.md").write_text(f"# Receipt {rid} — {status}\n", encoding="utf-8")
        # a stale receipt from an older matrix must not be mistaken for this run's
        (out / rid).mkdir(parents=True, exist_ok=True)
        (out / rid / "receipt.json").write_text(json.dumps({"status": EXPECTED}), encoding="utf-8")
        runs.append(harvest(runs_dir, out, hazard, seed, rid, code, secs, fired))
    meta = {"task": TASK, "contract": rel(CONTRACT), "model_note": "selftest"}
    table = write_outputs(out, table_path, meta, runs)
    text = table_path.read_text(encoding="utf-8")
    assert text == table and HEADER in text.splitlines(), "table header line present"

    def parse(label: str) -> tuple[int, int, str]:
        m = re.search(rf"^\|\s*{re.escape(label)}\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|", text, re.M)
        assert m, f"row for {label} found"
        return int(m[1]), int(m[2]), m[3]

    assert parse("baseline") == (3, 3, "100%"), parse("baseline")
    assert parse("save_500") == (3, 2, "67%"), parse("save_500")
    assert parse("date_rule") == (2, 1, "50%"), parse("date_rule")
    assert parse(r"**all**") == (8, 6, "75%"), parse("**all**")
    assert "| baseline | 3 | 3 | 100% | 9 | 70 |" in text, "baseline medians"
    assert "| date_rule | 2 | 1 | 50% | 13 | 75 |" in text, "medians skip missing llm_calls"
    order = [ln.split("|")[1].strip() for ln in text.splitlines() if re.match(r"^\| [A-Za-z*]", ln)]
    assert order == ["Hazard", "baseline", "save_500", "date_rule", "**all**"], order

    stored = json.loads((out / "matrix.json").read_text(encoding="utf-8"))
    assert set(stored) >= {"generated", "task", "model_note", "runs"}, "matrix.json keys"
    by_id = {r["run_id"]: r for r in stored["runs"]}
    assert len(by_id) == 8
    assert set(by_id["matrix-baseline-1"]) == {"hazard", "seed", "run_id", "expected", "status", "exit_code",
                                               "llm_calls", "seconds", "hazards_fired"}, "record keys"
    assert by_id["matrix-date_rule-2"]["status"] == "MISSING", "stale receipt replaced, no receipt -> MISSING"
    assert not (out / "matrix-date_rule-2" / "receipt.json").exists(), "stale receipt removed"
    assert (out / "matrix-date_rule-2" / "events.jsonl").is_file(), "events copied even without a receipt"
    assert (out / "matrix-baseline-1" / "receipt.md").is_file(), "receipt.md copied"
    mismatch = by_id["matrix-save_500-2"]
    assert mismatch["exit_code"] == 0 and mismatch["status"] != mismatch["expected"], "exit 0 alone is not a pass"
    assert "`matrix-save_500-2`: expected VERIFIED, got VERIFIED_WITH_EXCEPTIONS" in text, "failure listed"
    assert "`matrix-date_rule-2`: expected VERIFIED, got MISSING" in text, "missing run listed"
    assert "`matrix-baseline-1`" not in text, "passing runs not listed as failures"

    assert harvest(runs_dir, out, "baseline", 1, "matrix-baseline-1", None, 1800.0, 0)["status"] == "TIMEOUT"
    resumable = load_resumable(out)
    assert "matrix-baseline-1" in resumable and "matrix-date_rule-2" not in resumable, "resume skips only real receipts"
    shutil.rmtree(tmp, ignore_errors=True)
    print("MATRIX SELFTEST OK")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--seeds", type=int, default=5, help="runs per hazard (default 5)")
    p.add_argument("--hazards", default=",".join(HAZARDS), help=f"comma list from: {', '.join(HAZARDS)}")
    p.add_argument("--out", default="docs/evidence/matrix", help="evidence dir (receipts + matrix.json)")
    p.add_argument("--table", default="docs/reliability.md", help="markdown table output")
    p.add_argument("--model-note", default="", help="free text recorded in matrix.json and the table")
    p.add_argument("--resume", action="store_true", help="skip run_ids already stored with a receipt")
    p.add_argument("--selftest", action="store_true", help="offline check of the table/matrix code paths")
    args = p.parse_args()
    sys.stdout.reconfigure(errors="replace")  # a non-ASCII --model-note must not crash a cp1252 console
    if args.selftest:
        selftest()
        return
    hazards = [h.strip() for h in args.hazards.split(",") if h.strip()]
    unknown = [h for h in hazards if h not in HAZARDS]
    if unknown or not hazards:
        p.error(f"unknown hazards {unknown}; choose from {', '.join(HAZARDS)}")
    if args.seeds < 1:
        p.error("--seeds must be >= 1")
    run_matrix(list(dict.fromkeys(hazards)), args.seeds, ROOT / args.out, ROOT / args.table, args.model_note,
               args.resume)


if __name__ == "__main__":
    main()
