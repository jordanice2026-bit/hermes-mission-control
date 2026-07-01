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
# Department configuration — maps agent roles → business departments
# ---------------------------------------------------------------------------

DEPARTMENTS: dict[str, dict] = {
    "Sourcing": {
        "icon": "🔍",
        "color": "#2563eb",
        "description": "Finds on-market Indiana investment properties & owners",
        "agents": ["property-sourcer", "owner-researcher", "buyer-sourcer"],
    },
    "Underwriting": {
        "icon": "📊",
        "color": "#7c3aed",
        "description": "Runs financial models & screens deals",
        "agents": ["underwriter", "deal-screener"],
    },
    "Matchmaking": {
        "icon": "🎯",
        "color": "#db2777",
        "description": "Matches deals to buyers & drafts outreach",
        "agents": ["matchmaker", "investor-profiler"],
    },
    "Outreach": {
        "icon": "📬",
        "color": "#059669",
        "description": "Contacts owners & prospects via email and direct mail",
        "agents": ["prospector", "lead-agent", "marketing-agent", "client-agent"],
    },
    "Transaction Coordination": {
        "icon": "📋",
        "color": "#ca8a04",
        "description": "Monitors deals, deadlines & inbound party communication",
        "agents": ["inbox-monitor"],
    },
    "Management": {
        "icon": "🧭",
        "color": "#dc2626",
        "description": "Oversees the org, market research & reports to you",
        "agents": ["manager", "research-agent"],
    },
}


def _agent_department(agent_name: str) -> str:
    for dept, cfg in DEPARTMENTS.items():
        if agent_name in cfg["agents"]:
            return dept
    return "Management"


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

# Pipeline router (Notion + Gmail — seller outreach)
from pipeline import router as pipeline_router
app.include_router(pipeline_router)

# Transaction Coordination router
try:
    from tc import router as tc_router
    app.include_router(tc_router)
except Exception as _tc_err:
    logger.warning("TC router failed to load: %s", _tc_err)

# Advanced features router (alerts, SLA, analytics, calendar, search, settings, templates, exports)
try:
    from extras import router as extras_router
    app.include_router(extras_router)
except Exception as _ex_err:
    logger.warning("Extras router failed to load: %s", _ex_err)

# Listing Management router (listings, doc upload, checklist, auto-promote to deals)
try:
    from listings import router as listings_router
    app.include_router(listings_router)
except Exception as _ls_err:
    logger.warning("Listings router failed to load: %s", _ls_err)

# Manager Console router (agent-corporation health, proposals, approve/reject)
try:
    from manager import router as manager_router
    app.include_router(manager_router)
except Exception as _mg_err:
    logger.warning("Manager router failed to load: %s", _mg_err)


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


@app.get("/api/agents")
async def get_agents(user: dict = Depends(require_user)):
    tasks = list(_board.values())
    STATUS_KEYS = ["pending", "in_progress", "blocked", "done", "cancelled"]
    agents: dict[str, dict] = {}

    for t in tasks:
        name = t.get("assignee") or "unassigned"
        if name not in agents:
            agents[name] = {k: 0 for k in STATUS_KEYS}
            agents[name]["other"] = 0
            agents[name]["total"] = 0
            agents[name]["last_active"] = 0
        s = t.get("status", "pending")
        if s in STATUS_KEYS:
            agents[name][s] += 1
        else:
            agents[name]["other"] += 1
        agents[name]["total"] += 1
        ts = max(
            t.get("last_heartbeat_at") or 0,
            t.get("started_at") or 0,
            t.get("completed_at") or 0,
            t.get("created_at") or 0,
        )
        if ts > agents[name]["last_active"]:
            agents[name]["last_active"] = ts

    result = []
    for name, stats in sorted(agents.items(), key=lambda x: -x[1]["total"]):
        result.append({
            "name": name,
            "department": _agent_department(name),
            "stats": stats,
            "is_active": stats["in_progress"] > 0,
        })

    return JSONResponse({"agents": result})


