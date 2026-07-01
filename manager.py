"""
manager.py — Manager Console backend.

Reads the Agent Runs ledger + Manager Proposals, exposes department/org health,
and lets Jordan approve/reject proposals. Approval only flips Notion status to
'approved'; the VPS command_worker applies approved structural changes on its
next tick (pause/resume cron, append SOUL.md guidance, scaffold a skill) and
marks them 'applied'. Nothing structural changes without Jordan's approval.
"""
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Any, Dict, List

import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

import tc
from tc import _require_auth, _notion_headers, NOTION_BASE

router = APIRouter()

RUNS_DS_ID = 'aaf3b0a9-9013-41f6-be66-7e1d2bc26f98'
PROPOSALS_DS_ID = '75c4eadb-65c5-42c1-b90d-5d3a67cf6ddc'

# Department -> agent roster (org chart)
DEPARTMENTS = {
    'Sourcing': ['property-sourcer', 'owner-researcher', 'buyer-sourcer'],
    'Underwriting': ['underwriter', 'deal-screener'],
    'Matchmaking': ['matchmaker', 'investor-profiler'],
    'Outreach': ['prospector', 'lead-agent', 'marketing-agent', 'client-agent'],
    'Transaction Coordination': ['inbox-monitor'],
    'Management': ['manager', 'research-agent'],
}
DEPT_META = {
    'Sourcing': {'icon': '🔍', 'desc': 'Finds on-market Indiana investment properties'},
    'Underwriting': {'icon': '📊', 'desc': 'Runs financials & screens deals'},
    'Matchmaking': {'icon': '🎯', 'desc': 'Matches deals to buyers & drafts outreach'},
    'Outreach': {'icon': '📬', 'desc': 'Contacts owners & prospects'},
    'Transaction Coordination': {'icon': '📋', 'desc': 'Monitors deals & deadlines'},
    'Management': {'icon': '🧭', 'desc': 'Oversees the org & reports to you'},
}


async def _query(ds_id: str, body: Dict = None) -> List[Dict]:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f'{NOTION_BASE}/data_sources/{ds_id}/query',
                         headers=_notion_headers(), json=(body if body is not None else {}))
    if r.status_code >= 300:
        raise HTTPException(502, f'Notion query error: {r.text[:200]}')
    return r.json().get('results', [])


def _rt(p): return ''.join(i.get('plain_text', '') for i in (p or {}).get('rich_text', []))
def _title(p): return ''.join(i.get('plain_text', '') for i in (p or {}).get('title', []))
def _sel(p):
    s = (p or {}).get('select'); return s.get('name', '') if s else ''
def _num(p): return (p or {}).get('number') or 0
def _date(p):
    d = (p or {}).get('date'); return d.get('start') if d else None
def _chk(p): return bool((p or {}).get('checkbox'))


def _parse_run(page):
    pr = page['properties']
    return {
        'id': page['id'],
        'agent': _rt(pr.get('Agent')),
        'department': _sel(pr.get('Department')),
        'status': _sel(pr.get('Status')),
        'summary': _rt(pr.get('Summary')),
        'error': _rt(pr.get('Error')),
        'items': _num(pr.get('Items Processed')),
        'duration': _num(pr.get('Duration Sec')),
        'run_at': _date(pr.get('Run At')),
        'lesson_logged': _chk(pr.get('Lesson Logged')),
    }


def _parse_proposal(page):
    pr = page['properties']
    return {
        'id': page['id'],
        'title': _title(pr.get('Title')),
        'agent': _rt(pr.get('Agent')),
        'department': _rt(pr.get('Department')),
        'change_type': _sel(pr.get('Change Type')),
        'rationale': _rt(pr.get('Rationale')),
        'current_value': _rt(pr.get('Current Value')),
        'proposed_value': _rt(pr.get('Proposed Value')),
        'status': _sel(pr.get('Status')),
        'proposed_at': _date(pr.get('Proposed At')),
        'decided_at': _date(pr.get('Decided At')),
        'apply_target': _rt(pr.get('Apply Target')),
    }


