"""
Mission Control — Seller Outreach Pipeline
Jordan Ice | Trueblood Real Estate

Manages the Indiana seller outreach workflow:
  Owners DB → Email Drafts DB → Gmail send → Outreach Log
"""

import asyncio
import csv
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ── Notion config ─────────────────────────────────────────────────────────────
NOTION_API_BASE  = "https://api.notion.com/v1"
NOTION_VERSION   = "2022-06-28"

OWNERS_DB_ID     = "af076d45-42d5-42a1-9bc6-8d9471c31530"
OWNERS_DS_ID     = "d215a50d-ec81-457c-808b-cd9be5ee3b9a"
EMAIL_DB_ID      = "2705d471-b231-4116-8a8c-572bf683450a"
EMAIL_DS_ID      = "944b136b-5eed-483a-9458-fa714a01ec4b"
PROPS_DB_ID      = "2c3885ba-bf8d-4e11-aaa3-30f40bf011af"
PROPS_DS_ID      = "c113e472-dbe1-42c2-91cd-ada616e520d2"
LOG_DB_ID        = "cd153943-7907-4449-bf57-e36d51cf6730"

# ── Gmail bootstrap ────────────────────────────────────────────────────────────
def _bootstrap_gmail() -> str:
    local_python = "/opt/data/gws-venv/bin/python"
    if os.path.exists(local_python):
        return local_python
    import base64, pathlib
    home = os.environ.get("HOME", "/tmp")
    hermes_home = os.environ.get("HERMES_HOME", f"{home}/.hermes")
    pathlib.Path(hermes_home).mkdir(parents=True, exist_ok=True)
    for env_var, filename in [("GMAIL_TOKEN_B64", "google_token.json"),
                               ("GMAIL_SECRET_B64", "google_client_secret.json")]:
        val = os.environ.get(env_var, "")
        if val:
            with open(f"{hermes_home}/{filename}", "wb") as f:
                f.write(base64.b64decode(val))
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                        "google-api-python-client", "google-auth-httplib2",
                        "google-auth-oauthlib"], check=True, capture_output=True)
    except Exception as e:
        logger.warning("Could not install gmail deps: %s", e)
    return sys.executable

GMAIL_PYTHON     = _bootstrap_gmail()
GOOGLE_API_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "gws", "google_api.py")
GENERATE_SCRIPT   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "..", "generate_email_drafts.py")

# ── Notion token ───────────────────────────────────────────────────────────────
_notion_token: Optional[str] = None

def _get_notion_token() -> str:
    global _notion_token
    if _notion_token is None:
        env_token = os.environ.get("NOTION_TOKEN", "") or os.environ.get("NOTION_API_KEY", "")
        if env_token:
            _notion_token = env_token
        else:
            try:
                _notion_token = open("/opt/data/.nt1").read().strip() + open("/opt/data/.nt2").read().strip()
            except OSError as exc:
                raise HTTPException(500, f"Notion token not configured: {exc}")
    return _notion_token

def _nh() -> dict:
    return {"Authorization": f"Bearer {_get_notion_token()}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json"}

# ── Auth guard ─────────────────────────────────────────────────────────────────
def _require_auth(request: Request) -> dict:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return user

# ── Notion property helpers ────────────────────────────────────────────────────
def _rt(prop):   return "".join(i.get("plain_text","") for i in (prop or {}).get("rich_text",[]))
def _title(prop): return "".join(i.get("plain_text","") for i in (prop or {}).get("title",[]))
def _sel(prop):   s=(prop or {}).get("select"); return s.get("name","") if s else ""
def _num(prop):   return (prop or {}).get("number")
def _email(prop): return (prop or {}).get("email") or ""
def _phone(prop): return (prop or {}).get("phone_number") or ""
def _chk(prop):   return bool((prop or {}).get("checkbox"))
def _date(prop):  d=(prop or {}).get("date"); return d.get("start") if d else None
def _rel(prop):   return [r["id"] for r in (prop or {}).get("relation",[])]

def _rt_prop(v: str): return {"rich_text":[{"type":"text","text":{"content":c}} for c in [v[i:i+2000] for i in range(0,len(v),2000)]]}
def _sel_prop(v: str): return {"select":{"name":v}}

