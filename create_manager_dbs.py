#!/usr/bin/env python3
"""Create the Agent Runs (ledger) and Manager Proposals databases in Notion."""
import sys, json, httpx, asyncio
sys.path.insert(0, '/opt/data/mission-control')
import tc

ROOT_PAGE = '38f9925f-e691-819d-9f93-fe461117ed82'

RUNS_PROPS = {
    'Run': {'title': {}},                       # e.g. "property-sourcer 2026-07-01 07:00"
    'Agent': {'rich_text': {}},                 # agent/profile key
    'Department': {'select': {'options': [
        {'name': 'Sourcing', 'color': 'blue'},
        {'name': 'Underwriting', 'color': 'purple'},
        {'name': 'Matchmaking', 'color': 'pink'},
        {'name': 'Outreach', 'color': 'green'},
        {'name': 'Transaction Coordination', 'color': 'orange'},
        {'name': 'Management', 'color': 'red'},
    ]}},
    'Status': {'select': {'options': [
        {'name': 'success', 'color': 'green'},
        {'name': 'partial', 'color': 'yellow'},
        {'name': 'failure', 'color': 'red'},
    ]}},
    'Summary': {'rich_text': {}},               # what it produced
    'Error': {'rich_text': {}},                 # error text if any
    'Items Processed': {'number': {'format': 'number'}},
    'Duration Sec': {'number': {'format': 'number'}},
    'Run At': {'date': {}},
    'Lesson Logged': {'checkbox': {}},          # did this run append a lesson?
}

PROPOSALS_PROPS = {
    'Title': {'title': {}},
    'Agent': {'rich_text': {}},
    'Department': {'rich_text': {}},
    'Change Type': {'select': {'options': [
        {'name': 'prompt_tweak', 'color': 'blue'},
        {'name': 'pause_agent', 'color': 'orange'},
        {'name': 'resume_agent', 'color': 'green'},
        {'name': 'new_skill', 'color': 'purple'},
        {'name': 'schedule_change', 'color': 'yellow'},
    ]}},
    'Rationale': {'rich_text': {}},             # why the manager proposes this
    'Current Value': {'rich_text': {}},         # what it is now
    'Proposed Value': {'rich_text': {}},        # what manager wants it to be
    'Status': {'select': {'options': [
        {'name': 'pending', 'color': 'yellow'},
        {'name': 'approved', 'color': 'green'},
        {'name': 'rejected', 'color': 'red'},
        {'name': 'applied', 'color': 'blue'},
    ]}},
    'Proposed At': {'date': {}},
    'Decided At': {'date': {}},
    'Apply Target': {'rich_text': {}},          # e.g. profile path / job id
    'Briefing Batch': {'rich_text': {}},        # groups proposals from one manager run
}


async def make_db(h, title, props):
    body = {
        'parent': {'type': 'page_id', 'page_id': ROOT_PAGE},
        'title': [{'type': 'text', 'text': {'content': title}}],
        'is_inline': False,
        'initial_data_source': {'properties': props},
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post('https://api.notion.com/v1/databases', headers=h, json=body)
        if r.status_code >= 300:
            print('ERROR', title, r.status_code, r.text[:300]); return None, None
        d = r.json()
        ds = (d.get('data_sources') or [{}])[0].get('id')
        return d['id'], ds


async def main():
    h = tc._notion_headers()
    r_db, r_ds = await make_db(h, '🗂️ Agent Runs', RUNS_PROPS)
    print('RUNS_DB_ID =', r_db)
    print('RUNS_DS_ID =', r_ds)
    p_db, p_ds = await make_db(h, '📋 Manager Proposals', PROPOSALS_PROPS)
    print('PROPOSALS_DB_ID =', p_db)
    print('PROPOSALS_DS_ID =', p_ds)

asyncio.run(main())
