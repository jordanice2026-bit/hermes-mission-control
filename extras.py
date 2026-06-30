"""
extras.py — Mission Control advanced features.

Bundles: Alert Board, SLA quick-stats, Pipeline analytics, Calendar,
Global search, Settings, Email-template library, and CSV exports.

Reuses tc.py helpers/IDs and pipeline.py parsers so there is one source of
truth for Notion access.
"""
import io
import csv
import json
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

# Reuse everything from tc.py (Notion access, parsers, IDs, auth)
import tc
from tc import (
    _require_auth, _notion_query, _notion_get_page,
    _parse_deal, _parse_comm,
    DEALS_DS_ID, TCM_DS_ID, DL_DS_ID, OWNERS_DS_ID,
)

# pipeline.py owner parser + data source ids
import pipeline as pl

router = APIRouter()

# ---------------------------------------------------------------------------
# Persistent config (settings + templates) — simple JSON files
# ---------------------------------------------------------------------------
_BASE = os.path.dirname(__file__)
SETTINGS_PATH = os.path.join(_BASE, 'mc_settings.json')
TEMPLATES_PATH = os.path.join(_BASE, 'mc_templates.json')

DEFAULT_SETTINGS = {
    'deadline_lead_days': 1,          # alert N days before a deadline
    'stalled_comm_hours': 48,         # pending approval older than this = stalled
    'deadline_warning_days': 3,       # "closing soon" window for alerts
    'notify_email': 'jordan@truebloodre.com',
    'notify_on_new_draft': True,
    'notify_on_deadline': True,
    'notify_on_incoming': True,
    'default_signature': ('Jordan Ice\nInvestment Sales Broker | Trueblood Real Estate\n'
                          'jordan@truebloodre.com'),
}

DEFAULT_TEMPLATES = [
    {'id': 'tmpl_intro', 'name': 'Out-of-State Owner Intro',
     'subject': 'Managing your Indiana investment property from out of state?',
     'body': ('Hi {{owner_name}},\n\nI noticed you own an investment property in Indiana while '
              'managing it from {{owner_state}}. I work with out-of-state owners to list and sell '
              'multi-family investments here in Indiana.\n\nIf you have ever considered selling, '
              'I would be glad to share what comparable properties are bringing right now.\n\n'
              'Best,\nJordan Ice\nInvestment Sales Broker | Trueblood Real Estate')},
    {'id': 'tmpl_followup', 'name': 'Follow-Up (No Response)',
     'subject': 'Following up — your Indiana investment property',
     'body': ('Hi {{owner_name}},\n\nJust circling back on my note below. No pressure at all — '
              'if selling is not on your radar right now, I completely understand.\n\n'
              'Happy to be a resource whenever the timing is right.\n\nBest,\nJordan Ice')},
    {'id': 'tmpl_inspection', 'name': 'Inspection Deadline Reminder',
     'subject': 'Reminder: inspection deadline approaching — {{property_address}}',
     'body': ('Hi {{name}},\n\nThis is a friendly reminder that the inspection deadline for '
              '{{property_address}} is {{deadline}}. Please let me know if you need anything '
              'to stay on track.\n\nBest,\nJordan Ice')},
]


def _load_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return json.loads(json.dumps(default))  # deep copy


def _save_json(path: str, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def _get_settings() -> Dict[str, Any]:
    s = _load_json(SETTINGS_PATH, DEFAULT_SETTINGS)
    # backfill any new defaults
    for k, v in DEFAULT_SETTINGS.items():
        s.setdefault(k, v)
    return s


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).date()
    except Exception:
        try:
            return datetime.strptime(s[:10], '%Y-%m-%d').date()
        except Exception:
            return None


def _days_until(s: Optional[str]) -> Optional[int]:
    d = _parse_date(s)
    return (d - date.today()).days if d else None