@app.get("/api/departments")
async def get_departments(user: dict = Depends(require_user)):
    tasks = list(_board.values())
    STATUS_KEYS = ["pending", "in_progress", "blocked", "done", "cancelled"]

    def empty_stats() -> dict:
        return {k: 0 for k in [*STATUS_KEYS, "total", "last_active"]}

    def empty_agent_stats() -> dict:
        return {**{k: 0 for k in STATUS_KEYS}, "total": 0, "is_active": False}

    # Seed dept_stats from config
    dept_stats: dict[str, dict] = {}
    for dept_name in DEPARTMENTS:
        dept_stats[dept_name] = {**empty_stats(), "agents": {}}

    for t in tasks:
        agent = t.get("assignee") or "unassigned"
        dept = _agent_department(agent)
        if dept not in dept_stats:
            dept_stats[dept] = {**empty_stats(), "agents": {}}
        d = dept_stats[dept]
        s = t.get("status", "pending")
        if s in STATUS_KEYS:
            d[s] += 1
        d["total"] += 1
        ts = max(
            t.get("last_heartbeat_at") or 0,
            t.get("started_at") or 0,
            t.get("completed_at") or 0,
            t.get("created_at") or 0,
        )
        if ts > d["last_active"]:
            d["last_active"] = ts
        if agent not in d["agents"]:
            d["agents"][agent] = empty_agent_stats()
        ag = d["agents"][agent]
        ag[s if s in STATUS_KEYS else "pending"] += 1
        ag["total"] += 1
        if s == "in_progress":
            ag["is_active"] = True

    result = []
    for dept_name, cfg in DEPARTMENTS.items():
        d = dept_stats.get(dept_name, {**empty_stats(), "agents": {}})
        agents_list = [
            {
                "name": a,
                "is_active": d["agents"].get(a, empty_agent_stats()).get("is_active", False),
                "stats": d["agents"].get(a, empty_agent_stats()),
            }
            for a in cfg["agents"]
        ]
        result.append({
            "name": dept_name,
            "icon": cfg["icon"],
            "color": cfg["color"],
            "description": cfg["description"],
            "stats": {k: d.get(k, 0) for k in [*STATUS_KEYS, "total", "last_active"]},
            "agents": agents_list,
            "is_active": d.get("in_progress", 0) > 0,
        })

    return JSONResponse({"departments": result})


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


@app.post("/api/tasks/{task_id}/assign")
async def assign_task(task_id: str, request: Request, user: dict = Depends(require_user)):
    """Reassign a task to an agent/owner. Updates in-memory board + broadcasts."""
    task = _board.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    body = await request.json()
    assignee = (body.get("assignee") or "").strip() or "unassigned"
    task["assignee"] = assignee
    _board[task_id] = task
    _broadcast("task_update", task)
    return JSONResponse({"ok": True, "task_id": task_id, "assignee": assignee})


@app.post("/api/tasks/{task_id}/status")
async def set_task_status(task_id: str, request: Request, user: dict = Depends(require_user)):
    """Change a task's status from the dashboard (e.g. retry a blocked task)."""
    task = _board.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    body = await request.json()
    status = (body.get("status") or "").strip()
    if status not in ("pending", "in_progress", "blocked", "done", "cancelled"):
        raise HTTPException(400, "invalid status")
    task["status"] = status
    if status in ("pending", "in_progress"):
        task.pop("error", None)
        task.pop("last_error", None)
    _board[task_id] = task
    _broadcast("task_update", task)
    return JSONResponse({"ok": True, "task_id": task_id, "status": status})


# ---------------------------------------------------------------------------
# Command Console — control agents (cron jobs) from the dashboard
#
# Architecture: agents run on the VPS, dashboard runs on Render. The console
# queues commands here (Render); a VPS-side worker (command_worker.py) polls
# /api/agent-control/poll every few seconds, executes via `hermes cron`, then
# reports agent state + command results back. The browser reads that state.
# ---------------------------------------------------------------------------

# In-memory stores (Render side)
_command_queue: list[dict] = []      # pending commands for the VPS worker
_command_log: list[dict] = []        # executed command history (most recent first)
_agent_state: dict = {               # last-known state pushed by the VPS worker
    "jobs": [],
    "system_status": "unknown",      # running | paused | unknown
    "updated_at": 0,
    "worker_online": False,
    "team_lessons": [],              # shared cross-agent lessons (org brain)
}
_manager_chat: list[dict] = []       # manager chat transcript (newest last)
_ALLOWED_COMMANDS = {"start_all", "stop_all", "pause_job", "resume_job", "run_job"}


@app.get("/api/agent-control/state")
async def agent_control_state(user: dict = Depends(require_user)):
    """Browser reads current agent/system state."""
    # worker considered online if it checked in within the last 90s
    online = (time.time() - _agent_state.get("updated_at", 0)) < 90
    state = dict(_agent_state)
    state["worker_online"] = online
    return JSONResponse({
        "state": state,
        "pending_commands": len(_command_queue),
        "log": _command_log[:30],
    })


