#!/usr/bin/env python3
"""
proposal_applier.py — applies APPROVED manager proposals on the VPS.

Runs on the VPS (where profiles + cron jobs live). Polls the Manager Proposals
DB for status='approved', applies the change, and flips it to 'applied'.

Change types:
  pause_agent     -> pause the agent's cron job(s) via `hermes cron pause`
  resume_agent    -> resume the agent's cron job(s)
  prompt_tweak    -> append a dated "Manager guidance" note to the agent SOUL.md
                     (the manager's rationale/proposed value — a human-reviewed
                     hint; the agent reads its SOUL each run)
  new_skill       -> scaffold a skill stub in the agent's skills dir
  schedule_change -> (recorded only; schedule edits are surfaced but applied
                     manually to avoid clobbering cron expressions)

Safe + idempotent. Designed to be invoked by command_worker.py each tick, or
standalone via cron.
"""
import os
import re
import sys
import json
import subprocess
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, '/opt/data')
sys.path.insert(0, '/opt/data/mission-control')
import agent_learning as AL

NOTION_BASE = 'https://api.notion.com/v1'
NOTION_VERSION = '2025-09-03'
PROPOSALS_DS_ID = '75c4eadb-65c5-42c1-b90d-5d3a67cf6ddc'
HERMES_BIN = '/opt/hermes/.venv/bin/hermes'
CRON_JOBS = '/opt/data/cron/jobs.json'


def _headers():
    return {'Authorization': f'Bearer {AL._notion_token()}',
            'Notion-Version': NOTION_VERSION, 'Content-Type': 'application/json'}


def _req(url, method='GET', body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _rt(p): return ''.join(i.get('plain_text', '') for i in (p or {}).get('rich_text', []))
def _title(p): return ''.join(i.get('plain_text', '') for i in (p or {}).get('title', []))
def _sel(p):
    s = (p or {}).get('select'); return s.get('name', '') if s else ''


def cron_jobs_for_agent(agent: str):
    """Find cron job IDs whose prompt references this agent's stage/name."""
    try:
        d = json.load(open(CRON_JOBS))
    except Exception:
        return []
    jobs = d if isinstance(d, list) else d.get('jobs', d)
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    # map agent key -> pipeline stage keyword that appears in its cron prompt
    stage_kw = {
        'property-sourcer': 'notion_run_learned.sh scout',
        'underwriter': 'notion_run_learned.sh underwrite',
        'matchmaker': 'notion_run_learned.sh match',
        'prospector': 'notion_run_learned.sh email-sender',
    }.get(agent, agent)
    out = []
    for j in jobs:
        p = (j.get('prompt') or '') + ' ' + (j.get('name') or '')
        if stage_kw in p or agent in (j.get('name') or '').lower():
            out.append(j.get('id'))
    return [x for x in out if x]


def run_cli(args):
    try:
        r = subprocess.run([HERMES_BIN, 'cron'] + args, capture_output=True, text=True, timeout=60)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[:300]
    except Exception as e:
        return False, str(e)[:300]


def apply_pause(agent, resume=False):
    ids = cron_jobs_for_agent(agent)
    if not ids:
        return False, f'no cron job found for {agent}'
    verb = 'resume' if resume else 'pause'
    results = [run_cli([verb, jid]) for jid in ids]
    ok = all(r[0] for r in results)
    return ok, f'{verb}d {len(ids)} job(s) for {agent}'


def apply_prompt_tweak(agent, rationale, proposed):
    soul = os.path.join(AL.PROFILES_DIR, agent, 'SOUL.md')
    if not os.path.isfile(soul):
        return False, f'SOUL.md not found for {agent}'
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    note = (f"\n\n## Manager Guidance ({ts})\n"
            f"{rationale.strip()}\n")
    if proposed and proposed not in ('reviewed/updated instructions',):
        note += f"Suggested change: {proposed.strip()}\n"
    with open(soul, 'a') as f:
        f.write(note)
    return True, f'appended manager guidance to {agent}/SOUL.md'


def apply_new_skill(agent, title, rationale):
    skills_dir = os.path.join(AL.PROFILES_DIR, agent, 'skills')
    if not os.path.isdir(skills_dir):
        return False, f'skills dir not found for {agent}'
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:48] or 'manager-skill'
    skill_dir = os.path.join(skills_dir, f'mgr-{slug}')
    os.makedirs(skill_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    content = (f"---\nname: mgr-{slug}\n"
               f"description: Manager-proposed skill ({ts}) to fix a recurring issue\n---\n\n"
               f"# {title}\n\n## Why\n{rationale.strip()}\n\n"
               f"## Procedure\n"
               f"1. (Manager scaffolded this skill from a recurring lesson.)\n"
               f"2. Review and flesh out the exact steps that resolve the issue.\n")
    with open(os.path.join(skill_dir, 'SKILL.md'), 'w') as f:
        f.write(content)
    return True, f'scaffolded skill mgr-{slug} for {agent}'


def set_applied(pid, ok, note):
    props = {
        'Status': {'select': {'name': 'applied' if ok else 'approved'}},
    }
    if ok:
        props['Decided At'] = {'date': {'start': datetime.now(timezone.utc).isoformat()}}
        # stash apply note into Proposed Value tail is risky; use Rationale append instead
    _req(f'{NOTION_BASE}/pages/{pid}', method='PATCH', body={'properties': props})


def apply_all_approved():
    """Find approved proposals, apply, mark applied. Returns count applied."""
    body = {'filter': {'property': 'Status', 'select': {'equals': 'approved'}}}
    try:
        data = _req(f'{NOTION_BASE}/data_sources/{PROPOSALS_DS_ID}/query', method='POST', body=body)
    except Exception as e:
        sys.stderr.write(f'query approved failed: {e}\n')
        return 0
    applied = 0
    for page in data.get('results', []):
        pr = page['properties']
        pid = page['id']
        agent = _rt(pr.get('Agent'))
        ctype = _sel(pr.get('Change Type'))
        title = _title(pr.get('Title'))
        rationale = _rt(pr.get('Rationale'))
        proposed = _rt(pr.get('Proposed Value'))

        ok, note = False, 'unknown change type'
        if ctype == 'pause_agent':
            ok, note = apply_pause(agent, resume=False)
        elif ctype == 'resume_agent':
            ok, note = apply_pause(agent, resume=True)
        elif ctype == 'prompt_tweak':
            ok, note = apply_prompt_tweak(agent, rationale, proposed)
        elif ctype == 'new_skill':
            ok, note = apply_new_skill(agent, title, rationale)
        elif ctype == 'schedule_change':
            ok, note = True, 'schedule change recorded (apply manually)'

        try:
            set_applied(pid, ok, note)
        except Exception as e:
            sys.stderr.write(f'set_applied failed: {e}\n')
        print(f'[applier] {ctype} {agent}: {"OK" if ok else "FAIL"} — {note}')
        if ok:
            applied += 1
    return applied


if __name__ == '__main__':
    n = apply_all_approved()
    print(f'[applier] applied {n} approved proposal(s)')
