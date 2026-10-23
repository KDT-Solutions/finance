from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from . import __version__
from .db import init_db
from .routes import mcp, ui

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="finance", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(ui.router)
app.include_router(mcp.router)


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": __version__}


@app.exception_handler(StarletteHTTPException)
async def _redirects(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 303 and exc.headers and "Location" in exc.headers:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    from fastapi.exception_handlers import http_exception_handler

    return await http_exception_handler(request, exc)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src 'self'; object-src 'self'")
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


# CORS only for the MCP endpoint: browser-based MCP clients (claude.ai) call it cross-origin and
# send a preflight OPTIONS without the Authorization header. The UI keeps same-origin policy.
_cors_mcp = CORSMiddleware(app, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                           expose_headers=["Mcp-Session-Id", "MCP-Protocol-Version"])


async def application(scope, receive, send):
    if scope["type"] == "http" and scope.get("path", "").rstrip("/") == "/mcp":
        await _cors_mcp(scope, receive, send)
        return
    await app(scope, receive, send)
