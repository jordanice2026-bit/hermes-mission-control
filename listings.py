"""
listings.py — Listing Management dashboard.

A Listing is the home for a property from the moment the listing agreement is
won, BEFORE any purchase agreement exists. When BOTH the listing agreement and
the purchase agreement are uploaded AND marked fully executed, the listing
auto-promotes into the Deal Pipeline (creates a Deal + deadlines) and the
existing TC deadline monitor takes over the transaction.

Documents are stored as Notion-native file uploads attached to the listing card.
"""
import os
import sys
import json
import tempfile
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import tc
from tc import (
    _require_auth, _notion_query, _notion_get_page, _notion_create_page,
    _notion_patch_page, _notion_headers, _run,
    _title, _rt, _sel, _num, _date, _chk, _rel,
    _title_prop, _rt_prop, _sel_prop, _num_prop, _date_prop, _chk_prop, _rel_prop,
    NOTION_BASE, DEALS_DB_ID, OWNERS_DS_ID,
)
import pipeline as pl

router = APIRouter()

LISTINGS_DB_ID = '637295d0-9b82-459d-b315-d2ebcc761c21'
LISTINGS_DS_ID = '29107c4c-88cc-4cc3-9e00-ddd985a32c95'

PARSE_LISTING_SCRIPT = '/opt/data/mission-control/parse_listing_agreement.py'
PARSE_PA_SCRIPT = '/opt/data/parse_purchase_agreement.py'

# Key contract deadlines checklist (generated when a purchase agreement attaches)
CHECKLIST_ITEMS = [
    'Earnest money delivered',
    'Inspection completed',
    'Inspection response deadline',
    'Appraisal ordered',
    'Financing / loan commitment',
    'Title ordered',
    'Title commitment reviewed',
    'Final walkthrough',
    'Closing',
]


# ---------------------------------------------------------------------------
# Parse a listing page → dict
# ---------------------------------------------------------------------------
def _parse_listing(page: Dict) -> Dict:
    p = page.get('properties', {})

    def _files(prop):
        out = []
        for f in (prop or {}).get('files', []):
            name = f.get('name', '')
            url = ''
            if f.get('type') == 'file':
                url = f.get('file', {}).get('url', '')
            elif f.get('type') == 'external':
                url = f.get('external', {}).get('url', '')
            elif f.get('type') == 'file_upload':
                url = ''  # uploaded files render via Notion; no public URL
            out.append({'name': name, 'url': url})
        return out

    checklist_raw = _rt(p.get('Checklist'))
    try:
        checklist = json.loads(checklist_raw) if checklist_raw else []
    except Exception:
        checklist = []

    return {
        'id': page.get('id', ''),
        'address': _title(p.get('Property Address')),
        'status': _sel(p.get('Listing Status')),
        'list_price': _num(p.get('List Price')),
        'commission_pct': _num(p.get('Commission Pct')),
        'listing_type': _sel(p.get('Listing Type')),
        'property_type': _sel(p.get('Property Type')),
        'listing_start_date': _date(p.get('Listing Start Date')),
        'listing_expiration': _date(p.get('Listing Expiration')),
        'seller_names': _rt(p.get('Seller Names')),
        'mls_number': _rt(p.get('MLS Number')),
        'notes': _rt(p.get('Notes')),
        'listing_agreement': _files(p.get('Listing Agreement')),
        'listing_agreement_executed': _chk(p.get('Listing Agreement Executed')),
        'purchase_agreement': _files(p.get('Purchase Agreement')),
        'purchase_agreement_executed': _chk(p.get('Purchase Agreement Executed')),
        'promoted_to_deal': _chk(p.get('Promoted to Deal')),
        'promoted_deal_id': _rt(p.get('Promoted Deal ID')),
        'checklist': checklist,
        'owner_ids': _rel(p.get('Owner')),
        'has_listing_agreement': bool(_files(p.get('Listing Agreement'))),
        'has_purchase_agreement': bool(_files(p.get('Purchase Agreement'))),
    }


