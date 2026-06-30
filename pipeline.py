"""
Mission Control — Notion deal pipeline API routes.

Provides all Notion + Gmail pipeline operations for the Mission Control dashboard.

Mount with:
    from pipeline import router
    app.include_router(router)
"""

import asyncio
import json
import logging
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOTION_DB_ID = "38e9925fe691814781b9c4fa64806810"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Gmail: on Render, install deps + write credential files from env vars at startup
import os as _os
import base64 as _base64
import subprocess as _subprocess
import sys as _sys

def _bootstrap_gmail() -> str:
    """
    Returns the python executable to use for Gmail calls.
    On Render: installs google-api-python-client into a temp venv and writes
    credential files from GMAIL_TOKEN_B64 / GMAIL_SECRET_B64 env vars.
    Locally: uses the pre-built gws-venv.
    """
    local_python = "/opt/data/gws-venv/bin/python"
    if _os.path.exists(local_python):
        return local_python

    # Render path — bootstrap credentials from env vars
    import tempfile, pathlib
    home = _os.environ.get("HOME", "/tmp")
    hermes_home = _os.environ.get("HERMES_HOME", f"{home}/.hermes")
    pathlib.Path(hermes_home).mkdir(parents=True, exist_ok=True)

    token_b64 = _os.environ.get("GMAIL_TOKEN_B64", "")
    secret_b64 = _os.environ.get("GMAIL_SECRET_B64", "")
    if token_b64:
        token_path = f"{hermes_home}/google_token.json"
        with open(token_path, "wb") as f:
            f.write(_base64.b64decode(token_b64))
    if secret_b64:
        secret_path = f"{hermes_home}/google_client_secret.json"
        with open(secret_path, "wb") as f:
            f.write(_base64.b64decode(secret_b64))

    # Install required packages into the current Python env
    try:
        _subprocess.run(
            [_sys.executable, "-m", "pip", "install", "-q",
             "google-api-python-client", "google-auth-httplib2", "google-auth-oauthlib"],
            check=True, capture_output=True
        )
    except Exception as e:
        logger.warning("Could not install gmail deps: %s", e)

    return _sys.executable


GMAIL_PYTHON = _bootstrap_gmail()
GOOGLE_API_SCRIPT = _os.path.join(
    _os.path.dirname(_os.path.abspath(__file__)), "gws", "google_api.py"
)

STAGE_DRAFT_READY = "✉️ Draft Ready"
STAGE_EMAIL_SENT = "📨 Email Sent"
STAGE_REPLIED = "💬 Replied"
STAGE_FOLLOW_UP = "📝 Follow-Up Draft"

# ---------------------------------------------------------------------------
# Notion token — loaded once, lazily
# ---------------------------------------------------------------------------

_notion_token: Optional[str] = None


def _get_notion_token() -> str:
    """Read Notion token from env var (Render) or local split files (dev)."""
    global _notion_token
    if _notion_token is None:
        import os
        env_token = os.environ.get("NOTION_TOKEN", "")
        if env_token:
            _notion_token = env_token
        else:
            try:
                part1 = open("/opt/data/.nt1").read().strip()
                part2 = open("/opt/data/.nt2").read().strip()
                _notion_token = part1 + part2
            except OSError as exc:
                logger.error("Failed to load Notion token: %s", exc)
                raise HTTPException(
                    status_code=500, detail="Notion token not configured"
                )
    return _notion_token


def _notion_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_notion_token()}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Auth guard (session-cookie-based, compatible with main.py SessionMiddleware)
# ---------------------------------------------------------------------------