# ---------------------------------------------------------------------------
# Briefing + department health
# ---------------------------------------------------------------------------
@router.get('/api/manager/briefing')
async def manager_briefing(request: Request):
    _require_auth(request)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    run_pages = await _query(RUNS_DS_ID, {
        'filter': {'property': 'Run At', 'date': {'on_or_after': cutoff}},
        'sorts': [{'property': 'Run At', 'direction': 'descending'}],
    })
    runs = [_parse_run(p) for p in run_pages]

    # Department health
    dept_health = {}
    for dept, agents in DEPARTMENTS.items():
        drun = [r for r in runs if r['department'] == dept or r['agent'] in agents]
        succ = sum(1 for r in drun if r['status'] == 'success')
        part = sum(1 for r in drun if r['status'] == 'partial')
        fail = sum(1 for r in drun if r['status'] == 'failure')
        total = len(drun)
        health = 'healthy'
        if fail >= 3 or (total and fail / total > 0.4):
            health = 'critical'
        elif fail > 0 or part > total * 0.3:
            health = 'degraded'
        dept_health[dept] = {
            'icon': DEPT_META.get(dept, {}).get('icon', '•'),
            'desc': DEPT_META.get(dept, {}).get('desc', ''),
            'agents': agents,
            'runs': total, 'success': succ, 'partial': part, 'failure': fail,
            'health': health if total else 'idle',
        }

    # Pending proposals count
    pend = await _query(PROPOSALS_DS_ID, {
        'filter': {'property': 'Status', 'select': {'equals': 'pending'}}})

    return JSONResponse({
        'window_days': 7,
        'total_runs': len(runs),
        'departments': dept_health,
        'pending_proposals': len(pend),
        'generated_at': datetime.now(timezone.utc).isoformat(),
    })


# ---------------------------------------------------------------------------
# Per-agent detail (runs + lessons summary)
# ---------------------------------------------------------------------------
@router.get('/api/manager/agent/{agent}')
async def agent_detail(request: Request, agent: str):
    _require_auth(request)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    pages = await _query(RUNS_DS_ID, {
        'filter': {'and': [
            {'property': 'Agent', 'rich_text': {'equals': agent}},
            {'property': 'Run At', 'date': {'on_or_after': cutoff}},
        ]},
        'sorts': [{'property': 'Run At', 'direction': 'descending'}],
    })
    runs = [_parse_run(p) for p in pages]
    return JSONResponse({'agent': agent, 'runs': runs})


# ---------------------------------------------------------------------------
# Proposals — list, approve, reject
# ---------------------------------------------------------------------------
@router.get('/api/manager/proposals')
async def list_proposals(request: Request, status: str = ''):
    _require_auth(request)
    body: Dict[str, Any] = {}
    if status:
        body = {'filter': {'property': 'Status', 'select': {'equals': status}}}
    body['sorts'] = [{'property': 'Proposed At', 'direction': 'descending'}]
    pages = await _query(PROPOSALS_DS_ID, body)
    return JSONResponse({'proposals': [_parse_proposal(p) for p in pages]})


async def _set_status(proposal_id: str, status: str):
    props = {
        'Status': {'select': {'name': status}},
        'Decided At': {'date': {'start': datetime.now(timezone.utc).isoformat()}},
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.patch(f'{NOTION_BASE}/pages/{proposal_id}',
                          headers=_notion_headers(), json={'properties': props})
    if r.status_code >= 300:
        raise HTTPException(502, f'Notion update error: {r.text[:200]}')


@router.post('/api/manager/proposals/{proposal_id}/approve')
async def approve_proposal(request: Request, proposal_id: str):
    _require_auth(request)
    await _set_status(proposal_id, 'approved')
    return JSONResponse({'ok': True, 'status': 'approved',
                         'note': 'Change will be applied by the agent worker on its next tick.'})


@router.post('/api/manager/proposals/{proposal_id}/reject')
async def reject_proposal(request: Request, proposal_id: str):
    _require_auth(request)
    await _set_status(proposal_id, 'rejected')
    return JSONResponse({'ok': True, 'status': 'rejected'})


# ---------------------------------------------------------------------------
# Recent runs feed (for the console activity view)
# ---------------------------------------------------------------------------
@router.get('/api/manager/runs')
async def recent_runs(request: Request, limit: int = 40):
    _require_auth(request)
    pages = await _query(RUNS_DS_ID, {
        'sorts': [{'property': 'Run At', 'direction': 'descending'}]})
    runs = [_parse_run(p) for p in pages][:limit]
    return JSONResponse({'runs': runs})


@router.get('/api/manager/team-lessons')
async def team_lessons(request: Request):
    """Return the shared cross-agent lessons (the org brain).

    The TEAM_LESSONS file lives on the VPS; the command_worker pushes its
    contents to Render via /api/agent-control/poll, stored in main._agent_state.
    """
    _require_auth(request)
    try:
        import main
        lessons = main._agent_state.get('team_lessons', []) or []
    except Exception:
        lessons = []
    return JSONResponse({'lessons': lessons, 'count': len(lessons)})
