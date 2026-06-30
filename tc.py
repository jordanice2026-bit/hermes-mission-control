"""
tc.py — Transaction Coordination backend router for Mission Control.
Mounted in main.py via: app.include_router(router)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Notion database / datasource IDs
# ---------------------------------------------------------------------------
DEALS_DB_ID  = '4f4c9c20-47a3-4557-9d91-0b8bd89906c6'
DEALS_DS_ID  = 'a0199525-c4cb-4950-8d9f-d754c18f5e6e'
TCC_DB_ID    = '1daad6cc-0c17-4d20-9cb2-81ca42a46056'
TCC_DS_ID    = '36423ce7-79b5-4944-8894-e5e7330c57f4'
TCM_DB_ID    = '64e26662-d4ac-4f26-8793-325ad71ce1db'
TCM_DS_ID    = '0d3e7cce-3fb7-4e0a-b187-d204927c90b5'
DL_DB_ID     = 'fff907b8-a2bd-4b1d-83eb-420658fcd791'
DL_DS_ID     = 'ea2e1aab-4d1c-4c20-b24f-ac2b7d5f789e'
OWNERS_DB_ID = 'af076d45-42d5-42a1-9bc6-8d9471c31530'
OWNERS_DS_ID = 'd215a50d-ec81-457c-808b-cd9be5ee3b9a'

NOTION_VERSION = '2025-09-03'
NOTION_BASE    = 'https://api.notion.com/v1'

# ---------------------------------------------------------------------------
# Gmail / Google API paths
# ---------------------------------------------------------------------------
_LOCAL_GMAIL_PYTHON = '/opt/data/gws-venv/bin/python'
GMAIL_PYTHON = (
    _LOCAL_GMAIL_PYTHON
    if os.path.exists(_LOCAL_GMAIL_PYTHON)
    else sys.executable
)
GOOGLE_API_SCRIPT = os.path.join(os.path.dirname(__file__), 'gws', 'google_api.py')

# ---------------------------------------------------------------------------
# PDF parser path
# ---------------------------------------------------------------------------
PARSE_PDF_SCRIPT = '/opt/data/parse_purchase_agreement.py'

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _require_auth(request: Request) -> Dict[str, Any]:
    user = request.session.get('user')
    if not user:
        raise HTTPException(status_code=401, detail='Not authenticated')
    return user


# ---------------------------------------------------------------------------
# Notion token resolution
# ---------------------------------------------------------------------------

def _get_notion_token() -> str:
    token = os.environ.get('NOTION_TOKEN', '') or os.environ.get('NOTION_API_KEY', '')
    if token:
        return token
    try:
        p1 = open('/opt/data/.nt1').read().strip()
        p2 = open('/opt/data/.nt2').read().strip()
        return p1 + p2
    except Exception:
        pass
    return ''


def _notion_headers() -> Dict[str, str]:
    return {
        'Authorization': f'Bearer {_get_notion_token()}',
        'Notion-Version': NOTION_VERSION,
        'Content-Type': 'application/json',
    }


# ---------------------------------------------------------------------------
# Notion helpers — low-level HTTP
# ---------------------------------------------------------------------------

async def _notion_query(ds_id: str, payload: Optional[Dict] = None) -> List[Dict]:
    """Query all pages from a data_sources/{ds_id}/query endpoint, handling pagination."""
    url = f'{NOTION_BASE}/data_sources/{ds_id}/query'
    body: Dict[str, Any] = dict(payload or {})
    results: List[Dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            resp = await client.post(url, headers=_notion_headers(), json=body)
            if resp.status_code != 200:
                logger.error('Notion query %s → %s %s', ds_id, resp.status_code, resp.text)
                raise HTTPException(status_code=502, detail=f'Notion error: {resp.text}')
            data = resp.json()
            results.extend(data.get('results', []))
            if not data.get('has_more'):
                break
            body['start_cursor'] = data['next_cursor']

    return results


async def _notion_get_page(page_id: str) -> Dict:
    url = f'{NOTION_BASE}/pages/{page_id}'
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=_notion_headers())
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f'Notion error: {resp.text}')
    return resp.json()


async def _notion_create_page(db_id: str, properties: Dict) -> Dict:
    url = f'{NOTION_BASE}/pages'
    body = {'parent': {'database_id': db_id}, 'properties': properties}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, headers=_notion_headers(), json=body)
    if resp.status_code not in (200, 201):
        logger.error('Notion create page → %s %s', resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail=f'Notion error: {resp.text}')
    return resp.json()


async def _notion_patch_page(page_id: str, properties: Dict) -> Dict:
    url = f'{NOTION_BASE}/pages/{page_id}'
    body = {'properties': properties}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(url, headers=_notion_headers(), json=body)
    if resp.status_code != 200:
        logger.error('Notion patch %s → %s %s', page_id, resp.status_code, resp.text)
        raise HTTPException(status_code=502, detail=f'Notion error: {resp.text}')
    return resp.json()


# ---------------------------------------------------------------------------
# Property helpers — read
# ---------------------------------------------------------------------------

def _rt(prop):
    return ''.join(i.get('plain_text', '') for i in (prop or {}).get('rich_text', []))

def _title(prop):
    return ''.join(i.get('plain_text', '') for i in (prop or {}).get('title', []))

def _sel(prop):
    s = (prop or {}).get('select')
    return s.get('name', '') if s else ''

def _num(prop):
    return (prop or {}).get('number')

def _email_prop(prop):
    return (prop or {}).get('email') or ''

def _date(prop):
    d = (prop or {}).get('date')
    return d.get('start') if d else None

def _chk(prop):
    return bool((prop or {}).get('checkbox'))

def _rel(prop):
    return [r['id'] for r in (prop or {}).get('relation', [])]


# ---------------------------------------------------------------------------
# Property helpers — write
# ---------------------------------------------------------------------------

def _rt_prop(v: str) -> Dict:
    chunks = [v[i:i+2000] for i in range(0, len(v), 2000)]
    return {'rich_text': [{'type': 'text', 'text': {'content': c}} for c in chunks]}

def _sel_prop(v: str) -> Dict:
    return {'select': {'name': v}}

def _title_prop(v: str) -> Dict:
    return {'title': [{'type': 'text', 'text': {'content': v}}]}

def _num_prop(v: float) -> Dict:
    return {'number': v}

def _email_write(v: str) -> Dict:
    return {'email': v}

def _date_prop(v: str) -> Dict:
    return {'date': {'start': v}}

def _chk_prop(v: bool) -> Dict:
    return {'checkbox': v}

def _rel_prop(ids: List[str]) -> Dict:
    return {'relation': [{'id': i} for i in ids]}

def _today_iso() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Parsed objects
# ---------------------------------------------------------------------------

def _parse_deal(page: Dict) -> Dict:
    p = page.get('properties', {})
    return {
        'id': page.get('id', ''),
        'address': _title(p.get('Property Address')),
        'status': _sel(p.get('Status')),
        'purchase_price': _num(p.get('Purchase Price')),
        'earnest_money': _num(p.get('Earnest Money')),
        'commission_pct': _num(p.get('Commission Pct')),
        'contract_date': _date(p.get('Contract Date')),
        'inspection_deadline': _date(p.get('Inspection Deadline')),
        'financing_deadline': _date(p.get('Financing Deadline')),
        'closing_date': _date(p.get('Closing Date')),
        'buyer_name': _rt(p.get('Buyer Name')),
        'buyer_agent_name': _rt(p.get('Buyer Agent Name')),
        'buyer_agent_email': _email_prop(p.get('Buyer Agent Email')),
        'lender_name': _rt(p.get('Lender Name')),
        'lender_email': _email_prop(p.get('Lender Email')),
        'inspector_name': _rt(p.get('Inspector Name')),
        'inspector_email': _email_prop(p.get('Inspector Email')),
        'title_company': _rt(p.get('Title Company')),
        'title_rep_name': _rt(p.get('Title Rep Name')),
        'title_rep_email': _email_prop(p.get('Title Rep Email')),
        'mls_number': _rt(p.get('MLS Number')),
        'notes': _rt(p.get('Notes')),
        'pdf_parsed': _chk(p.get('PDF Parsed')),
        'tc_comms_ids': _rel(p.get('TC Comms')),
        'deadline_ids': _rel(p.get('Deadlines')),
        'tc_contact_ids': _rel(p.get('TC Contacts')),
        'seller_ids': _rel(p.get('Seller')),
    }


def _parse_comm(page: Dict) -> Dict:
    p = page.get('properties', {})
    return {
        'id': page.get('id', ''),
        'subject': _title(p.get('Subject')),
        'deal_ids': _rel(p.get('Deal')),
        'to_email': _email_prop(p.get('To Email')),
        'to_name': _rt(p.get('To Name')),
        'recipient_role': _sel(p.get('Recipient Role')),
        'body': _rt(p.get('Body')),
        'status': _sel(p.get('Status')),
        'approved': _chk(p.get('Approved')),
        'date_created': _date(p.get('Date Created')),
        'sent_date': _date(p.get('Sent Date')),
        'gmail_thread_id': _rt(p.get('Gmail Thread ID')),
        'triggered_by': _sel(p.get('Triggered By')),
        'incoming_email_id': _rt(p.get('Incoming Email ID')),
        'notes': _rt(p.get('Notes')),
    }


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

async def _run(*args: str):
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(), err.decode()


async def _gmail_send(to: str, subject: str, body: str) -> Dict:
    rc, out, err = await _run(
        GMAIL_PYTHON, GOOGLE_API_SCRIPT,
        'gmail', 'send',
        '--to', to,
        '--subject', subject,
        '--body', body,
    )
    if rc != 0:
        raise RuntimeError(err.strip() or 'gmail send failed')
    return json.loads(out.strip())


async def _gmail_search(query: str, max_results: int = 50) -> List[Dict]:
    rc, out, err = await _run(
        GMAIL_PYTHON, GOOGLE_API_SCRIPT,
        'gmail', 'search', query,
        '--max', str(max_results),
    )
    if rc != 0:
        logger.warning('gmail search failed: %s', err.strip())
        return []
    return json.loads(out.strip()) if out.strip() else []


async def _gmail_get(msg_id: str) -> Dict:
    rc, out, err = await _run(
        GMAIL_PYTHON, GOOGLE_API_SCRIPT,
        'gmail', 'get', msg_id,
    )
    if rc != 0:
        logger.warning('gmail get %s failed: %s', msg_id, err.strip())
        return {}
    return json.loads(out.strip())


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class DealCreateBody(BaseModel):
    address: str
    status: str = 'Under Contract'
    purchase_price: Optional[float] = None
    earnest_money: Optional[float] = None
    commission_pct: Optional[float] = None
    contract_date: Optional[str] = None
    inspection_deadline: Optional[str] = None
    financing_deadline: Optional[str] = None
    closing_date: Optional[str] = None
    buyer_name: str = ''
    buyer_agent_name: str = ''
    buyer_agent_email: str = ''
    lender_name: str = ''
    lender_email: str = ''
    inspector_name: str = ''
    inspector_email: str = ''
    title_company: str = ''
    title_rep_name: str = ''
    title_rep_email: str = ''
    mls_number: str = ''
    notes: str = ''
    seller_id: str = ''


class CommDraftBody(BaseModel):
    to_email: str
    to_name: str = ''
    recipient_role: str = 'Other'
    subject: str
    body: str


class DealPatchBody(BaseModel):
    status: Optional[str] = None
    purchase_price: Optional[float] = None
    closing_date: Optional[str] = None
    title_company: Optional[str] = None
    title_rep_name: Optional[str] = None
    title_rep_email: Optional[str] = None
    buyer_agent_email: Optional[str] = None
    lender_email: Optional[str] = None
    inspector_email: Optional[str] = None
    notes: Optional[str] = None
    pdf_parsed: Optional[bool] = None


# ---------------------------------------------------------------------------
# Internal business-logic helpers
# ---------------------------------------------------------------------------

async def _create_deadline(deal_id: str, name: str, date_str: Optional[str]) -> Optional[str]:
    """Create a Deadline page linked to a deal. Returns the new page id or None."""
    if not date_str:
        return None
    props: Dict[str, Any] = {
        'Name': _title_prop(name),
        'Date': _date_prop(date_str),
        'Deal': _rel_prop([deal_id]),
    }
    page = await _notion_create_page(DL_DB_ID, props)
    return page.get('id')


async def _create_tc_contact(
    deal_id: str,
    name: str,
    email: str,
    role: str,
) -> Optional[str]:
    """Create a TC Contact (TCM) page linked to a deal. Returns new page id or None."""
    if not email:
        return None
    props: Dict[str, Any] = {
        'Name': _title_prop(name or email),
        'Email': _email_write(email),
        'Role': _sel_prop(role),
        'Deal': _rel_prop([deal_id]),
    }
    page = await _notion_create_page(TCM_DB_ID, props)
    return page.get('id')


async def _get_comms_for_deal(deal_id: str) -> List[Dict]:
    """Return all TC Comm pages related to a given deal."""
    payload = {
        'filter': {
            'property': 'Deal',
            'relation': {'contains': deal_id},
        }
    }
    pages = await _notion_query(TCM_DS_ID, payload)
    return [_parse_comm(p) for p in pages]


async def _get_deadlines_for_deal(deal_id: str) -> List[Dict]:
    payload = {
        'filter': {
            'property': 'Deal',
            'relation': {'contains': deal_id},
        }
    }
    pages = await _notion_query(DL_DS_ID, payload)
    results = []
    for p in pages:
        props = p.get('properties', {})
        results.append({
            'id': p.get('id', ''),
            'name': _title(props.get('Name')),
            'date': _date(props.get('Date')),
        })
    return results


async def _get_tc_contacts_for_deal(deal_id: str) -> List[Dict]:
    payload = {
        'filter': {
            'property': 'Deal',
            'relation': {'contains': deal_id},
        }
    }
    pages = await _notion_query(TCM_DS_ID, payload)
    results = []
    for p in pages:
        props = p.get('properties', {})
        results.append({
            'id': p.get('id', ''),
            'name': _title(props.get('Name')),
            'email': _email_prop(props.get('Email')),
            'role': _sel(props.get('Role')),
        })
    return results


def _build_reply_body(
    to_name: str,
    address: str,
    role: str,
    original_subject: str,
) -> str:
    """Compose a contextual reply body using the standard template."""
    name_part = to_name.split()[0] if to_name else 'there'

    role_sentences: Dict[str, str] = {
        'Buyer Agent':  'I wanted to follow up and confirm we are on track with the transaction timeline.',
        'Lender':       'Please let us know if you need any additional documentation or information to keep the financing on schedule.',
        'Inspector':    'Please confirm the inspection appointment details and let us know if you need access arrangements.',
        'Title Rep':    'Please keep us updated on the title work and let us know if anything needs our attention.',
        'Seller':       'We appreciate your cooperation throughout this transaction.',
        'Buyer':        'We are working diligently to ensure a smooth closing for you.',
    }
    context_sentence = role_sentences.get(
        role,
        'We are reviewing your message and will follow up with any additional information needed.',
    )

    return (
        f'Hi {name_part},\n\n'
        f'Thank you for your email regarding {address}. '
        f'{context_sentence} '
        f'Please let me know if you need anything else.\n\n'
        f'Best regards,\n'
        f'Jordan Ice | Investment Sales Broker | Trueblood Real Estate | jordan@truebloodre.com'
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()


# ---------------------------------------------------------------------------
# 1. GET /api/deals — list all deals
# ---------------------------------------------------------------------------

@router.get('/api/deals')
async def list_deals(request: Request):
    _require_auth(request)
    pages = await _notion_query(DEALS_DS_ID)
    deals = [_parse_deal(p) for p in pages]
    return {'deals': deals, 'total': len(deals)}


# ---------------------------------------------------------------------------
# 2a. POST /api/deals/parse-pdf-preview — parse PDF before deal exists
# (MUST be before /{deal_id} routes so FastAPI doesn't treat 'parse-pdf-preview' as a deal_id)
# ---------------------------------------------------------------------------

@router.post('/api/deals/parse-pdf-preview')
async def parse_pdf_preview(request: Request, pdf_file: UploadFile = File(...)):
    """Parse a PDF before a deal exists — returns parsed fields for form pre-fill."""
    _require_auth(request)
    content = await pdf_file.read()
    if not content:
        return JSONResponse({'error': 'Empty file uploaded'}, status_code=400)
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        python = GMAIL_PYTHON if os.path.exists(GMAIL_PYTHON) else sys.executable
        rc, out, err = await _run(python, PARSE_PDF_SCRIPT, tmp_path)
        if rc != 0:
            return JSONResponse({'error': err.strip() or 'Parse failed', 'parse_warnings': []})
        result = json.loads(out.strip())
        return JSONResponse(result)
    except Exception as exc:
        logger.exception('parse_pdf_preview error: %s', exc)
        return JSONResponse({'error': str(exc), 'parse_warnings': []}, status_code=500)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 2. POST /api/deals — create a new deal
# ---------------------------------------------------------------------------

@router.post('/api/deals', status_code=201)
async def create_deal(request: Request, body: DealCreateBody):
    _require_auth(request)

    # Build core deal properties
    props: Dict[str, Any] = {
        'Property Address': _title_prop(body.address),
        'Status': _sel_prop(body.status),
    }

    if body.purchase_price is not None:
        props['Purchase Price'] = _num_prop(body.purchase_price)
    if body.earnest_money is not None:
        props['Earnest Money'] = _num_prop(body.earnest_money)
    if body.commission_pct is not None:
        props['Commission Pct'] = _num_prop(body.commission_pct)
    if body.contract_date:
        props['Contract Date'] = _date_prop(body.contract_date)
    if body.inspection_deadline:
        props['Inspection Deadline'] = _date_prop(body.inspection_deadline)
    if body.financing_deadline:
        props['Financing Deadline'] = _date_prop(body.financing_deadline)
    if body.closing_date:
        props['Closing Date'] = _date_prop(body.closing_date)
    if body.buyer_name:
        props['Buyer Name'] = _rt_prop(body.buyer_name)
    if body.buyer_agent_name:
        props['Buyer Agent Name'] = _rt_prop(body.buyer_agent_name)
    if body.buyer_agent_email:
        props['Buyer Agent Email'] = _email_write(body.buyer_agent_email)
    if body.lender_name:
        props['Lender Name'] = _rt_prop(body.lender_name)
    if body.lender_email:
        props['Lender Email'] = _email_write(body.lender_email)
    if body.inspector_name:
        props['Inspector Name'] = _rt_prop(body.inspector_name)
    if body.inspector_email:
        props['Inspector Email'] = _email_write(body.inspector_email)
    if body.title_company:
        props['Title Company'] = _rt_prop(body.title_company)
    if body.title_rep_name:
        props['Title Rep Name'] = _rt_prop(body.title_rep_name)
    if body.title_rep_email:
        props['Title Rep Email'] = _email_write(body.title_rep_email)
    if body.mls_number:
        props['MLS Number'] = _rt_prop(body.mls_number)
    if body.notes:
        props['Notes'] = _rt_prop(body.notes)
    if body.seller_id:
        props['Seller'] = _rel_prop([body.seller_id])

    # Create the deal page first
    deal_page = await _notion_create_page(DEALS_DB_ID, props)
    deal_id = deal_page['id']

    # Auto-create Deadline records
    deadline_tasks = [
        _create_deadline(deal_id, 'Inspection Period Expires', body.inspection_deadline),
        _create_deadline(deal_id, 'Financing Contingency Expires', body.financing_deadline),
        _create_deadline(deal_id, 'Closing Date', body.closing_date),
    ]
    deadline_ids = [
        did for did in await asyncio.gather(*deadline_tasks, return_exceptions=False)
        if did
    ]

    # Auto-create TC Contact records for each party with an email
    contact_specs = [
        (body.buyer_agent_name or 'Buyer Agent', body.buyer_agent_email, 'Buyer Agent'),
        (body.title_rep_name or 'Title Rep',     body.title_rep_email,   'Title Rep'),
        (body.inspector_name or 'Inspector',     body.inspector_email,   'Inspector'),
        (body.lender_name or 'Lender',           body.lender_email,      'Lender'),
    ]
    contact_tasks = [
        _create_tc_contact(deal_id, name, email, role)
        for name, email, role in contact_specs
        if email
    ]
    contact_ids = [
        cid for cid in await asyncio.gather(*contact_tasks, return_exceptions=False)
        if cid
    ]

    # Patch the deal to link newly created deadlines & contacts
    link_props: Dict[str, Any] = {}
    if deadline_ids:
        link_props['Deadlines'] = _rel_prop(deadline_ids)
    if contact_ids:
        link_props['TC Contacts'] = _rel_prop(contact_ids)
    if link_props:
        deal_page = await _notion_patch_page(deal_id, link_props)

    return _parse_deal(deal_page)


# ---------------------------------------------------------------------------
# 3. GET /api/deals/{deal_id} — single deal with comms & deadlines
# ---------------------------------------------------------------------------

@router.get('/api/deals/{deal_id}')
async def get_deal(request: Request, deal_id: str):
    _require_auth(request)

    deal_page, comms, deadlines, contacts = await asyncio.gather(
        _notion_get_page(deal_id),
        _get_comms_for_deal(deal_id),
        _get_deadlines_for_deal(deal_id),
        _get_tc_contacts_for_deal(deal_id),
    )

    deal = _parse_deal(deal_page)
    deal['comms'] = comms
    deal['deadlines'] = deadlines
    deal['tc_contacts'] = contacts
    return deal


# ---------------------------------------------------------------------------
# 4. PATCH /api/deals/{deal_id} — update deal fields
# ---------------------------------------------------------------------------

@router.patch('/api/deals/{deal_id}')
async def patch_deal(request: Request, deal_id: str, body: DealPatchBody):
    _require_auth(request)

    props: Dict[str, Any] = {}

    if body.status is not None:
        props['Status'] = _sel_prop(body.status)
    if body.purchase_price is not None:
        props['Purchase Price'] = _num_prop(body.purchase_price)
    if body.closing_date is not None:
        props['Closing Date'] = _date_prop(body.closing_date)
    if body.title_company is not None:
        props['Title Company'] = _rt_prop(body.title_company)
    if body.title_rep_name is not None:
        props['Title Rep Name'] = _rt_prop(body.title_rep_name)
    if body.title_rep_email is not None:
        props['Title Rep Email'] = _email_write(body.title_rep_email)
    if body.buyer_agent_email is not None:
        props['Buyer Agent Email'] = _email_write(body.buyer_agent_email)
    if body.lender_email is not None:
        props['Lender Email'] = _email_write(body.lender_email)
    if body.inspector_email is not None:
        props['Inspector Email'] = _email_write(body.inspector_email)
    if body.notes is not None:
        props['Notes'] = _rt_prop(body.notes)
    if body.pdf_parsed is not None:
        props['PDF Parsed'] = _chk_prop(body.pdf_parsed)

    if not props:
        raise HTTPException(status_code=400, detail='No fields to update')

    page = await _notion_patch_page(deal_id, props)
    return _parse_deal(page)


# ---------------------------------------------------------------------------
# 5. GET /api/deals/{deal_id}/comms — all TC comms for a deal
# ---------------------------------------------------------------------------

@router.get('/api/deals/{deal_id}/comms')
async def list_deal_comms(request: Request, deal_id: str):
    _require_auth(request)
    comms = await _get_comms_for_deal(deal_id)
    return {'comms': comms, 'total': len(comms)}


# ---------------------------------------------------------------------------
# 6. GET /api/tc/pending — all comms with Status='Pending Approval'
# ---------------------------------------------------------------------------

@router.get('/api/tc/pending')
async def list_pending_comms(request: Request):
    _require_auth(request)
    payload = {
        'filter': {
            'property': 'Status',
            'select': {'equals': 'Pending Approval'},
        }
    }
    pages = await _notion_query(TCM_DS_ID, payload)
    comms = [_parse_comm(p) for p in pages]
    return {'comms': comms, 'total': len(comms)}


# ---------------------------------------------------------------------------
# 7. POST /api/tc/{comm_id}/approve — approve a TC comm
# ---------------------------------------------------------------------------

@router.post('/api/tc/{comm_id}/approve')
async def approve_comm(request: Request, comm_id: str):
    _require_auth(request)
    props = {
        'Approved': _chk_prop(True),
        'Status': _sel_prop('Approved'),
    }
    page = await _notion_patch_page(comm_id, props)
    return _parse_comm(page)


# ---------------------------------------------------------------------------
# 8. POST /api/tc/{comm_id}/send — send via Gmail, update status
# ---------------------------------------------------------------------------

@router.post('/api/tc/{comm_id}/send')
async def send_comm(request: Request, comm_id: str):
    _require_auth(request)

    # Fetch current comm
    page = await _notion_get_page(comm_id)
    comm = _parse_comm(page)

    if not comm['to_email']:
        raise HTTPException(status_code=400, detail='Comm has no to_email')

    if comm['status'] not in ('Approved', 'Pending Approval'):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot send comm with status '{comm['status']}'. Must be Approved or Pending Approval.",
        )

    # Send via Gmail
    try:
        result = await _gmail_send(
            to=comm['to_email'],
            subject=comm['subject'],
            body=comm['body'],
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f'Gmail send failed: {exc}')

    thread_id = result.get('threadId', '') or result.get('id', '')
    today = _today_iso()

    props = {
        'Status': _sel_prop('Sent'),
        'Sent Date': _date_prop(today),
        'Approved': _chk_prop(True),
    }
    if thread_id:
        props['Gmail Thread ID'] = _rt_prop(thread_id)

    updated = await _notion_patch_page(comm_id, props)
    return _parse_comm(updated)


# ---------------------------------------------------------------------------
# 9. POST /api/tc/{comm_id}/reject — set Status back to 'Draft'
# ---------------------------------------------------------------------------

@router.post('/api/tc/{comm_id}/reject')
async def reject_comm(request: Request, comm_id: str):
    _require_auth(request)
    props = {
        'Status': _sel_prop('Draft'),
        'Approved': _chk_prop(False),
    }
    page = await _notion_patch_page(comm_id, props)
    return _parse_comm(page)


# ---------------------------------------------------------------------------
# 10. POST /api/deals/{deal_id}/draft-comm — manually draft a TC comm
# ---------------------------------------------------------------------------

@router.post('/api/deals/{deal_id}/draft-comm', status_code=201)
async def draft_comm(request: Request, deal_id: str, body: CommDraftBody):
    _require_auth(request)

    props: Dict[str, Any] = {
        'Subject': _title_prop(body.subject),
        'To Email': _email_write(body.to_email),
        'To Name': _rt_prop(body.to_name),
        'Recipient Role': _sel_prop(body.recipient_role),
        'Body': _rt_prop(body.body),
        'Status': _sel_prop('Pending Approval'),
        'Triggered By': _sel_prop('Manual'),
        'Date Created': _date_prop(_today_iso()),
        'Deal': _rel_prop([deal_id]),
    }

    page = await _notion_create_page(TCC_DB_ID, props)
    return _parse_comm(page)


# ---------------------------------------------------------------------------
# 11. POST /api/deals/{deal_id}/parse-pdf — parse a purchase agreement PDF
# ---------------------------------------------------------------------------


@router.post('/api/deals/{deal_id}/parse-pdf')
async def parse_pdf(request: Request, deal_id: str, pdf_file: UploadFile = File(...)):
    _require_auth(request)

    # Write the uploaded file to a temp location
    suffix = os.path.splitext(pdf_file.filename or '.pdf')[1] or '.pdf'
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
        content = await pdf_file.read()
        tmp.write(content)

    try:
        rc, out, err = await _run(sys.executable, PARSE_PDF_SCRIPT, tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if rc != 0:
        logger.error('parse_purchase_agreement.py failed: %s', err.strip())
        raise HTTPException(status_code=500, detail=f'PDF parsing failed: {err.strip() or "unknown error"}')

    try:
        parsed = json.loads(out.strip())
    except json.JSONDecodeError:
        logger.error('parse_purchase_agreement.py bad JSON: %s', out[:200])
        raise HTTPException(status_code=500, detail='PDF parser returned invalid JSON')

    return {'deal_id': deal_id, 'parsed': parsed}


# ---------------------------------------------------------------------------
# 12. GET /api/tc/gmail-monitor — scan Gmail for inbound emails from TC contacts
# ---------------------------------------------------------------------------

async def _check_existing_comm_by_msg_id(msg_id: str) -> bool:
    """Return True if a TC Comm with this Gmail Message ID already exists."""
    payload = {
        'filter': {
            'property': 'Incoming Email ID',
            'rich_text': {'equals': msg_id},
        }
    }
    pages = await _notion_query(TCM_DS_ID, payload)
    return len(pages) > 0


async def _collect_active_deals_and_emails() -> List[Dict]:
    """
    Query all non-closed deals and collect contact emails.
    Returns a list of dicts with deal info + all contact emails.
    """
    closed_statuses = {'Closed', 'Dead', 'Cancelled', 'Withdrawn'}

    all_pages = await _notion_query(DEALS_DS_ID)
    active = []
    for page in all_pages:
        deal = _parse_deal(page)
        if deal['status'] in closed_statuses:
            continue

        # Gather field-level emails
        field_emails: Dict[str, str] = {}
        for key in ('buyer_agent_email', 'lender_email', 'inspector_email', 'title_rep_email'):
            email = deal.get(key, '')
            if email:
                field_emails[email.lower()] = key.replace('_email', '').replace('_', ' ').title()

        # Gather TC Contacts from TCM database
        contact_pages = await _notion_query(
            TCM_DS_ID,
            {'filter': {'property': 'Deal', 'relation': {'contains': deal['id']}}},
        )
        for cp in contact_pages:
            cp_props = cp.get('properties', {})
            em = _email_prop(cp_props.get('Email'))
            role = _sel(cp_props.get('Role'))
            name = _title(cp_props.get('Name'))
            if em:
                field_emails[em.lower()] = role or name or em

        active.append({
            'deal': deal,
            'emails': field_emails,  # {email_lower: role_label}
        })

    return active


@router.get('/api/tc/gmail-monitor')
async def gmail_monitor(request: Request):
    _require_auth(request)

    active_deals = await _collect_active_deals_and_emails()

    # Build a flat map: email → (deal, role)
    email_to_deal: Dict[str, Dict] = {}        # email → deal dict
    email_to_role: Dict[str, str] = {}         # email → role label
    email_to_name: Dict[str, str] = {}         # email → contact name

    for entry in active_deals:
        deal = entry['deal']
        for email, role in entry['emails'].items():
            if email not in email_to_deal:
                email_to_deal[email] = deal
                email_to_role[email] = role
                # Best-effort name from deal fields
                role_name_map = {
                    'Buyer Agent': deal.get('buyer_agent_name', ''),
                    'Lender':      deal.get('lender_name', ''),
                    'Inspector':   deal.get('inspector_name', ''),
                    'Title Rep':   deal.get('title_rep_name', ''),
                }
                email_to_name[email] = role_name_map.get(role, '') or role

    if not email_to_deal:
        return {'checked': 0, 'new_drafts': 0, 'message': 'No active TC contact emails found'}

    # Build Gmail search query
    email_list = ' OR '.join(email_to_deal.keys())
    gmail_query = f'from:({email_list}) newer_than:1d'

    messages = await _gmail_search(gmail_query, max_results=100)
    checked = len(messages)
    new_drafts = 0

    for msg in messages:
        msg_id = msg.get('id') or msg.get('messageId', '')
        if not msg_id:
            continue

        # Avoid duplicates
        if await _check_existing_comm_by_msg_id(msg_id):
            continue

        # Fetch full message
        full_msg = await _gmail_get(msg_id)
        if not full_msg:
            continue

        sender_email = (
            full_msg.get('from', '')
            or full_msg.get('sender', '')
            or msg.get('from', '')
        ).lower()

        # Extract bare email address if "Name <email>" format
        if '<' in sender_email and '>' in sender_email:
            sender_email = sender_email.split('<')[-1].rstrip('>').strip()

        # Match to a deal
        matched_deal = email_to_deal.get(sender_email)
        if not matched_deal:
            # Try partial match (email may have display name prefix)
            for known_email in email_to_deal:
                if known_email in sender_email:
                    matched_deal = email_to_deal[known_email]
                    sender_email = known_email
                    break

        if not matched_deal:
            continue

        role = email_to_role.get(sender_email, 'Other')
        contact_name = email_to_name.get(sender_email, '')
        address = matched_deal.get('address', 'the property')
        original_subject = full_msg.get('subject', '') or msg.get('subject', '')

        # Draft a contextual reply
        reply_subject = (
            f'Re: {original_subject}' if original_subject
            else f'Re: {address}'
        )
        reply_body = _build_reply_body(
            to_name=contact_name,
            address=address,
            role=role,
            original_subject=original_subject,
        )

        # Create TC Comm record
        props: Dict[str, Any] = {
            'Subject': _title_prop(reply_subject),
            'To Email': _email_write(sender_email),
            'To Name': _rt_prop(contact_name),
            'Recipient Role': _sel_prop(role),
            'Body': _rt_prop(reply_body),
            'Status': _sel_prop('Pending Approval'),
            'Triggered By': _sel_prop('Incoming Email'),
            'Incoming Email ID': _rt_prop(msg_id),
            'Date Created': _date_prop(_today_iso()),
            'Deal': _rel_prop([matched_deal['id']]),
        }

        try:
            await _notion_create_page(TCC_DB_ID, props)
            new_drafts += 1
        except Exception as exc:
            logger.error('Failed to create TC Comm for msg %s: %s', msg_id, exc)

    return {
        'checked': checked,
        'new_drafts': new_drafts,
        'active_deals': len(active_deals),
        'monitored_emails': len(email_to_deal),
    }
