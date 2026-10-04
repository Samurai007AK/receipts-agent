"""Single-port launcher for a Hugging Face Docker Space.

A Space exposes one port. This runs the company app privately on 127.0.0.1:8000 (the agent's
browser and the referee talk to it there) and serves, on $PORT (7860):

    /            the operator console
    /company/... a reverse proxy to the company app, so people can open the chaos panel and pages
"""
from __future__ import annotations

import atexit
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Mount, Route

ROOT = Path(__file__).resolve().parent
INTERNAL = "http://127.0.0.1:8000"
PREFIX = "/company"
os.environ.setdefault("RECEIPTS_APP_URL", INTERNAL)
os.environ.setdefault("RECEIPTS_PUBLIC_APP_URL", PREFIX)

# Root-relative links in the company app's HTML, and "/api/..." strings in its inline JS.
_LINK = re.compile(r"""((?:href|action|src)=["'])/(?!/|company/)""")
_JS_API = re.compile(r"""(["'`])/api/""")
_HOP = {"host", "content-length", "accept-encoding", "connection", "transfer-encoding"}
_DROP = {"content-length", "content-encoding", "transfer-encoding", "connection"}


def start_company_app() -> None:
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000",
                             "--log-level", "warning"], cwd=ROOT)
    atexit.register(proc.terminate)
    for _ in range(120):
        try:
            if httpx.get(f"{INTERNAL}/api/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.5)
    raise RuntimeError("company app did not start")


def rewrite_html(html: str) -> str:
    return _JS_API.sub(rf"\1{PREFIX}/api/", _LINK.sub(rf"\1{PREFIX}/", html))


async def company(request: Request) -> Response:
    path = request.path_params.get("path", "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    async with httpx.AsyncClient(timeout=30) as client:
        upstream = await client.request(request.method, f"{INTERNAL}/{path}", params=request.query_params,
                                        content=await request.body(), headers=headers)
    body = upstream.content
    if "text/html" in upstream.headers.get("content-type", ""):
        body = rewrite_html(upstream.text).encode("utf-8")
    response = Response(body, status_code=upstream.status_code)
    response.raw_headers = [(b"content-length", str(len(body)).encode())]
    for key, value in upstream.headers.multi_items():
        if key.lower() in _DROP:
            continue
        if key.lower() == "location" and value.startswith("/") and not value.startswith("//"):
            value = PREFIX + value
        response.raw_headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
    return response


def build() -> Starlette:
    import console  # imported after the env vars above are set
    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(routes=[
        Route(PREFIX, lambda r: RedirectResponse(PREFIX + "/")),
        Route(PREFIX + "/{path:path}", company, methods=methods),
        Mount("/", app=console.app),
    ])


if __name__ == "__main__":
    start_company_app()
    uvicorn.run(build(), host="0.0.0.0", port=int(os.environ.get("PORT", "7860")), log_level="info")