async def _enrich_owner(listing: Dict) -> Dict:
    """Attach owner name for display."""
    oid = (listing.get('owner_ids') or [None])[0]
    listing['owner_name'] = ''
    if oid:
        try:
            page = await _notion_get_page(oid)
            listing['owner_name'] = _title(page.get('properties', {}).get('Owner Name'))
        except Exception:
            pass
    return listing


# ---------------------------------------------------------------------------
# LIST + GET
# ---------------------------------------------------------------------------
@router.get('/api/listings')
async def list_listings(request: Request):
    _require_auth(request)
    pages = await _notion_query(LISTINGS_DS_ID)
    listings = [_parse_listing(p) for p in pages]
    # Enrich owner names (bounded concurrency)
    import asyncio
    await asyncio.gather(*[_enrich_owner(l) for l in listings])
    return JSONResponse({'listings': listings})


@router.get('/api/listings/{listing_id}')
async def get_listing(request: Request, listing_id: str):
    _require_auth(request)
    page = await _notion_get_page(listing_id)
    listing = _parse_listing(page)
    await _enrich_owner(listing)
    return JSONResponse(listing)


# ---------------------------------------------------------------------------
# CREATE
# ---------------------------------------------------------------------------
class ListingCreate(BaseModel):
    address: str
    owner_id: str
    status: str = 'Pre-Listing'
    list_price: Optional[float] = None
    commission_pct: Optional[float] = None
    listing_type: Optional[str] = None
    property_type: Optional[str] = None
    listing_start_date: Optional[str] = None
    listing_expiration: Optional[str] = None
    seller_names: Optional[str] = None
    mls_number: Optional[str] = None
    notes: Optional[str] = None


@router.post('/api/listings', status_code=201)
async def create_listing(request: Request, body: ListingCreate):
    _require_auth(request)
    if not body.owner_id:
        raise HTTPException(400, 'An owner must be attached to every listing')
    props: Dict[str, Any] = {
        'Property Address': _title_prop(body.address),
        'Listing Status': _sel_prop(body.status),
        'Owner': _rel_prop([body.owner_id]),
    }
    if body.list_price is not None:
        props['List Price'] = _num_prop(body.list_price)
    if body.commission_pct is not None:
        props['Commission Pct'] = _num_prop(body.commission_pct / 100.0)  # Notion percent = fraction
    if body.listing_type:
        props['Listing Type'] = _sel_prop(body.listing_type)
    if body.property_type:
        props['Property Type'] = _sel_prop(body.property_type)
    if body.listing_start_date:
        props['Listing Start Date'] = _date_prop(body.listing_start_date)
    if body.listing_expiration:
        props['Listing Expiration'] = _date_prop(body.listing_expiration)
    if body.seller_names:
        props['Seller Names'] = _rt_prop(body.seller_names)
    if body.mls_number:
        props['MLS Number'] = _rt_prop(body.mls_number)
    if body.notes:
        props['Notes'] = _rt_prop(body.notes)

    page = await _notion_create_page(LISTINGS_DB_ID, props)
    return _parse_listing(page)