@app.post("/api/agent-control/command")
async def agent_control_command(request: Request, user: dict = Depends(require_user)):
    """Browser queues a command for the VPS worker to execute."""
    body = await request.json()
    action = (body.get("action") or "").strip()
    if action not in _ALLOWED_COMMANDS:
        raise HTTPException(400, f"Unknown action. Allowed: {sorted(_ALLOWED_COMMANDS)}")
    cmd = {
        "id": secrets.token_hex(8),
        "action": action,
        "job_id": body.get("job_id"),
        "issued_by": user.get("email") or user.get("name") or "user",
        "issued_at": int(time.time() * 1000),
        "status": "queued",
    }
    _command_queue.append(cmd)
    logger.info("Command queued: %s %s by %s", action, cmd.get("job_id") or "", cmd["issued_by"])
    return JSONResponse({"ok": True, "command": cmd})


# ---------------------------------------------------------------------------
# Manager chat — text the Manager agent from inside the dashboard.
# Browser POSTs a message; the VPS worker (which has the LLM + kanban CLI)
# picks up pending user messages on its next poll, classifies + dispatches the
# task, and posts the reply back. Browser polls GET /api/manager/chat.
# ---------------------------------------------------------------------------
@app.get("/api/manager/chat")
async def manager_chat_get(request: Request, user: dict = Depends(require_user)):
    return JSONResponse({"messages": _manager_chat[-100:],
                         "worker_online": (time.time() - _agent_state.get("updated_at", 0)) < 90})


@app.post("/api/manager/chat")
async def manager_chat_post(request: Request, user: dict = Depends(require_user)):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "empty message")
    if len(text) > 4000:
        text = text[:4000]
    msg = {
        "id": secrets.token_hex(8),
        "role": "user",
        "text": text,
        "status": "pending",          # pending -> processing -> done
        "ts": int(time.time() * 1000),
        "issued_by": user.get("email") or user.get("name") or "user",
    }
    _manager_chat.append(msg)
    del _manager_chat[:-200]          # keep last 200
    return JSONResponse({"ok": True, "message": msg})


@app.post("/api/agent-control/poll")
async def agent_control_poll(request: Request, _=Depends(require_sync_token)):
    """VPS worker: fetch queued commands + push current agent state.

    Body: {"jobs": [...], "system_status": "...", "results": [{id,status,output}]}
    Returns: {"commands": [...]} — the queued commands to execute this tick.
    """
    body = await request.json()

    # 1. Update agent state snapshot
    if "jobs" in body:
        _agent_state["jobs"] = body["jobs"]
    if "system_status" in body:
        _agent_state["system_status"] = body["system_status"]
    if "team_lessons" in body:
        _agent_state["team_lessons"] = body["team_lessons"]
    _agent_state["updated_at"] = time.time()

    # 2. Record any results the worker reports for previously-issued commands
    for res in body.get("results", []):
        cid = res.get("id")
        for c in _command_queue:
            if c["id"] == cid:
                c["status"] = res.get("status", "done")
                c["output"] = (res.get("output") or "")[:500]
                c["completed_at"] = int(time.time() * 1000)
                _command_log.insert(0, c)
        # remove completed from the queue
    done_ids = {r.get("id") for r in body.get("results", [])}
    remaining = [c for c in _command_queue if c["id"] not in done_ids]
    _command_queue.clear()
    _command_queue.extend(remaining)
    # trim log
    del _command_log[100:]

    # 2b. Manager chat — accept status updates + assistant replies from the worker
    for upd in body.get("chat_updates", []):
        mid = upd.get("id")
        for m in _manager_chat:
            if m["id"] == mid:
                if upd.get("status"):
                    m["status"] = upd["status"]
                break
        reply = upd.get("reply")
        if reply:
            _manager_chat.append({
                "id": secrets.token_hex(8),
                "role": "assistant",
                "text": reply[:4000],
                "status": "done",
                "ts": int(time.time() * 1000),
                "reply_to": mid,
            })
    del _manager_chat[:-200]

    # 3. Hand back the still-queued commands + any pending chat messages
    pending_chat = [m for m in _manager_chat if m["role"] == "user" and m.get("status") == "pending"]
    # mark them processing so we don't hand them out twice
    for m in pending_chat:
        m["status"] = "processing"
    return JSONResponse({"commands": list(_command_queue),
                         "chat_messages": [{"id": m["id"], "text": m["text"]} for m in pending_chat]})


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