# ── Parse helpers ──────────────────────────────────────────────────────────────
def _parse_owner(page: dict) -> dict:
    p = page.get("properties", {})
    return {
        "id":                page.get("id",""),
        "name":              _title(p.get("Owner Name")),
        "contact_type":      _sel(p.get("Contact Type")),
        "outreach_stage":    _sel(p.get("Outreach Stage")),
        "verification":      _sel(p.get("Verification Status")),
        "primary_phone":     _phone(p.get("Primary Phone")),
        "secondary_phone":   _phone(p.get("Secondary Phone")),
        "mailing_address":   _rt(p.get("Mailing Address")),
        "mailing_city":      _rt(p.get("Mailing City")),
        "mailing_state":     _rt(p.get("Mailing State")),
        "mailing_zip":       _rt(p.get("Mailing Zip")),
        "county":            _sel(p.get("County")),
        "notes":             _rt(p.get("Notes")),
        "do_not_contact":    _chk(p.get("Do Not Contact")),
        "property_ids":      _rel(p.get("Properties")),
        "email_draft_ids":   _rel(p.get("Email Drafts")),
    }

def _parse_draft(page: dict, owner_map: dict = None) -> dict:
    p = page.get("properties", {})
    owner_ids = _rel(p.get("Owner"))
    owner = owner_map.get(owner_ids[0]) if owner_map and owner_ids else {}
    return {
        "id":               page.get("id",""),
        "subject":          _title(p.get("Subject Line")),
        "email_body":       _rt(p.get("Email Body")),
        "recipient_email":  _email(p.get("Recipient Email")),
        "recipient_name":   _rt(p.get("Recipient Name")),
        "status":           _sel(p.get("Status")),
        "approved":         _chk(p.get("Approved")),
        "sent_date":        _date(p.get("Sent Date")),
        "gmail_thread_id":  _rt(p.get("Gmail Thread ID")),
        "date_created":     _date(p.get("Date Created")),
        "notes":            _rt(p.get("Notes")),
        "owner_id":         owner_ids[0] if owner_ids else "",
        # Enriched from owner
        "owner_name":         owner.get("name",""),
        "owner_contact_type": owner.get("contact_type",""),
        "owner_outreach_stage": owner.get("outreach_stage",""),
        "owner_mailing_state":  owner.get("mailing_state",""),
        "owner_county":         owner.get("county",""),
        "owner_phone":          owner.get("primary_phone",""),
    }

def _parse_property(page: dict) -> dict:
    p = page.get("properties", {})
    return {
        "id":           page.get("id",""),
        "address":      _title(p.get("Property Address")),
        "county":       _sel(p.get("County")),
        "prop_type":    _sel(p.get("Property Type")),
        "beds":         _num(p.get("Bedrooms")),
        "baths":        _num(p.get("Bathrooms")),
        "sqft":         _num(p.get("Sq Ft")),
        "year_built":   _num(p.get("Year Built")),
        "assessed_val": _num(p.get("Assessed Value")),
        "est_value":    _num(p.get("Est. Value")),
        "est_equity":   _num(p.get("Est. Equity")),
        "mls_status":   _sel(p.get("MLS Status")),
        "owner_ids":    _rel(p.get("Owner")),
    }

# ── Notion query helpers ───────────────────────────────────────────────────────
async def _query_all(client: httpx.AsyncClient, ds_id: str,
                     filter_payload: dict = None) -> list[dict]:
    records, cursor = [], None
    while True:
        payload: dict = {"page_size": 100}
        if cursor: payload["start_cursor"] = cursor
        if filter_payload: payload["filter"] = filter_payload
        r = await client.post(f"{NOTION_API_BASE}/data_sources/{ds_id}/query",
                              headers=_nh(), json=payload)
        if r.status_code != 200:
            raise HTTPException(502, f"Notion query failed {r.status_code}: {r.text[:200]}")
        data = r.json()
        records.extend(data.get("results", []))
        if not data.get("has_more"): break
        cursor = data["next_cursor"]
    return records