# ---------------------------------------------------------------------------
# UPDATE (terms, status, notes, executed toggles)
# ---------------------------------------------------------------------------
@router.patch('/api/listings/{listing_id}')
async def update_listing(request: Request, listing_id: str):
    _require_auth(request)
    body = await request.json()
    props: Dict[str, Any] = {}
    FIELD_MAP = {
        'address': ('Property Address', _title_prop),
        'status': ('Listing Status', _sel_prop),
        'listing_type': ('Listing Type', _sel_prop),
        'property_type': ('Property Type', _sel_prop),
        'listing_start_date': ('Listing Start Date', _date_prop),
        'listing_expiration': ('Listing Expiration', _date_prop),
        'seller_names': ('Seller Names', _rt_prop),
        'mls_number': ('MLS Number', _rt_prop),
        'notes': ('Notes', _rt_prop),
    }
    for key, (prop, fn) in FIELD_MAP.items():
        if key in body and body[key] not in (None, ''):
            props[prop] = fn(body[key])
    if 'list_price' in body and body['list_price'] is not None:
        props['List Price'] = _num_prop(float(body['list_price']))
    if 'commission_pct' in body and body['commission_pct'] is not None:
        props['Commission Pct'] = _num_prop(float(body['commission_pct']) / 100.0)
    if 'owner_id' in body and body['owner_id']:
        props['Owner'] = _rel_prop([body['owner_id']])
    if 'listing_agreement_executed' in body:
        props['Listing Agreement Executed'] = _chk_prop(bool(body['listing_agreement_executed']))
    if 'purchase_agreement_executed' in body:
        props['Purchase Agreement Executed'] = _chk_prop(bool(body['purchase_agreement_executed']))

    if props:
        await _notion_patch_page(listing_id, props)

    # After any executed-toggle change, evaluate auto-promotion
    result: Dict[str, Any] = {'ok': True}
    page = await _notion_get_page(listing_id)
    listing = _parse_listing(page)
    promo = await _maybe_promote(listing)
    if promo:
        result['promoted'] = promo
        page = await _notion_get_page(listing_id)
        listing = _parse_listing(page)
    result['listing'] = listing
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# CHECKLIST update
# ---------------------------------------------------------------------------
@router.post('/api/listings/{listing_id}/checklist')
async def update_checklist(request: Request, listing_id: str):
    _require_auth(request)
    body = await request.json()
    checklist = body.get('checklist', [])
    await _notion_patch_page(listing_id, {'Checklist': _rt_prop(json.dumps(checklist))})
    return JSONResponse({'ok': True, 'checklist': checklist})


# ---------------------------------------------------------------------------
# Notion native file upload (3-step) + attach to a listing file property
# ---------------------------------------------------------------------------
async def _notion_upload_file(content: bytes, filename: str, content_type: str) -> str:
    """Create a Notion file upload, send the bytes, return the file_upload id."""
    h = _notion_headers()
    async with httpx.AsyncClient(timeout=60) as c:
        # 1. create
        r = await c.post(f'{NOTION_BASE}/file_uploads', headers=h,
                         json={'mode': 'single_part', 'filename': filename,
                               'content_type': content_type})
        if r.status_code >= 300:
            raise HTTPException(502, f'Notion file_upload create failed: {r.text}')
        fu = r.json()
        upload_id = fu['id']
        upload_url = fu['upload_url']
        # 2. send bytes (multipart) — omit JSON content-type header
        send_headers = {k: v for k, v in h.items() if k.lower() != 'content-type'}
        files = {'file': (filename, content, content_type)}
        r2 = await c.post(upload_url, headers=send_headers, files=files)
        if r2.status_code >= 300:
            raise HTTPException(502, f'Notion file_upload send failed: {r2.text}')
    return upload_id