def _require_auth(request: Request) -> dict:
    """Raise 401 if no authenticated user is in the session."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    return user


# ---------------------------------------------------------------------------
# Notion property helpers
# ---------------------------------------------------------------------------


def _rich_text_value(prop: Optional[dict]) -> str:
    if not prop:
        return ""
    return "".join(
        item.get("plain_text", "") for item in prop.get("rich_text", [])
    )


def _title_value(prop: Optional[dict]) -> str:
    if not prop:
        return ""
    return "".join(
        item.get("plain_text", "") for item in prop.get("title", [])
    )


def _select_value(prop: Optional[dict]) -> str:
    if not prop:
        return ""
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


def _number_value(prop: Optional[dict]) -> Optional[float]:
    if not prop:
        return None
    return prop.get("number")


def _email_value(prop: Optional[dict]) -> str:
    if not prop:
        return ""
    return prop.get("email") or ""


def _date_value(prop: Optional[dict]) -> Optional[str]:
    if not prop:
        return None
    d = prop.get("date")
    return d.get("start") if d else None


def _parse_page(page: dict) -> dict:
    """Flatten a raw Notion page object into a pipeline record dict."""
    props = page.get("properties", {})
    return {
        "id": page.get("id", ""),
        "deal_name": _title_value(props.get("Deal Name")),
        "stage": _select_value(props.get("Stage")),
        "buyer_name": _rich_text_value(props.get("Buyer Name")),
        "buyer_email": _email_value(props.get("Buyer Email")),
        "email_subject": _rich_text_value(props.get("Email Subject")),
        "email_body": _rich_text_value(props.get("Email Body")),
        "property_address": _rich_text_value(props.get("Property Address")),
        "purchase_price": _number_value(props.get("Purchase Price")),
        "arv": _number_value(props.get("ARV")),
        "equity_spread": _number_value(props.get("Equity Spread")),
        "match_score": _number_value(props.get("Match Score")),
        "gmail_draft_id": _rich_text_value(props.get("Gmail Draft ID")),
        "gmail_thread_id": _rich_text_value(props.get("Gmail Thread ID")),
        "notes": _rich_text_value(props.get("Notes")),
        "date_created": _date_value(props.get("Date Created")),
    }


def _rich_text_prop(value: str) -> dict:
    """Build a Notion rich_text property value, chunking at the 2000-char limit."""
    if not value:
        return {"rich_text": []}
    chunks = [value[i : i + 2000] for i in range(0, len(value), 2000)]
    return {
        "rich_text": [
            {"type": "text", "text": {"content": chunk}} for chunk in chunks
        ]
    }


def _select_prop(name: str) -> dict:
    return {"select": {"name": name}}


# ---------------------------------------------------------------------------
# Notion API calls
# ---------------------------------------------------------------------------


async def _notion_query_all(client: httpx.AsyncClient) -> list[dict]:
    """Fetch every page from the pipeline DB, following Notion pagination cursors."""
    records: list[dict] = []
    payload: dict[str, Any] = {"page_size": 100}

    while True:
        resp = await client.post(
            f"{NOTION_API_BASE}/databases/{NOTION_DB_ID}/query",
            headers=_notion_headers(),
            json=payload,
        )
        if resp.status_code != 200:
            logger.error(
                "Notion DB query failed %d: %s", resp.status_code, resp.text
            )
            raise HTTPException(
                status_code=502,
                detail=f"Notion API error ({resp.status_code}): {resp.text[:200]}",
            )
        data = resp.json()
        for page in data.get("results", []):
            records.append(_parse_page(page))

        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]

    return records


async def _notion_get_page(client: httpx.AsyncClient, page_id: str) -> dict:
    """Fetch and parse a single Notion page."""
    resp = await client.get(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=_notion_headers(),
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Notion get page failed ({resp.status_code}): {resp.text[:200]}",
        )
    return _parse_page(resp.json())


async def _notion_update_page(
    client: httpx.AsyncClient, page_id: str, properties: dict[str, Any]
) -> None:
    """PATCH a Notion page's properties."""
    resp = await client.patch(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=_notion_headers(),
        json={"properties": properties},
    )
    if resp.status_code != 200:
        logger.error(
            "Notion update page %s failed %d: %s",
            page_id,
            resp.status_code,
            resp.text,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Notion update failed ({resp.status_code}): {resp.text[:200]}",
        )


# ---------------------------------------------------------------------------
# Subprocess helpers — asyncio-based, non-blocking
# ---------------------------------------------------------------------------


