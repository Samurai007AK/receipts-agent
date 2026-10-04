"""Shared helpers for check scripts: run servers on free ports, isolate state, assert."""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator

import httpx

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# Check output goes through cp1252 pipes on Windows; never crash on a non-ASCII character.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", flush=True)
    sys.exit(1)


def check(cond: object, msg: str) -> None:
    if not cond:
        fail(msg)
    print(f"  ok  {msg}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def isolated_env() -> dict[str, str]:
    """Env vars pointing all mutable state at a fresh temp dir."""
    tmp = Path(tempfile.mkdtemp(prefix="receipts-check-"))
    return {
        "RECEIPTS_DB": str(tmp / "receipts.db"),
        "RECEIPTS_RUNS": str(tmp / "runs"),
        "RECEIPTS_MEMORY": str(tmp / "site_notes.json"),
        "RECEIPTS_OVERRIDES": str(tmp / "overrides.json"),
    }


def wait_http(url: str, timeout: float = 40) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    fail(f"server at {url} did not come up")


@contextlib.contextmanager
def serve(module: str, port: int | None = None, env: dict[str, str] | None = None,
          health: str = "/api/health") -> Iterator[str]:
    """Run `uvicorn <module>:app` from the repo root; yield its base URL."""
    port = port or free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", f"{module}:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env={**os.environ, **(env or {})},
    )
    try:
        wait_http(base + health)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def company_app(**kw) -> contextlib.AbstractContextManager[str]:
    return serve("app", **kw)


def stub_app(**kw) -> contextlib.AbstractContextManager[str]:
    return serve("checks.stub_company", **kw)


def reset(base_url: str, chaos: dict | None = None) -> dict:
    r = httpx.post(f"{base_url}/api/reset", json={"chaos": chaos or {}}, timeout=10)
    r.raise_for_status()
    return r.json()


def login_client(base_url: str) -> httpx.Client:
    c = httpx.Client(base_url=base_url, follow_redirects=False, timeout=10)
    r = c.post("/login", data={"username": "clerk", "password": "receipts123", "next": "/"})
    if r.status_code != 303:
        fail(f"login failed: HTTP {r.status_code}")
    return c