def _hours_since(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return (now - dt).total_seconds() / 3600.0
    except Exception:
        return None


# ===========================================================================
# 1. ALERT BOARD
# ===========================================================================
@router.get('/api/alerts')
async def get_alerts(request: Request):
    _require_auth(request)
    settings = _get_settings()
    warn_days = settings.get('deadline_warning_days', 3)
    stalled_hours = settings.get('stalled_comm_hours', 48)

    alerts: List[Dict[str, Any]] = []

    # --- Deal deadline warnings ---
    deal_pages = await _notion_query(DEALS_DS_ID)
    deals = [_parse_deal(p) for p in deal_pages]
    DEADLINE_FIELDS = [
        ('inspection_deadline', 'Inspection'),
        ('financing_deadline', 'Financing'),
        ('closing_date', 'Closing'),
    ]
    for d in deals:
        if d.get('status') in ('Closed', 'Cancelled', 'Dead'):
            continue
        for field, label in DEADLINE_FIELDS:
            du = _days_until(d.get(field))
            if du is None:
                continue
            if du < 0:
                alerts.append({
                    'severity': 'critical', 'type': 'deadline',
                    'title': f'{label} deadline PASSED',
                    'detail': f'{d.get("address") or "Unknown deal"} — was due {d.get(field)} ({abs(du)}d ago)',
                    'deal_id': d.get('id'), 'days': du, 'date': d.get(field),
                })
            elif du <= warn_days:
                alerts.append({
                    'severity': 'warning' if du > 0 else 'critical', 'type': 'deadline',
                    'title': f'{label} deadline in {du}d' if du > 0 else f'{label} deadline TODAY',
                    'detail': f'{d.get("address") or "Unknown deal"} — due {d.get(field)}',
                    'deal_id': d.get('id'), 'days': du, 'date': d.get(field),
                })

    # --- Stalled comms (pending approval too long) ---
    comm_pages = await _notion_query(TCM_DS_ID, {
        'filter': {'property': 'Status', 'select': {'equals': 'Pending Approval'}}
    })
    for page in comm_pages:
        c = _parse_comm(page)
        hrs = _hours_since(c.get('date_created'))
        if hrs is not None and hrs >= stalled_hours:
            alerts.append({
                'severity': 'warning', 'type': 'stalled_comm',
                'title': f'Email awaiting approval {int(hrs)}h',
                'detail': f'{c.get("subject") or "(no subject)"} → {c.get("to_name") or c.get("to_email") or "?"}',
                'comm_id': c.get('id'), 'hours': int(hrs),
            })

    # --- Failed / blocked tasks (from in-memory board in main.py) ---
    try:
        import main
        for t in main._board.values():
            if t.get('status') == 'blocked':
                alerts.append({
                    'severity': 'warning', 'type': 'task',
                    'title': 'Blocked task',
                    'detail': t.get('title') or t.get('id'),
                    'task_id': t.get('id'),
                })
            err = t.get('error') or t.get('last_error')
            if err and t.get('status') not in ('done', 'cancelled'):
                alerts.append({
                    'severity': 'critical', 'type': 'task',
                    'title': 'Task error',
                    'detail': f'{t.get("title") or t.get("id")}: {str(err)[:120]}',
                    'task_id': t.get('id'),
                })
    except Exception:
        pass

    sev_order = {'critical': 0, 'warning': 1, 'info': 2}
    alerts.sort(key=lambda a: (sev_order.get(a['severity'], 3), a.get('days', 999)))
    counts = {
        'critical': sum(1 for a in alerts if a['severity'] == 'critical'),
        'warning': sum(1 for a in alerts if a['severity'] == 'warning'),
        'total': len(alerts),
    }
    return JSONResponse({'alerts': alerts, 'counts': counts})


# ===========================================================================
# 2. SLA QUICK STATS
# ===========================================================================
@router.get('/api/sla-stats')
async def get_sla_stats(request: Request):
    _require_auth(request)

    deal_pages = await _notion_query(DEALS_DS_ID)
    deals = [_parse_deal(p) for p in deal_pages]
    active = [d for d in deals if d.get('status') not in ('Closed', 'Cancelled', 'Dead')]
    closed = [d for d in deals if d.get('status') == 'Closed']

    # deadlines in next 7 days
    deadlines_week = 0
    for d in active:
        for f in ('inspection_deadline', 'financing_deadline', 'closing_date'):
            du = _days_until(d.get(f))
            if du is not None and 0 <= du <= 7:
                deadlines_week += 1

    # pending approvals + $ outgoing
    pending_pages = await _notion_query(TCM_DS_ID, {
        'filter': {'property': 'Status', 'select': {'equals': 'Pending Approval'}}
    })
    pending_approvals = len(pending_pages)

    # outgoing $ = sum of active deal purchase prices (pipeline value)
    pipeline_value = sum((d.get('purchase_price') or 0) for d in active)
    commission_pipeline = sum(
        (d.get('purchase_price') or 0) * (d.get('commission_pct') or 0) / 100.0
        for d in active
    )

    total_deals = len(deals)
    close_rate = round(100.0 * len(closed) / total_deals, 1) if total_deals else 0.0

    return JSONResponse({
        'active_deals': len(active),
        'closed_deals': len(closed),
        'deadlines_week': deadlines_week,
        'pending_approvals': pending_approvals,
        'pipeline_value': pipeline_value,
        'commission_pipeline': round(commission_pipeline, 2),
        'close_rate': close_rate,
        'total_deals': total_deals,
    })


# ===========================================================================
# 3. PIPELINE ANALYTICS (funnel + breakdowns)
# ===========================================================================
@router.get('/api/analytics')
async def get_analytics(request: Request):
    _require_auth(request)

    # Outreach funnel from Owners
    owner_pages = await _notion_query(OWNERS_DS_ID)
    owners = [pl._parse_owner(p) for p in owner_pages]
    stage_counts: Dict[str, int] = {}
    for o in owners:
        st = o.get('outreach_stage') or 'Not Started'
        stage_counts[st] = stage_counts.get(st, 0) + 1

    # Deal stage funnel
    deal_pages = await _notion_query(DEALS_DS_ID)
    deals = [_parse_deal(p) for p in deal_pages]
    deal_stage_counts: Dict[str, int] = {}
    for d in deals:
        st = d.get('status') or 'Unknown'
        deal_stage_counts[st] = deal_stage_counts.get(st, 0) + 1

    # Comms breakdown by status
    comm_pages = await _notion_query(TCM_DS_ID)
    comm_status: Dict[str, int] = {}
    for p in comm_pages:
        c = _parse_comm(p)
        st = c.get('status') or 'Unknown'
        comm_status[st] = comm_status.get(st, 0) + 1

    # Funnel as ordered stages
    funnel = [
        {'stage': 'Total Owners', 'count': len(owners)},
        {'stage': 'Contacted', 'count': sum(v for k, v in stage_counts.items()
                                            if k not in ('Not Started', '', None))},
        {'stage': 'Active Deals', 'count': sum(1 for d in deals
                                               if d.get('status') not in ('Closed', 'Cancelled', 'Dead'))},
        {'stage': 'Closed', 'count': sum(1 for d in deals if d.get('status') == 'Closed')},
    ]

    return JSONResponse({
        'funnel': funnel,
        'outreach_stages': stage_counts,
        'deal_stages': deal_stage_counts,
        'comm_status': comm_status,
        'totals': {'owners': len(owners), 'deals': len(deals), 'comms': len(comm_pages)},
    })


# ===========================================================================
# 4. CALENDAR
# ===========================================================================
@router.get('/api/calendar')
async def get_calendar(request: Request,
                       start: Optional[str] = Query(None),
                       end: Optional[str] = Query(None)):
    _require_auth(request)
    s = _parse_date(start) or date.today().replace(day=1)
    e = _parse_date(end) or (s + timedelta(days=62))

    events: List[Dict[str, Any]] = []

    deal_pages = await _notion_query(DEALS_DS_ID)
    for p in deal_pages:
        d = _parse_deal(p)
        addr = d.get('address') or 'Unknown deal'
        for field, label, color in [
            ('inspection_deadline', 'Inspection', 'warning'),
            ('financing_deadline', 'Financing', 'warning'),
            ('closing_date', 'Closing', 'accent'),
            ('contract_date', 'Contract', 'info'),
        ]:
            dt = _parse_date(d.get(field))
            if dt and s <= dt <= e:
                events.append({
                    'date': dt.isoformat(), 'label': f'{label}: {addr}',
                    'type': label.lower(), 'color': color, 'deal_id': d.get('id'),
                })

    # Deadlines DB (explicit follow-ups / milestones)
    try:
        dl_pages = await _notion_query(DL_DS_ID)
        for p in dl_pages:
            pr = p.get('properties', {})
            dt = _parse_date(tc._date(pr.get('Due Date')) or tc._date(pr.get('Date')))
            title = tc._title(pr.get('Name')) or tc._title(pr.get('Deadline')) or 'Milestone'
            if dt and s <= dt <= e:
                events.append({
                    'date': dt.isoformat(), 'label': title,
                    'type': 'milestone', 'color': 'done', 'deadline_id': p.get('id'),
                })
    except Exception:
        pass

    events.sort(key=lambda x: x['date'])
    return JSONResponse({'events': events, 'start': s.isoformat(), 'end': e.isoformat()})


# ===========================================================================
# 5. GLOBAL SEARCH
# ===========================================================================
@router.get('/api/search')
async def global_search(request: Request, q: str = Query('', min_length=0)):
    _require_auth(request)
    q = (q or '').strip().lower()
    if not q:
        return JSONResponse({'results': [], 'query': ''})

    results: List[Dict[str, Any]] = []

    # Deals
    for p in await _notion_query(DEALS_DS_ID):
        d = _parse_deal(p)
        hay = ' '.join(str(d.get(k) or '') for k in
                       ('address', 'buyer_name', 'buyer_agent_name', 'title_company',
                        'mls_number', 'lender_name', 'status')).lower()
        if q in hay:
            results.append({'type': 'deal', 'id': d.get('id'),
                            'title': d.get('address') or 'Unknown deal',
                            'subtitle': f'{d.get("status") or ""} · {d.get("buyer_name") or ""}'.strip(' ·'),
                            'view': 'deals'})

    # Owners
    try:
        owner_pages = await _notion_query(OWNERS_DS_ID)
        for p in owner_pages:
            o = pl._parse_owner(p)
            hay = ' '.join(str(o.get(k) or '') for k in
                           ('name', 'mailing_address', 'mailing_city', 'mailing_state',
                            'county', 'primary_phone', 'outreach_stage')).lower()
            if q in hay:
                results.append({'type': 'owner', 'id': o.get('id'),
                                'title': o.get('name') or 'Unknown owner',
                                'subtitle': f'{o.get("mailing_city") or ""} {o.get("mailing_state") or ""} · {o.get("outreach_stage") or ""}'.strip(' ·'),
                                'view': 'owners'})
    except Exception:
        pass

    # Comms
    for p in await _notion_query(TCM_DS_ID):
        c = _parse_comm(p)
        hay = ' '.join(str(c.get(k) or '') for k in
                       ('subject', 'to_name', 'to_email', 'body', 'status')).lower()
        if q in hay:
            results.append({'type': 'comm', 'id': c.get('id'),
                            'title': c.get('subject') or '(no subject)',
                            'subtitle': f'{c.get("status") or ""} · {c.get("to_name") or c.get("to_email") or ""}'.strip(' ·'),
                            'view': 'tc'})

    return JSONResponse({'results': results[:50], 'query': q, 'count': len(results)})


# ===========================================================================
# 6. SETTINGS
# ===========================================================================
@router.get('/api/settings')
async def get_settings(request: Request):
    _require_auth(request)
    return JSONResponse(_get_settings())


@router.post('/api/settings')
async def update_settings(request: Request):
    _require_auth(request)
    body = await request.json()
    s = _get_settings()
    for k in DEFAULT_SETTINGS:
        if k in body:
            s[k] = body[k]
    _save_json(SETTINGS_PATH, s)
    return JSONResponse({'ok': True, 'settings': s})


# ===========================================================================
# 7. EMAIL TEMPLATE LIBRARY
# ===========================================================================
@router.get('/api/templates')
async def list_templates(request: Request):
    _require_auth(request)
    return JSONResponse({'templates': _load_json(TEMPLATES_PATH, DEFAULT_TEMPLATES)})


@router.post('/api/templates')
async def save_template(request: Request):
    _require_auth(request)
    body = await request.json()
    tmpls = _load_json(TEMPLATES_PATH, DEFAULT_TEMPLATES)
    tid = body.get('id')
    rec = {'id': tid or f'tmpl_{int(datetime.now().timestamp())}',
           'name': body.get('name', 'Untitled'),
           'subject': body.get('subject', ''),
           'body': body.get('body', '')}
    if tid:
        tmpls = [rec if t['id'] == tid else t for t in tmpls]
        if not any(t['id'] == tid for t in tmpls):
            tmpls.append(rec)
    else:
        tmpls.append(rec)
    _save_json(TEMPLATES_PATH, tmpls)
    return JSONResponse({'ok': True, 'template': rec, 'templates': tmpls})


@router.delete('/api/templates/{tid}')
async def delete_template(request: Request, tid: str):
    _require_auth(request)
    tmpls = [t for t in _load_json(TEMPLATES_PATH, DEFAULT_TEMPLATES) if t['id'] != tid]
    _save_json(TEMPLATES_PATH, tmpls)
    return JSONResponse({'ok': True, 'templates': tmpls})


# ===========================================================================
# 8. EXPORTS (CSV)
# ===========================================================================
def _csv_response(rows: List[Dict], fieldnames: List[str], filename: str):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction='ignore')
    w.writeheader()
    for r in rows:
        w.writerow(r)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@router.get('/api/exports/deals.csv')
