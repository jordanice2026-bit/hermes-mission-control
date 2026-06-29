"""
Hermes Mission Control Dashboard
- Google OAuth login
- REST API for Kanban board data
- SSE for real-time updates
- Sync endpoint for Hermes agent to push task updates
"""

import os
import json
import time
import asyncio
import secrets
import logging
from typing import Optional
from collections import defaultdict

import httpx
from fastapi import FastAPI, Request, HTTPException, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

_ENV = os.environ.get

GOOGLE_CLIENT_ID = _ENV("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = _ENV("GOOGLE_CLIENT_SECRET", "")
SESSION_SECRET = _ENV("SESSION_SECRET", secrets.token_hex(32))
SYNC_TOKEN = _ENV("SYNC_TOKEN", secrets.token_hex(32))
ALLOWED_EMAILS_RAW = _ENV("ALLOWED_EMAILS", "")
ALLOWED_EMAILS = set(e.strip().lower() for e in ALLOWED_EMAILS_RAW.split(",") if e.strip())

HOST = _ENV("RENDER_EXTERNAL_URL", "http://localhost:8000").rstrip("/")
OAUTH_REDIRECT_URI = f"{HOST}/auth/callback"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# ---------------------------------------------------------------------------
# In-memory store (task board + SSE subscribers)
# ---------------------------------------------------------------------------

_board: dict[str, dict] = {}
_sse_queues: list[asyncio.Queue] = []


def _broadcast(event_type: str, data: dict):
    msg = {"type": event_type, "data": data, "ts": int(time.time() * 1000)}
    dead = []
    for q in _sse_queues:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        try:
            _sse_queues.remove(q)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Hermes Mission Control", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    max_age=86400 * 7,
    https_only=HOST.startswith("https"),
)

app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def get_current_user(request: Request) -> Optional[dict]:
    return request.session.get("user")


def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def require_sync_token(request: Request):
    auth = request.headers.get("Authorization", "")
    if not SYNC_TOKEN or auth != f"Bearer {SYNC_TOKEN}":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid sync token")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login")
    with open("static/index.html") as f:
        return HTMLResponse(f.read())


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/")
    with open("static/login.html") as f:
        return HTMLResponse(f.read())


# ---------------------------------------------------------------------------
# Google OAuth flow
# ---------------------------------------------------------------------------


@app.get("/auth/login")
async def auth_login(request: Request):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(500, "Google OAuth not configured")
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{qs}")


@app.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
):
    if error:
        return RedirectResponse(f"/login?error={error}")
    saved_state = request.session.get("oauth_state")
    if not state or state != saved_state:
        return RedirectResponse("/login?error=state_mismatch")
    if not code:
        return RedirectResponse("/login?error=no_code")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            logger.error("Token exchange failed: %s", token_resp.text)
            return RedirectResponse("/login?error=token_exchange_failed")
        tokens = token_resp.json()
        access_token = tokens.get("access_token")

        user_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code != 200:
            return RedirectResponse("/login?error=userinfo_failed")
        user_info = user_resp.json()

    email = user_info.get("email", "").lower()
    if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
        logger.warning("Blocked login attempt from %s", email)
        return RedirectResponse("/login?error=not_allowed")

    request.session["user"] = {
        "email": email,
        "name": user_info.get("name", email),
        "picture": user_info.get("picture", ""),
    }
    request.session.pop("oauth_state", None)
    logger.info("User logged in: %s", email)
    return RedirectResponse("/")


@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


@app.get("/auth/me")
async def auth_me(request: Request):
    user = get_current_user(request)
    if not user:
        return JSONResponse({"authenticated": False})
    return JSONResponse({"authenticated": True, "user": user})


# ---------------------------------------------------------------------------
# Kanban API (read)
# ---------------------------------------------------------------------------


@app.get("/api/board")
async def get_board(user: dict = Depends(require_user)):
    tasks = list(_board.values())
    columns: dict[str, list] = {
        "pending": [],
        "in_progress": [],
        "blocked": [],
        "done": [],
        "cancelled": [],
    }
    for task in sorted(tasks, key=lambda t: t.get("created_at", 0)):
        s = task.get("status", "pending")
        if s not in columns:
            columns[s] = []
        columns[s].append(task)
    return JSONResponse({
        "columns": columns,
        "total": len(tasks),
        "updated_at": int(time.time() * 1000),
    })


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, user: dict = Depends(require_user)):
    task = _board.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return JSONResponse(task)


@app.get("/api/stats")
async def get_stats(user: dict = Depends(require_user)):
    tasks = list(_board.values())
    by_status: dict[str, int] = defaultdict(int)
    by_assignee: dict[str, int] = defaultdict(int)
    for t in tasks:
        by_status[t.get("status", "unknown")] += 1
        assignee = t.get("assignee") or "unassigned"
        by_assignee[assignee] += 1
    return JSONResponse({
        "total": len(tasks),
        "by_status": dict(by_status),
        "by_assignee": dict(by_assignee),
    })


# ---------------------------------------------------------------------------
# SSE real-time stream
# ---------------------------------------------------------------------------


@app.get("/api/events")
async def sse_events(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _sse_queues.append(queue)

    async def event_stream():
        snapshot = {"type": "snapshot", "data": list(_board.values()), "ts": int(time.time() * 1000)}
        yield f"data: {json.dumps(snapshot)}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'ping', 'ts': int(time.time()*1000)})}\n\n"
        finally:
            try:
                _sse_queues.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Sync endpoint — Hermes agent POSTs here to push board state
# ---------------------------------------------------------------------------


class TaskSyncPayload(BaseModel):
    tasks: list[dict]


@app.post("/api/sync")
async def sync_tasks(payload: TaskSyncPayload, _=Depends(require_sync_token)):
    """Full board sync — Hermes posts the complete task list."""
    new_board: dict[str, dict] = {}
    for task in payload.tasks:
        tid = task.get("id")
        if tid:
            new_board[tid] = task

    all_ids = set(new_board) | set(_board)
    changed = []
    for tid in all_ids:
        old = _board.get(tid)
        new = new_board.get(tid)
        if old != new:
            changed.append(new if new else {"id": tid, "_deleted": True})

    _board.clear()
    _board.update(new_board)

    if changed:
        _broadcast("update", {"changed": changed})

    logger.info("Sync: %d tasks, %d changed", len(new_board), len(changed))
    return JSONResponse({"ok": True, "total": len(new_board), "changed": len(changed)})


@app.post("/api/sync/task")
async def sync_single_task(request: Request, _=Depends(require_sync_token)):
    """Single task upsert."""
    body = await request.json()
    tid = body.get("id")
    if not tid:
        raise HTTPException(400, "task id required")
    old = _board.get(tid)
    _board[tid] = body
    if old != body:
        _broadcast("task_update", body)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"ok": True, "tasks": len(_board), "clients": len(_sse_queues)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        reload=False,
    )