async def _run_subprocess(*args: str) -> tuple[int, str, str]:
    """Spawn a subprocess asynchronously. Returns (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    # proc.returncode is always set after communicate() — cast away Optional
    rc: int = proc.returncode if proc.returncode is not None else 1
    return rc, stdout_bytes.decode("utf-8", errors="replace"), stderr_bytes.decode("utf-8", errors="replace")


async def _gmail_send(to: str, subject: str, body: str) -> dict:
    """
    Send an email via google_api.py.

    Note: if the server has gmail send blocked (SystemExit guard), the subprocess
    will exit non-zero and the error message is raised as RuntimeError so callers
    can surface it gracefully.
    """
    rc, stdout, stderr = await _run_subprocess(
        GMAIL_PYTHON,
        GOOGLE_API_SCRIPT,
        "gmail",
        "send",
        "--to",
        to,
        "--subject",
        subject,
        "--body",
        body,
    )
    if rc != 0:
        err = (stderr.strip() or stdout.strip() or "gmail send failed (unknown error)")
        logger.error("gmail send failed (rc=%d): %s", rc, err)
        raise RuntimeError(err)

    raw = stdout.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("gmail send non-JSON stdout: %r", raw)
        return {"id": "", "threadId": "", "raw": raw}


async def _gmail_search(query: str, max_results: int = 50) -> list[dict]:
    """Search Gmail and return a list of message dicts (each has threadId, id, etc.)."""
    rc, stdout, stderr = await _run_subprocess(
        GMAIL_PYTHON,
        GOOGLE_API_SCRIPT,
        "gmail",
        "search",
        query,
        "--max",
        str(max_results),
    )
    if rc != 0:
        logger.warning("gmail search %r failed (rc=%d): %s", query, rc, stderr)
        return []
    try:
        result = json.loads(stdout.strip())
        return result if isinstance(result, list) else []
    except json.JSONDecodeError:
        logger.warning("gmail search non-JSON stdout: %r", stdout[:200])
        return []


async def _hermes_oneshot(prompt: str) -> str:
    """Run `hermes chat -q <prompt>` and return the captured stdout."""
    rc, stdout, stderr = await _run_subprocess("hermes", "chat", "-q", prompt)
    if rc != 0:
        logger.warning("hermes oneshot failed (rc=%d): %s", rc, stderr.strip())
        return ""
    return stdout.strip()


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------


class DraftUpdateBody(BaseModel):
    email_subject: str = ""
    email_body: str = ""
    notes: str = ""


class AIFixBody(BaseModel):
    error_description: str


# ---------------------------------------------------------------------------
# Router
# NOTE: Static path segments (/send-all, /inbox-check) are declared BEFORE the
# dynamic /{page_id}/... routes so FastAPI does not shadow them.
# ---------------------------------------------------------------------------

router = APIRouter()


# ===========================================================================
# GET /api/pipeline — Fetch all records grouped by Stage
# ===========================================================================


@router.get("/api/pipeline")
async def get_pipeline(request: Request):
    """Return all Notion pipeline records grouped by Stage."""
    _require_auth(request)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            records = await _notion_query_all(client)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("get_pipeline: unexpected error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    stages: dict[str, list] = {}
    for record in records:
        stage_key = record.get("stage") or "Unknown"
        stages.setdefault(stage_key, []).append(record)

    return JSONResponse(
        {
            "stages": stages,
            "total": len(records),
            "updated_at": int(time.time() * 1000),
        }
    )


# ===========================================================================
# POST /api/pipeline/send-all — Send every Draft Ready record
# (Static path; declared before /{page_id}/send to avoid shadowing)
# ===========================================================================


@router.post("/api/pipeline/send-all")
async def send_all(request: Request):
    """Send all records currently in the '✉️ Draft Ready' stage."""
    _require_auth(request)

    sent_count = 0
    failed_count = 0
    results: list[dict] = []

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            all_records = await _notion_query_all(client)
            draft_ready = [
                r for r in all_records if r.get("stage") == STAGE_DRAFT_READY
            ]

            for record in draft_ready:
                page_id = record["id"]
                deal_name = record.get("deal_name", "")
                try:
                    buyer_email = (record.get("buyer_email") or "").strip()
                    email_subject = (record.get("email_subject") or "").strip()
                    email_body = (record.get("email_body") or "").strip()

                    if not buyer_email:
                        raise ValueError("missing buyer_email")
                    if not email_subject:
                        raise ValueError("missing email_subject")
                    if not email_body:
                        raise ValueError("missing email_body")

                    gmail_result = await _gmail_send(
                        to=buyer_email,
                        subject=email_subject,
                        body=email_body,
                    )
                    gmail_id = gmail_result.get("id", "")
                    thread_id = gmail_result.get("threadId", "")

                    notion_props: dict[str, Any] = {
                        "Stage": _select_prop(STAGE_EMAIL_SENT),
                        "Gmail Draft ID": _rich_text_prop(gmail_id),
                    }
                    if thread_id:
                        notion_props["Gmail Thread ID"] = _rich_text_prop(thread_id)

                    await _notion_update_page(client, page_id, notion_props)

                    results.append(
                        {
                            "ok": True,
                            "page_id": page_id,
                            "deal_name": deal_name,
                            "gmail_id": gmail_id,
                            "thread_id": thread_id,
                        }
                    )
                    sent_count += 1

                except Exception as exc:
                    logger.exception(
                        "send-all: error for page %s (%s): %s", page_id, deal_name, exc
                    )
                    results.append(
                        {
                            "ok": False,
                            "page_id": page_id,
                            "deal_name": deal_name,
                            "error": str(exc),
                        }
                    )
                    failed_count += 1

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("send_all: unexpected error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse(
        {"sent": sent_count, "failed": failed_count, "results": results}
    )


# ===========================================================================
# GET /api/pipeline/inbox-check — Check for investor replies
# (Static path; declared before /{page_id}/* to avoid shadowing)
# ===========================================================================


@router.get("/api/pipeline/inbox-check")
async def inbox_check(request: Request):
    """
    Search the inbox for replies to emails we've sent.

    For every '📨 Email Sent' record that has a stored Gmail Thread ID:
    - Scan recent inbox messages for a matching threadId
    - If a reply is found: update stage to '💬 Replied'
    - Generate a follow-up draft via hermes, save as '📝 Follow-Up Draft'
    """
    _require_auth(request)

    new_replies = 0
    follow_ups_drafted = 0
    reply_records: list[dict] = []

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            all_records = await _notion_query_all(client)
            sent_records = [
                r
                for r in all_records
                if r.get("stage") == STAGE_EMAIL_SENT
                and r.get("gmail_thread_id")
            ]

            if not sent_records:
                return JSONResponse(
                    {
                        "new_replies": 0,
                        "follow_ups_drafted": 0,
                        "records": [],
                        "note": "No sent records with thread IDs found.",
                    }
                )

            # Fetch recent inbox messages once; filter client-side by threadId
            inbox_messages = await _gmail_search("in:inbox", max_results=100)

            # Build threadId → messages index
            thread_index: dict[str, list[dict]] = {}
            for msg in inbox_messages:
                tid = msg.get("threadId", "")
                if tid:
                    thread_index.setdefault(tid, []).append(msg)

            for record in sent_records:
                page_id = record["id"]
                thread_id = record["gmail_thread_id"]
                deal_name = record.get("deal_name", "")

                try:
                    reply_msgs = thread_index.get(thread_id, [])
                    if not reply_msgs:
                        # No inbox message found for this thread — no reply yet
                        continue

                    # Mark as Replied
                    await _notion_update_page(
                        client,
                        page_id,
                        {
                            "Stage": _select_prop(STAGE_REPLIED),
                            "Gmail Thread ID": _rich_text_prop(thread_id),
                        },
                    )
                    new_replies += 1

                    # Build a follow-up draft via hermes
                    reply_snippet = reply_msgs[0].get("snippet", "")
                    followup_prompt = (
                        "Generate a professional follow-up email for a real estate "
                        "wholesaling deal. "
                        f"Original email was sent to investor {record.get('buyer_name', 'the buyer')} "
                        f"about the property at {record.get('property_address', 'the property')}. "
                        f"Purchase price: ${record.get('purchase_price', 'N/A')}, "
                        f"ARV: ${record.get('arv', 'N/A')}, "
                        f"equity spread: ${record.get('equity_spread', 'N/A')}. "
                        f"Their latest reply snippet: {reply_snippet!r}. "
                        "Return ONLY the email body text for the follow-up — no subject line, "
                        "no preamble, just the body."
                    )

                    followup_body = await _hermes_oneshot(followup_prompt)
                    followup_drafted = False

                    if followup_body:
                        orig_subject = record.get("email_subject") or ""
                        followup_subject = (
                            f"Re: {orig_subject}" if orig_subject else "Follow-Up"
                        )
                        await _notion_update_page(
                            client,
                            page_id,
                            {
                                "Stage": _select_prop(STAGE_FOLLOW_UP),
                                "Email Subject": _rich_text_prop(followup_subject),
                                "Email Body": _rich_text_prop(followup_body),
                            },
                        )
                        follow_ups_drafted += 1
                        followup_drafted = True

                    reply_records.append(
                        {
                            "page_id": page_id,
                            "deal_name": deal_name,
                            "thread_id": thread_id,
                            "reply_message_count": len(reply_msgs),
                            "follow_up_drafted": followup_drafted,
                        }
                    )

                except Exception as exc:
                    logger.exception(
                        "inbox-check: error for page %s (%s): %s", page_id, deal_name, exc
                    )
                    reply_records.append(
                        {
                            "page_id": page_id,
                            "deal_name": deal_name,
                            "error": str(exc),
                        }
                    )

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("inbox_check: unexpected error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    return JSONResponse(
        {
            "new_replies": new_replies,
            "follow_ups_drafted": follow_ups_drafted,
            "records": reply_records,
        }
    )


# ===========================================================================
# POST /api/pipeline/{page_id}/send — Send one email
# ===========================================================================


@router.post("/api/pipeline/{page_id}/send")
async def send_one(page_id: str, request: Request):
    """Fetch a Notion record, send its email draft, then mark it as Email Sent."""
    _require_auth(request)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            record = await _notion_get_page(client, page_id)

            buyer_email = (record.get("buyer_email") or "").strip()
            email_subject = (record.get("email_subject") or "").strip()
            email_body = (record.get("email_body") or "").strip()

            if not buyer_email:
                return JSONResponse(
                    {"ok": False, "error": "Record has no buyer_email"},
                    status_code=400,
                )
            if not email_subject:
                return JSONResponse(
                    {"ok": False, "error": "Record has no email_subject"},
                    status_code=400,
                )
            if not email_body:
                return JSONResponse(
                    {"ok": False, "error": "Record has no email_body"},
                    status_code=400,
                )

            gmail_result = await _gmail_send(
                to=buyer_email,
                subject=email_subject,
                body=email_body,
            )
            gmail_id = gmail_result.get("id", "")
            thread_id = gmail_result.get("threadId", "")

            notion_props: dict[str, Any] = {
                "Stage": _select_prop(STAGE_EMAIL_SENT),
                "Gmail Draft ID": _rich_text_prop(gmail_id),
            }
            if thread_id:
                notion_props["Gmail Thread ID"] = _rich_text_prop(thread_id)

            await _notion_update_page(client, page_id, notion_props)

        return JSONResponse({"ok": True, "gmail_id": gmail_id, "page_id": page_id})

    except HTTPException:
        raise
    except RuntimeError as exc:
        # RuntimeError surfaces gmail send failures (e.g. SEND BLOCKED)
        logger.error("send_one %s gmail error: %s", page_id, exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    except Exception as exc:
        logger.exception("send_one %s: unexpected error: %s", page_id, exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ===========================================================================
# PATCH /api/pipeline/{page_id}/draft — Save edited draft back to Notion
# ===========================================================================


@router.patch("/api/pipeline/{page_id}/draft")
async def update_draft(page_id: str, body: DraftUpdateBody, request: Request):
    """Persist edited email subject, body, and/or notes to a Notion record."""
    _require_auth(request)

    try:
        notion_props: dict[str, Any] = {}
        # Only include fields that were explicitly provided (non-empty or intentionally blank)
        if body.email_subject is not None:
            notion_props["Email Subject"] = _rich_text_prop(body.email_subject)
        if body.email_body is not None:
            notion_props["Email Body"] = _rich_text_prop(body.email_body)
        if body.notes is not None:
            notion_props["Notes"] = _rich_text_prop(body.notes)

        if not notion_props:
            return JSONResponse(
                {"ok": False, "error": "No updatable fields provided"},
                status_code=400,
            )

        async with httpx.AsyncClient(timeout=15.0) as client:
            await _notion_update_page(client, page_id, notion_props)

        return JSONResponse({"ok": True})

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("update_draft %s: %s", page_id, exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ===========================================================================
# POST /api/pipeline/{page_id}/ai-fix — AI-correct the email draft
# ===========================================================================


@router.post("/api/pipeline/{page_id}/ai-fix")
async def ai_fix(page_id: str, body: AIFixBody, request: Request):
    """
    Ask hermes to fix the current email draft based on the provided error description.
    Saves the corrected body back to Notion.
    """
    _require_auth(request)

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            record = await _notion_get_page(client, page_id)
            current_body = record.get("email_body") or ""

            prompt = (
                f"Fix this email draft. "
                f"Error: {body.error_description}. "
                f"Current draft: {current_body}. "
                f"Return ONLY the corrected email body."
            )

            corrected_body = await _hermes_oneshot(prompt)

            if not corrected_body:
                return JSONResponse(
                    {"ok": False, "error": "hermes returned an empty response"},
                    status_code=500,
                )

            await _notion_update_page(
                client,
                page_id,
                {"Email Body": _rich_text_prop(corrected_body)},
            )

        return JSONResponse({"ok": True, "corrected_body": corrected_body})

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("ai_fix %s: %s", page_id, exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