async def export_deals(request: Request):
    _require_auth(request)
    rows = [_parse_deal(p) for p in await _notion_query(DEALS_DS_ID)]
    fields = ['address', 'status', 'purchase_price', 'commission_pct', 'contract_date',
              'inspection_deadline', 'financing_deadline', 'closing_date',
              'buyer_name', 'buyer_agent_name', 'buyer_agent_email', 'lender_name',
              'title_company', 'mls_number']
    return _csv_response(rows, fields, 'deals.csv')


@router.get('/api/exports/comms.csv')
async def export_comms(request: Request):
    _require_auth(request)
    rows = [_parse_comm(p) for p in await _notion_query(TCM_DS_ID)]
    fields = ['subject', 'to_name', 'to_email', 'recipient_role', 'status',
              'date_created', 'sent_date', 'triggered_by']
    return _csv_response(rows, fields, 'comms.csv')


@router.get('/api/exports/tasks.csv')
async def export_tasks(request: Request):
    _require_auth(request)
    rows = []
    try:
        import main
        for t in main._board.values():
            rows.append({
                'id': t.get('id'), 'title': t.get('title'), 'status': t.get('status'),
                'assignee': t.get('assignee') or 'unassigned',
                'created_at': t.get('created_at'), 'department': t.get('department', ''),
            })
    except Exception:
        pass
    return _csv_response(rows, ['id', 'title', 'status', 'assignee', 'department', 'created_at'], 'tasks.csv')