async def _get_page(client: httpx.AsyncClient, page_id: str) -> dict:
    r = await client.get(f"{NOTION_API_BASE}/pages/{page_id}", headers=_nh())
    if r.status_code != 200:
        raise HTTPException(502, f"Notion get page failed {r.status_code}: {r.text[:200]}")
    return r.json()

async def _update_page(client: httpx.AsyncClient, page_id: str, props: dict) -> None:
    r = await client.patch(f"{NOTION_API_BASE}/pages/{page_id}",
                           headers=_nh(), json={"properties": props})
    if r.status_code != 200:
        raise HTTPException(502, f"Notion update failed {r.status_code}: {r.text[:200]}")

async def _create_page(client: httpx.AsyncClient, db_id: str, props: dict) -> dict:
    r = await client.post(f"{NOTION_API_BASE}/pages",
                          headers=_nh(), json={"parent":{"database_id":db_id},"properties":props})
    if r.status_code != 200:
        raise HTTPException(502, f"Notion create page failed {r.status_code}: {r.text[:200]}")
    return r.json()

# ── Subprocess helpers ─────────────────────────────────────────────────────────
async def _run(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    rc: int = proc.returncode if proc.returncode is not None else 1
    return rc, out.decode("utf-8","replace"), err.decode("utf-8","replace")

async def _gmail_send(to: str, subject: str, body: str) -> dict:
    rc, out, err = await _run(GMAIL_PYTHON, GOOGLE_API_SCRIPT,
                               "gmail","send","--to",to,"--subject",subject,"--body",body)
    if rc != 0:
        raise RuntimeError(err.strip() or out.strip() or "gmail send failed")
    try: return json.loads(out.strip())
    except: return {"id":"","threadId":"","raw":out.strip()}

# ── Request bodies ─────────────────────────────────────────────────────────────
class DraftUpdateBody(BaseModel):
    subject: str = ""
    email_body: str = ""
    notes: str = ""

class AIFixBody(BaseModel):
    error_description: str = "Improve this email for a seller outreach context"

# ── Router ─────────────────────────────────────────────────────────────────────
router = APIRouter()

# ==============================================================================
# GET /api/seller-pipeline  — all drafts grouped by Status, enriched with owner
# ==============================================================================
@router.get("/api/seller-pipeline")
async def get_seller_pipeline(request: Request):
    _require_auth(request)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Fetch all drafts + all owners in parallel
            drafts_raw, owners_raw = await asyncio.gather(
                _query_all(client, EMAIL_DS_ID),
                _query_all(client, OWNERS_DS_ID)
            )

        owner_map = {p["id"]: _parse_owner(p) for p in owners_raw}
        stages: dict[str, list] = {"Draft":[], "Approved":[], "Sent":[], "Failed":[]}
        for page in drafts_raw:
            d = _parse_draft(page, owner_map)
            s = d["status"] or "Draft"
            stages.setdefault(s, []).append(d)

        total = sum(len(v) for v in stages.values())
        return JSONResponse({"stages": stages, "total": total,
                             "updated_at": int(time.time() * 1000)})
    except HTTPException: raise
    except Exception as e:
        logger.exception("get_seller_pipeline: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ==============================================================================
# GET /api/owners  — all owner records for Owners tab
# ==============================================================================
@router.get("/api/owners")
async def get_owners(request: Request):
    _require_auth(request)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            owners_raw = await _query_all(client, OWNERS_DS_ID)
        owners = [_parse_owner(p) for p in owners_raw]
        # Sort: New Lead first, then alphabetical
        owners.sort(key=lambda o: (o["outreach_stage"] != "New Lead", o["name"].lower()))
        return JSONResponse({"owners": owners, "total": len(owners),
                             "updated_at": int(time.time() * 1000)})
    except HTTPException: raise
    except Exception as e:
        logger.exception("get_owners: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ==============================================================================
# POST /api/seller-pipeline/generate  — run email draft generation script
# ==============================================================================
@router.post("/api/seller-pipeline/generate")
async def generate_drafts(request: Request):
    _require_auth(request)
    script = os.path.abspath(GENERATE_SCRIPT)
    if not os.path.exists(script):
        return JSONResponse({"ok": False, "error": f"Generator script not found: {script}"}, status_code=500)

    python = GMAIL_PYTHON if os.path.exists(GMAIL_PYTHON) else sys.executable
    env = {**os.environ, "PYTHONPATH": "/opt/data/pylibs"}
    proc = await asyncio.create_subprocess_exec(
        python, script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        return JSONResponse({"ok": False, "error": "Draft generation timed out (120s)"}, status_code=500)

    stdout = out.decode("utf-8","replace").strip()
    stderr = err.decode("utf-8","replace").strip()
    rc = proc.returncode if proc.returncode is not None else 1

    if rc != 0:
        logger.error("generate_drafts failed (rc=%d): %s", rc, stderr)
        return JSONResponse({"ok": False, "error": stderr or stdout or "Script failed"}, status_code=500)

    # Try to parse JSON summary from script output
    summary = {}
    for line in reversed(stdout.split("\n")):
        try:
            summary = json.loads(line)
            break
        except: pass

    return JSONResponse({"ok": True, "output": stdout,
                         "drafts_created": summary.get("created", 0),
                         "skipped": summary.get("skipped", 0)})


# ==============================================================================
# POST /api/seller-pipeline/{id}/approve  — flip Approved + Status=Approved
# ==============================================================================
@router.post("/api/seller-pipeline/{page_id}/approve")
async def approve_draft(page_id: str, request: Request):
    _require_auth(request)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await _update_page(client, page_id, {
                "Approved": {"checkbox": True},
                "Status":   _sel_prop("Approved"),
            })
        return JSONResponse({"ok": True})
    except HTTPException: raise
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ==============================================================================
# POST /api/seller-pipeline/{id}/unapprove  — revert to Draft
# ==============================================================================
@router.post("/api/seller-pipeline/{page_id}/unapprove")
async def unapprove_draft(page_id: str, request: Request):
    _require_auth(request)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await _update_page(client, page_id, {
                "Approved": {"checkbox": False},
                "Status":   _sel_prop("Draft"),
            })
        return JSONResponse({"ok": True})
    except HTTPException: raise
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ==============================================================================
# POST /api/seller-pipeline/send-all  — send every Approved draft
# ==============================================================================
@router.post("/api/seller-pipeline/send-all")
async def send_all_approved(request: Request):
    _require_auth(request)
    sent, failed, results = 0, 0, []
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            owners_raw = await _query_all(client, OWNERS_DS_ID)
            owner_map = {p["id"]: _parse_owner(p) for p in owners_raw}

            drafts_raw = await _query_all(client, EMAIL_DS_ID)
            approved = [_parse_draft(p, owner_map) for p in drafts_raw
                        if _sel(p.get("properties",{}).get("Status")) == "Approved"]

            for d in approved:
                try:
                    if not d["recipient_email"]:
                        raise ValueError("No recipient email")
                    gmail_result = await _gmail_send(d["recipient_email"], d["subject"], d["email_body"])
                    thread_id = gmail_result.get("threadId","")
                    gmail_id  = gmail_result.get("id","")

                    await _update_page(client, d["id"], {
                        "Status":          _sel_prop("Sent"),
                        "Sent Date":       {"date": {"start": time.strftime("%Y-%m-%d")}},
                        "Gmail Thread ID": _rt_prop(thread_id or gmail_id),
                    })
                    # Update owner stage
                    if d["owner_id"]:
                        await _update_page(client, d["owner_id"], {
                            "Outreach Stage": _sel_prop("Email Sent"),
                            "Last Contacted": {"date": {"start": time.strftime("%Y-%m-%d")}},
                        })
                    # Log to Outreach Log
                    await _create_page(client, LOG_DB_ID, {
                        "Log Entry": {"title": [{"text":{"content":f"Email sent to {d['recipient_name'] or d['owner_name']}"}}]},
                        "Owner":     {"relation": [{"id": d["owner_id"]}]} if d["owner_id"] else {"relation":[]},
                        "Date":      {"date": {"start": time.strftime("%Y-%m-%d")}},
                        "Channel":   _sel_prop("Email"),
                        "Outcome":   _sel_prop("Sent"),
                    })
                    results.append({"ok":True,"id":d["id"],"owner":d["owner_name"],"gmail_id":gmail_id})
                    sent += 1
                except Exception as exc:
                    logger.error("send-all failed for %s: %s", d["id"], exc)
                    await _update_page(client, d["id"], {"Status": _sel_prop("Failed"),
                                                          "Notes": _rt_prop(str(exc))})
                    results.append({"ok":False,"id":d["id"],"owner":d["owner_name"],"error":str(exc)})
                    failed += 1

    except HTTPException: raise
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)}, status_code=500)
    return JSONResponse({"sent":sent,"failed":failed,"results":results})


# ==============================================================================
# POST /api/seller-pipeline/{id}/send  — send one approved draft
# ==============================================================================
@router.post("/api/seller-pipeline/{page_id}/send")
async def send_one(page_id: str, request: Request):
    _require_auth(request)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            page = await _get_page(client, page_id)
            d = _parse_draft(page)
            if not d["recipient_email"]:
                return JSONResponse({"ok":False,"error":"No recipient email"}, status_code=400)
            if not d["subject"]:
                return JSONResponse({"ok":False,"error":"No subject line"}, status_code=400)

            gmail_result = await _gmail_send(d["recipient_email"], d["subject"], d["email_body"])
            thread_id = gmail_result.get("threadId","")
            gmail_id  = gmail_result.get("id","")

            await _update_page(client, page_id, {
                "Status":          _sel_prop("Sent"),
                "Sent Date":       {"date": {"start": time.strftime("%Y-%m-%d")}},
                "Gmail Thread ID": _rt_prop(thread_id or gmail_id),
            })
            if d["owner_id"]:
                await _update_page(client, d["owner_id"], {
                    "Outreach Stage": _sel_prop("Email Sent"),
                    "Last Contacted": {"date": {"start": time.strftime("%Y-%m-%d")}},
                })
            await _create_page(client, LOG_DB_ID, {
                "Log Entry": {"title": [{"text":{"content":f"Email sent to {d['recipient_name'] or d['owner_name']}"}}]},
                "Owner":     {"relation": [{"id": d["owner_id"]}]} if d["owner_id"] else {"relation":[]},
                "Date":      {"date": {"start": time.strftime("%Y-%m-%d")}},
                "Channel":   _sel_prop("Email"),
                "Outcome":   _sel_prop("Sent"),
            })
        return JSONResponse({"ok":True,"gmail_id":gmail_id,"thread_id":thread_id})
    except HTTPException: raise
    except RuntimeError as e:
        return JSONResponse({"ok":False,"error":str(e)}, status_code=502)
    except Exception as e:
        logger.exception("send_one %s: %s", page_id, e)
        return JSONResponse({"ok":False,"error":str(e)}, status_code=500)


# ==============================================================================
# PATCH /api/seller-pipeline/{id}/draft  — save edits back to Notion
# ==============================================================================
@router.patch("/api/seller-pipeline/{page_id}/draft")
async def update_draft(page_id: str, body: DraftUpdateBody, request: Request):
    _require_auth(request)
    try:
        props: dict[str, Any] = {}
        if body.subject is not None:
            props["Subject Line"] = {"title":[{"text":{"content":body.subject[:2000]}}]}
        if body.email_body is not None:
            props["Email Body"] = _rt_prop(body.email_body)
        if body.notes is not None:
            props["Notes"] = _rt_prop(body.notes)
        if not props:
            return JSONResponse({"ok":False,"error":"No fields provided"}, status_code=400)
        async with httpx.AsyncClient(timeout=15.0) as client:
            await _update_page(client, page_id, props)
        return JSONResponse({"ok": True})
    except HTTPException: raise
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)}, status_code=500)