@router.post('/api/listings/{listing_id}/upload')
async def upload_document(request: Request, listing_id: str,
                          doc_type: str = Form(...),
                          file: UploadFile = File(...)):
    """doc_type: 'listing' or 'purchase'. Uploads to Notion + parses terms."""
    _require_auth(request)
    if doc_type not in ('listing', 'purchase'):
        raise HTTPException(400, "doc_type must be 'listing' or 'purchase'")

    content = await file.read()
    filename = file.filename or f'{doc_type}.pdf'
    ctype = file.content_type or 'application/pdf'

    # 1. Upload to Notion
    upload_id = await _notion_upload_file(content, filename, ctype)

    # 2. Attach to the correct file property
    prop_name = 'Listing Agreement' if doc_type == 'listing' else 'Purchase Agreement'
    await _notion_patch_page(listing_id, {
        prop_name: {'files': [{'type': 'file_upload', 'name': filename,
                               'file_upload': {'id': upload_id}}]}
    })

    # 3. Parse the PDF for terms
    parsed = {}
    suffix = os.path.splitext(filename)[1] or '.pdf'
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = tmp.name
        tmp.write(content)
    try:
        script = PARSE_LISTING_SCRIPT if doc_type == 'listing' else PARSE_PA_SCRIPT
        rc, out, err = await _run(sys.executable, script, tmp_path)
        if rc == 0:
            try:
                parsed = json.loads(out.strip())
            except Exception:
                parsed = {}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # 4. Auto-fill parsed listing terms (only empty fields; never overwrite)
    if doc_type == 'listing' and parsed and not parsed.get('error'):
        page = await _notion_get_page(listing_id)
        existing = _parse_listing(page)
        fill: Dict[str, Any] = {}
        if parsed.get('list_price') and not existing.get('list_price'):
            fill['List Price'] = _num_prop(parsed['list_price'])
        if parsed.get('commission_pct') and not existing.get('commission_pct'):
            fill['Commission Pct'] = _num_prop(parsed['commission_pct'] / 100.0)
        if parsed.get('listing_type') and not existing.get('listing_type'):
            fill['Listing Type'] = _sel_prop(parsed['listing_type'])
        if parsed.get('property_type') and not existing.get('property_type'):
            fill['Property Type'] = _sel_prop(parsed['property_type'])
        if parsed.get('listing_start_date') and not existing.get('listing_start_date'):
            fill['Listing Start Date'] = _date_prop(parsed['listing_start_date'])
        if parsed.get('listing_expiration') and not existing.get('listing_expiration'):
            fill['Listing Expiration'] = _date_prop(parsed['listing_expiration'])
        if parsed.get('seller_names') and not existing.get('seller_names'):
            fill['Seller Names'] = _rt_prop(parsed['seller_names'])
        if parsed.get('mls_number') and not existing.get('mls_number'):
            fill['MLS Number'] = _rt_prop(str(parsed['mls_number']))
        if fill:
            await _notion_patch_page(listing_id, fill)

    # 5. When a purchase agreement lands, generate the checklist (if empty)
    if doc_type == 'purchase':
        page = await _notion_get_page(listing_id)
        existing = _parse_listing(page)
        if not existing.get('checklist'):
            checklist = _build_checklist(parsed)
            await _notion_patch_page(listing_id, {'Checklist': _rt_prop(json.dumps(checklist))})

    page = await _notion_get_page(listing_id)
    return JSONResponse({'ok': True, 'parsed': parsed, 'listing': _parse_listing(page)})


def _build_checklist(pa_parsed: Dict) -> List[Dict]:
    """Build the key-deadline checklist, seeding dates from parsed PA when present."""
    date_map = {}
    if pa_parsed and not pa_parsed.get('error'):
        date_map = {
            'Inspection completed': pa_parsed.get('inspection_deadline'),
            'Inspection response deadline': pa_parsed.get('inspection_deadline'),
            'Financing / loan commitment': pa_parsed.get('financing_deadline'),
            'Closing': pa_parsed.get('closing_date'),
        }
    return [{'item': it, 'done': False, 'date': date_map.get(it)} for it in CHECKLIST_ITEMS]


