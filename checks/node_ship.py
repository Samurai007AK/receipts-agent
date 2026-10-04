"""Ship-level checks (setup, to-do, generalization, evidence, docs, push).

Usage: python checks/node_ship.py <mode>; each mode prints its success token last.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from checks._servers import ROOT, check, fail  # noqa: E402

EVIDENCE = ROOT / "docs" / "evidence"
CORE = ("agent.py", "gateway.py", "referee.py", "browser.py")


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def mode_setup() -> None:
    import importlib
    for mod in ("fastapi", "uvicorn", "jinja2", "multipart", "httpx", "playwright", "openai", "dotenv"):
        importlib.import_module(mod)
    check(True, "all requirements import")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.set_content("<h1>ok</h1>")
        check(page.inner_text("h1") == "ok", "Chromium launches and renders")
        b.close()
    import httpx
    from dotenv import dotenv_values
    key = dotenv_values(ROOT / ".env").get("GROQ_API_KEY", "")
    check(key.startswith("gsk_"), "GROQ_API_KEY present in .env")
    r = httpx.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    check(r.status_code == 200 and any(m["id"] == "openai/gpt-oss-120b" for m in r.json()["data"]),
          "Groq key authenticates and the primary model is available")
    check(git("check-ignore", ".env").returncode == 0, ".env is git-ignored")
    check(git("ls-files", ".env").stdout.strip() == "", ".env is not tracked")
    check(git("log", "--oneline", "-1", "--", "plan.md").stdout.strip() != "", "plan.md committed")
    print("SETUP VERIFIED")


def mode_todo() -> None:
    text = (ROOT / "to-do.md").read_text(encoding="utf-8")
    items = re.findall(r"^- \[(.)\] (.+)$", text, re.M)
    check(len(items) >= 60, f"to-do has {len(items)} items")
    open_items = [t for s, t in items if s in (" ", "~")]
    check(not open_items, f"no open items (open: {open_items[:3]})")
    dropped = [t for s, t in items if s == "-"]
    check(all(re.search(r"(dropped|reason|—|because)", t, re.I) for t in dropped), "every dropped item has a reason")
    log = text.split("## Progress log", 1)[1]
    for n in range(12):
        check(re.search(rf"Phase {n}\b", log), f"progress log mentions Phase {n}")
    print("TODO VERIFIED")


def mode_console_live() -> None:
    """Drive the real console + real agent (Groq) through preset A with chaos on; keep a screenshot."""
    import httpx
    from playwright.sync_api import expect, sync_playwright

    from checks._servers import company_app, isolated_env, reset, serve
    env = isolated_env()
    with company_app(env=env) as app_url, \
            serve("console", env={**env, "RECEIPTS_APP_URL": app_url}) as console_url:
        reset(app_url, {"save_500": 1, "session_expiry": True, "poisoned_email": True})
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(console_url)
            page.locator('.preset[data-id="A"]').click()
            page.locator("#run-btn").click()
            page.wait_for_function("location.hash.startsWith('#run=')")
            run_id = page.evaluate("location.hash.slice(5)")
            approve = page.locator("#contract-approve")
            expect(approve).to_be_visible(timeout=600_000)
            page.screenshot(path=str(ROOT / "docs" / "img" / "console-live-contract.png"), full_page=True)
            check(page.locator("#clauses li").count() >= 3, "generated contract shown as a checklist")
            approve.click()
            expect(page.locator("#receipt-status")).to_have_text("VERIFIED", timeout=1_500_000)
            page.screenshot(path=str(ROOT / "docs" / "img" / "console-live.png"), full_page=True)
            check(page.locator("#log .ev.k-detected.moment").count() >= 1, "live log shows a DETECTED moment")
            browser.close()
        r = httpx.get(f"{console_url}/api/runs/{run_id}/receipt", timeout=10).json()
        check(r["status"] == "VERIFIED", "receipt from the real agent is VERIFIED")
        rows = httpx.get(f"{app_url}/api/ledger", params={"invoice_no": "INV-1042"}, timeout=10).json()
        check(len(rows) == 1, "exactly one INV-1042 row written through the console run")
        check(not httpx.get(f"{app_url}/api/ledger", params={"invoice_no": "ZRG-1"}, timeout=10).json(),
              "hidden instruction in the email did not create a payable")
    print("CONSOLE LIVE VERIFIED")


def mode_core_unchanged() -> None:
    check(git("rev-parse", "--verify", "pre-catalog").returncode == 0, "tag pre-catalog exists")
    diff = git("diff", "--name-only", "pre-catalog", "HEAD", "--", *CORE).stdout.strip()
    check(diff == "", f"core files unchanged since pre-catalog (changed: {diff or 'none'})")
    dirty = git("diff", "--name-only", "HEAD", "--", *CORE).stdout.strip()
    check(dirty == "", "core files have no uncommitted edits")
    domain = git("diff", "--name-only", "pre-catalog", "HEAD", "--", "app.py", "policy.py").stdout.split()
    check("app.py" in domain, "control: the catalog work did change the environment (app.py)")
    print("CORE UNCHANGED VERIFIED")


def mode_matrix() -> None:
    receipts = sorted((EVIDENCE / "matrix").glob("*/receipt.json"))
    check(len(receipts) >= 5, f"{len(receipts)} matrix receipts stored")
    meta = load(EVIDENCE / "matrix" / "matrix.json")
    stats: dict[str, list[bool]] = {}
    for run in meta["runs"]:
        r = load(EVIDENCE / "matrix" / run["run_id"] / "receipt.json")
        stats.setdefault(run["hazard"], []).append(r["status"] == run["expected"])
    table = (ROOT / "docs" / "reliability.md").read_text(encoding="utf-8")
    for hazard, oks in stats.items():
        row = re.search(rf"^\|\s*{re.escape(hazard)}\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|", table, re.M)
        check(row, f"reliability table has a row for {hazard}")
        check(int(row.group(1)) == len(oks) and int(row.group(2)) == sum(oks),
              f"{hazard}: table says {row.group(2)}/{row.group(1)}, receipts say {sum(oks)}/{len(oks)}")
    print("MATRIX VERIFIED")


def mode_memory() -> None:
    trial = load(EVIDENCE / "memory" / "trial.json")
    r1 = load(EVIDENCE / "memory" / trial["first"] / "receipt.json")
    r2 = load(EVIDENCE / "memory" / trial["second"] / "receipt.json")
    check(r1["status"] == "VERIFIED" and r2["status"] == "VERIFIED", "both memory-trial runs VERIFIED")
    s1, s2 = r1["stats"]["llm_calls"], r2["stats"]["llm_calls"]
    check(s2 < s1, f"second run used fewer LLM calls ({s2} < {s1})")
    check(trial.get("notes"), "the notes saved by the first run are recorded")
    print("SITE MEMORY VERIFIED")


def mode_readme() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    heads = [h.lower() for h in re.findall(r"^##\s+(.+)$", text, re.M)]
    for need in ("how it works", "architecture", "quickstart", "demo", "judging", "repository"):
        check(any(need in h for h in heads), f"README has a section about '{need}'")
    check("```mermaid" in text, "README has an architecture diagram")
    for cmd in ("pip install -r requirements.txt", "playwright install chromium", "uvicorn app:app",
                "uvicorn console:app", "agent.py"):
        check(cmd in text, f"README quickstart mentions `{cmd}`")
    for link in re.findall(r"\]\((?!https?://|#)([^)#]+)", text):
        check((ROOT / link).exists(), f"relative link {link} exists")
    check(not re.search(r"GROQ_API_KEY=gsk_", text), "README leaks no key")
    print("README VERIFIED")


def mode_slides() -> None:
    text = (ROOT / "docs" / "slides.md").read_text(encoding="utf-8")
    slides = [s for s in re.split(r"^---\s*$", text, flags=re.M) if s.strip()]
    check(len(slides) >= 7, f"{len(slides)} slides")
    low = text.lower()
    for word in ("problem", "llm proposes", "referee", "gateway", "chaos", "demo", "autonomy", "generalization"):
        check(word in low, f"slides cover '{word}'")
    print("SLIDES VERIFIED")


def mode_rehearsal() -> None:
    meta = load(EVIDENCE / "rehearsal" / "rehearsal.json")
    check(len(meta["passes"]) >= 3, f"{len(meta['passes'])} rehearsal passes recorded")
    for i, p in enumerate(meta["passes"], 1):
        for step in p["steps"]:
            r = load(EVIDENCE / "rehearsal" / step["run_id"] / "receipt.json")
            check(r["status"] == step["expected"], f"pass {i} {step['name']}: {r['status']} == {step['expected']}")
    print("REHEARSAL VERIFIED")


def mode_video() -> None:
    vids = sorted(EVIDENCE.glob("*.webm")) + sorted(EVIDENCE.glob("*.mp4"))
    check(vids, "a demo video exists in docs/evidence")
    v = vids[0]
    head = v.read_bytes()[:12]
    check(head.startswith(b"\x1a\x45\xdf\xa3") or head[4:8] == b"ftyp", f"{v.name} is a real WebM/MP4 file")
    check(v.stat().st_size > 150_000, f"{v.name} is {v.stat().st_size} bytes")
    meta = load(EVIDENCE / "video.json")
    check(load(EVIDENCE / meta["run_dir"] / "receipt.json")["status"] in ("VERIFIED", "VERIFIED_WITH_EXCEPTIONS"),
          "the recorded run was verified by the referee")
    print("VIDEO VERIFIED")


def mode_pushed() -> None:
    remote = git("remote", "get-url", "origin")
    check(remote.returncode == 0, f"origin remote set ({remote.stdout.strip()})")
    head = git("rev-parse", "HEAD").stdout.strip()
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    ls = git("ls-remote", "origin", f"refs/heads/{branch}").stdout.split()
    check(ls and ls[0] == head, f"origin/{branch} == local HEAD {head[:8]}")
    check(git("status", "--porcelain").stdout.strip() == "", "working tree clean")
    print("PUSH VERIFIED")


if __name__ == "__main__":
    modes = {name[5:].replace("_", "-"): fn for name, fn in globals().items() if name.startswith("mode_")}
    if len(sys.argv) != 2 or sys.argv[1] not in modes:
        fail(f"usage: node_ship.py <{'|'.join(sorted(modes))}>")
    modes[sys.argv[1]]()