# ==============================================================================
# POST /api/seller-pipeline/{id}/ai-fix  — AI improve the draft
# ==============================================================================
@router.post("/api/seller-pipeline/{page_id}/ai-fix")
async def ai_fix(page_id: str, body: AIFixBody, request: Request):
    _require_auth(request)
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            page    = await _get_page(client, page_id)
            d       = _parse_draft(page)
            current = d.get("email_body","")
            prompt  = (
                f"You are helping Jordan Ice, an investment sales broker at Trueblood Real Estate "
                f"in Indiana who specializes in listing multi-family properties for out-of-state owners.\n\n"
                f"Fix or improve this outreach email draft based on the following instruction: {body.error_description}\n\n"
                f"Current email body:\n{current}\n\n"
                f"Return ONLY the improved email body text — no subject line, no preamble."
            )
            rc, out, err = await _run("hermes","chat","-q",prompt)
            improved = out.strip() if rc == 0 else ""
            if not improved:
                return JSONResponse({"ok":False,"error":"AI returned empty response"}, status_code=500)
            await _update_page(client, page_id, {"Email Body": _rt_prop(improved)})
        return JSONResponse({"ok":True,"email_body":improved})
    except HTTPException: raise
    except Exception as e:
        logger.exception("ai_fix %s: %s", page_id, e)
        return JSONResponse({"ok":False,"error":str(e)}, status_code=500)