# ---------------------------------------------------------------------------
# AUTO-PROMOTION → Deal Pipeline
# ---------------------------------------------------------------------------
async def _maybe_promote(listing: Dict) -> Optional[Dict]:
    """If both agreements uploaded AND both executed AND not already promoted,
    create a Deal, seed deadlines, flip listing status."""
    if listing.get('promoted_to_deal'):
        return None
    if not (listing.get('has_listing_agreement') and listing.get('listing_agreement_executed')):
        return None
    if not (listing.get('has_purchase_agreement') and listing.get('purchase_agreement_executed')):
        return None

    # Re-parse the purchase agreement dates from the checklist (seeded on upload)
    checklist = listing.get('checklist') or []
    cl_dates = {c['item']: c.get('date') for c in checklist}

    # Build the Deal
    props: Dict[str, Any] = {
        'Property Address': _title_prop(listing.get('address') or 'Unknown'),
        'Status': _sel_prop('Under Contract'),
    }
    if listing.get('list_price'):
        props['Purchase Price'] = _num_prop(listing['list_price'])
    if listing.get('commission_pct') is not None:
        # stored as fraction in Notion percent → convert back to whole number
        props['Commission Pct'] = _num_prop(round((listing['commission_pct'] or 0) * 100, 4))
    if listing.get('mls_number'):
        props['MLS Number'] = _rt_prop(listing['mls_number'])
    if cl_dates.get('Inspection completed'):
        props['Inspection Deadline'] = _date_prop(cl_dates['Inspection completed'])
    if cl_dates.get('Financing / loan commitment'):
        props['Financing Deadline'] = _date_prop(cl_dates['Financing / loan commitment'])
    if cl_dates.get('Closing'):
        props['Closing Date'] = _date_prop(cl_dates['Closing'])
    if listing.get('notes'):
        props['Notes'] = _rt_prop(f"Promoted from Listing. {listing.get('notes','')}")

    deal_page = await _notion_create_page(DEALS_DB_ID, props)
    deal_id = deal_page['id']

    # Seed deadlines using tc's helper
    try:
        await tc._create_deadline(deal_id, 'Inspection Period Expires', cl_dates.get('Inspection completed'))
        await tc._create_deadline(deal_id, 'Financing Contingency Expires', cl_dates.get('Financing / loan commitment'))
        await tc._create_deadline(deal_id, 'Closing Date', cl_dates.get('Closing'))
    except Exception:
        pass

    # Flip the listing
    await _notion_patch_page(listing['id'], {
        'Listing Status': _sel_prop('Under Contract'),
        'Promoted to Deal': _chk_prop(True),
        'Promoted Deal ID': _rt_prop(deal_id),
    })

    return {'deal_id': deal_id, 'address': listing.get('address')}


@router.post('/api/listings/{listing_id}/promote')
async def promote_listing(request: Request, listing_id: str):
    """Manual promote trigger (also runs automatically on executed-toggle)."""
    _require_auth(request)
    page = await _notion_get_page(listing_id)
    listing = _parse_listing(page)
    promo = await _maybe_promote(listing)
    if not promo:
        reasons = []
        if listing.get('promoted_to_deal'):
            reasons.append('already promoted')
        if not listing.get('has_listing_agreement'):
            reasons.append('no listing agreement uploaded')
        elif not listing.get('listing_agreement_executed'):
            reasons.append('listing agreement not marked executed')
        if not listing.get('has_purchase_agreement'):
            reasons.append('no purchase agreement uploaded')
        elif not listing.get('purchase_agreement_executed'):
            reasons.append('purchase agreement not marked executed')
        raise HTTPException(400, 'Not ready to promote: ' + '; '.join(reasons))
    return JSONResponse({'ok': True, 'promoted': promo})


# ---------------------------------------------------------------------------
# Owners dropdown (for the create form)
# ---------------------------------------------------------------------------
@router.get('/api/listings-owners')
async def listings_owners(request: Request):
    _require_auth(request)
    pages = await _notion_query(OWNERS_DS_ID)
    owners = [{'id': p['id'], 'name': _title(p.get('properties', {}).get('Owner Name'))}
              for p in pages]
    owners = [o for o in owners if o['name']]
    owners.sort(key=lambda o: o['name'].lower())
    return JSONResponse({'owners': owners})


@router.delete('/api/listings/{listing_id}')
async def delete_listing(request: Request, listing_id: str):
    _require_auth(request)
    # Notion "delete" = archive
    await _notion_patch_page(listing_id, {})
    async with httpx.AsyncClient(timeout=30) as c:
        await c.patch(f'{NOTION_BASE}/pages/{listing_id}', headers=_notion_headers(),
                      json={'archived': True})
    return JSONResponse({'ok': True})