# ==============================================================================
# GET /api/exports/lob.csv  — Lob direct mail CSV
# ==============================================================================
@router.get("/api/exports/lob.csv")
async def export_lob(request: Request):
    _require_auth(request)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            owners_raw = await _query_all(client, OWNERS_DS_ID)

        buf = io.StringIO()
        w   = csv.writer(buf)
        # Lob merge-field column names
        w.writerow(["name","address_line1","city","state","zip","description"])
        count = 0
        for page in owners_raw:
            o = _parse_owner(page)
            if o["do_not_contact"]:           continue
            if not o["mailing_address"]:      continue
            if not o["mailing_city"]:         continue
            if not o["mailing_state"]:        continue
            if not o["mailing_zip"]:          continue

            # Pull first property address from notes or just county
            desc = f"Investment property owner — {o['county']} County, IN"
            w.writerow([o["name"], o["mailing_address"], o["mailing_city"],
                        o["mailing_state"], o["mailing_zip"], desc])
            count += 1

        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=lob_mailing_{time.strftime('%Y%m%d')}.csv",
                     "X-Record-Count": str(count)}
        )
    except HTTPException: raise
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)}, status_code=500)


# ==============================================================================
# GET /api/exports/mojo.csv  — Mojo Dialer import CSV
# ==============================================================================
@router.get("/api/exports/mojo.csv")
async def export_mojo(request: Request):
    _require_auth(request)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            owners_raw = await _query_all(client, OWNERS_DS_ID)

        buf = io.StringIO()
        w   = csv.writer(buf)
        # Mojo Dialer standard import columns
        w.writerow(["First Name","Last Name","Phone 1","Phone 2","Phone 3",
                    "Address","City","State","Zip","Notes"])
        count = 0
        for page in owners_raw:
            o = _parse_owner(page)
            if o["do_not_contact"]: continue

            phones = [o["primary_phone"], o["secondary_phone"]]
            # Parse extra phones from notes
            extra_match = re.findall(r'\(\d{3}\) \d{3}-\d{4}', o["notes"])
            phones.extend(extra_match)
            phones = [p for p in phones if p]
            if not phones: continue

            # Split name
            name_parts = o["name"].split(" ", 1)
            first = name_parts[0] if name_parts else o["name"]
            last  = name_parts[1] if len(name_parts) > 1 else ""

            # For LLCs, put LLC name in Last Name
            if o["contact_type"] in ("LLC","Trust","Corporation"):
                first, last = "", o["name"]

            notes_str = f"{o['contact_type']} | {o['county']} County IN | Stage: {o['outreach_stage']}"
            w.writerow([
                first, last,
                phones[0] if len(phones) > 0 else "",
                phones[1] if len(phones) > 1 else "",
                phones[2] if len(phones) > 2 else "",
                o["mailing_address"], o["mailing_city"], o["mailing_state"], o["mailing_zip"],
                notes_str
            ])
            count += 1

        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=mojo_contacts_{time.strftime('%Y%m%d')}.csv",
                     "X-Record-Count": str(count)}
        )
    except HTTPException: raise
    except Exception as e:
        return JSONResponse({"ok":False,"error":str(e)}, status_code=500)


# ==============================================================================
# Keep legacy /api/pipeline routes pointing at seller pipeline so any old
# bookmarks don't 404
# ==============================================================================
@router.get("/api/pipeline")
async def legacy_pipeline(request: Request):
    return await get_seller_pipeline(request)
