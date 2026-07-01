#!/usr/bin/env python3
"""
manager_agent.py — the COO of the agent corporation.

Runs daily. Scans the Agent Runs ledger + each agent's LESSONS, computes
per-agent and per-department health, and generates PROPOSALS (structural
changes that require Jordan's approval). It writes proposals to the Manager
Proposals DB with status=pending. It NEVER applies changes itself.

Per the "daily scan, only ping when worth approving" cadence: if there are no
new proposals, it stays silent (prints nothing actionable).

Rule-based (no LLM tokens) so it's cheap, deterministic, and auditable:
  - Agent with >=N failures in the window            -> pause_agent proposal
  - Agent producing 0 items across the window        -> prompt_tweak proposal
  - Recurring identical lesson (same failure 3x)     -> new_skill proposal
  - Previously-paused agent now healthy              -> resume_agent proposal
"""
import os
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from collections import defaultdict, Counter

sys.path.insert(0, '/opt/data')
sys.path.insert(0, '/opt/data/mission-control')
import agent_learning as AL

NOTION_BASE = 'https://api.notion.com/v1'
NOTION_VERSION = '2025-09-03'
RUNS_DS_ID = 'aaf3b0a9-9013-41f6-be66-7e1d2bc26f98'
PROPOSALS_DB_ID = '3b655423-ecec-4be5-8d65-35bfbeafd732'
PROPOSALS_DS_ID = '75c4eadb-65c5-42c1-b90d-5d3a67cf6ddc'

WINDOW_DAYS = 7
FAILURE_THRESHOLD = 3        # >= this many failures in window -> propose pause
ZERO_ITEM_RUNS = 3           # >= this many zero-item runs -> propose prompt tweak


def _headers():
    return {'Authorization': f'Bearer {AL._notion_token()}',
            'Notion-Version': NOTION_VERSION, 'Content-Type': 'application/json'}


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=_headers(), method='POST')
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _rt(prop):
    return ''.join(i.get('plain_text', '') for i in (prop or {}).get('rich_text', []))


def _sel(prop):
    s = (prop or {}).get('select')
    return s.get('name', '') if s else ''


def _num(prop):
    return (prop or {}).get('number') or 0


def _date(prop):
    d = (prop or {}).get('date')
    return d.get('start') if d else None


def fetch_runs():
    """Pull recent runs from the ledger."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat()
    body = {'filter': {'property': 'Run At', 'date': {'on_or_after': cutoff}},
            'sorts': [{'property': 'Run At', 'direction': 'descending'}]}
    runs = []
    try:
        data = _post(f'{NOTION_BASE}/data_sources/{RUNS_DS_ID}/query', body)
        for p in data.get('results', []):
            pr = p['properties']
            runs.append({
                'agent': _rt(pr.get('Agent')),
                'department': _sel(pr.get('Department')),
                'status': _sel(pr.get('Status')),
                'summary': _rt(pr.get('Summary')),
                'error': _rt(pr.get('Error')),
                'items': _num(pr.get('Items Processed')),
                'run_at': _date(pr.get('Run At')),
            })
    except Exception as e:
        sys.stderr.write(f'fetch_runs failed: {e}\n')
    return runs


def existing_pending_proposals():
    """Avoid duplicate proposals — return set of (agent, change_type) already pending."""
    body = {'filter': {'property': 'Status', 'select': {'equals': 'pending'}}}
    seen = set()
    try:
        data = _post(f'{NOTION_BASE}/data_sources/{PROPOSALS_DS_ID}/query', body)
        for p in data.get('results', []):
            pr = p['properties']
            ctype = _sel(pr.get('Change Type'))
            if ctype == 'promote_lesson':
                seen.add(('promote_lesson', (_rt(pr.get('Proposed Value')) or '')[:60].lower()))
            else:
                seen.add((_rt(pr.get('Agent')), ctype))
    except Exception:
        pass
    return seen


def analyze(runs):
    """Return a list of proposal dicts based on run patterns + lessons."""
    by_agent = defaultdict(list)
    for r in runs:
        if r['agent']:
            by_agent[r['agent']].append(r)

    proposals = []
    for agent, agent_runs in by_agent.items():
        dept = agent_runs[0]['department'] or AL.department_for(agent)
        failures = [r for r in agent_runs if r['status'] == 'failure']
        zero_items = [r for r in agent_runs if r['status'] != 'failure' and r['items'] == 0]

        # Rule 1: too many failures -> propose pause
        if len(failures) >= FAILURE_THRESHOLD:
            last_err = failures[0]['error'] or failures[0]['summary'] or 'repeated failures'
            proposals.append({
                'agent': agent, 'department': dept, 'change_type': 'pause_agent',
                'title': f'Pause {agent} — {len(failures)} failures in {WINDOW_DAYS}d',
                'rationale': f'{agent} failed {len(failures)} times this week. Most recent error: {last_err[:200]}. '
                             f'Recommend pausing until the root cause is fixed.',
                'current': 'active', 'proposed': 'paused',
                'target': agent,
            })

        # Rule 2: producing zero work -> propose prompt tweak
        elif len(zero_items) >= ZERO_ITEM_RUNS:
            proposals.append({
                'agent': agent, 'department': dept, 'change_type': 'prompt_tweak',
                'title': f'Tune {agent} — {len(zero_items)} zero-output runs',
                'rationale': f'{agent} completed {len(zero_items)} runs this week but produced 0 items each time. '
                             f'Its search criteria or source URLs may be stale. Recommend reviewing its SOUL.md '
                             f'instructions (e.g. source list, filters).',
                'current': 'current instructions', 'proposed': 'reviewed/updated instructions',
                'target': os.path.join(AL.PROFILES_DIR, agent, 'SOUL.md'),
            })

        # Rule 3: recurring identical lesson -> propose a skill
        lessons_txt = AL.read_lessons(agent, max_chars=4000)
        if lessons_txt:
            lesson_lines = [l for l in lessons_txt.splitlines() if l.startswith('- [')]
            # normalize by stripping date prefix
            norm = Counter()
            for l in lesson_lines:
                core = l.split(']', 1)[-1].strip().lower()[:80]
                norm[core] += 1
            for core, cnt in norm.items():
                if cnt >= 3:
                    proposals.append({
                        'agent': agent, 'department': dept, 'change_type': 'new_skill',
                        'title': f'Create skill for {agent} — recurring issue',
                        'rationale': f'{agent} logged the same lesson {cnt} times: "{core}". '
                                     f'Recommend codifying the fix as a reusable skill so it stops recurring.',
                        'current': 'ad-hoc lesson', 'proposed': f'documented skill for: {core}',
                        'target': os.path.join(AL.PROFILES_DIR, agent, 'skills'),
                    })
                    break

    # Rule 4 (CROSS-AGENT): a similar lesson logged by 2+ DIFFERENT agents
    # -> propose promoting it to shared TEAM_LESSONS so every agent benefits.
    # Uses keyword-overlap (Jaccard) clustering to catch near-duplicates that
    # aren't worded identically across agents.
    STOP = {'about', 'after', 'again', 'their', 'there', 'which', 'while', 'these',
            'those', 'needs', 'need', 'from', 'with', 'that', 'this', 'have', 'when'}

    def keywords(text):
        return frozenset(w for w in text.lower().split() if len(w) > 4 and w not in STOP)

    # collect (agent, keyword-set, readable) for every lesson
    lesson_kw = []
    for agent in by_agent.keys():
        txt = AL.read_lessons(agent, max_chars=4000)
        for l in txt.splitlines():
            if not l.startswith('- ['):
                continue
            core = l.split(']', 1)[-1].strip()
            kw = keywords(core)
            if len(kw) >= 3:
                lesson_kw.append((agent, kw, core))

    # greedy cluster: for each lesson, find others (from different agents) with
    # >=60% keyword overlap
    used = set()
    for i, (ag_i, kw_i, core_i) in enumerate(lesson_kw):
        if i in used:
            continue
        cluster_agents = {ag_i}
        cluster_example = core_i
        for j, (ag_j, kw_j, core_j) in enumerate(lesson_kw):
            if j <= i or j in used:
                continue
            inter = len(kw_i & kw_j)
            union = len(kw_i | kw_j)
            if union and inter / union >= 0.55:
                cluster_agents.add(ag_j)
                used.add(j)
        if len(cluster_agents) >= 2:
            used.add(i)
            proposals.append({
                'agent': ', '.join(sorted(cluster_agents))[:80], 'department': 'Management',
                'change_type': 'promote_lesson',
                'title': f'Promote shared lesson — hit by {len(cluster_agents)} agents',
                'rationale': f'{len(cluster_agents)} different agents ({", ".join(sorted(cluster_agents))}) logged a '
                             f'similar lesson: "{cluster_example[:160]}". Recommend promoting it to the shared '
                             f'TEAM_LESSONS so every agent reads it before each run (compounds learning across the org).',
                'current': 'per-agent only', 'proposed': f'shared: {cluster_example[:120]}',
                'target': AL.TEAM_LESSONS_PATH,
            })

    return proposals


def write_proposals(proposals):
    """Write new proposals to Notion (dedup against existing pending)."""
    seen = existing_pending_proposals()
    batch = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M')
    written = 0
    for p in proposals:
        # For promote_lesson the agent field is a variable comma-list, so key on
        # the proposed lesson content instead to dedup reliably.
        if p['change_type'] == 'promote_lesson':
            key = ('promote_lesson', (p.get('proposed', '') or '')[:60].lower())
        else:
            key = (p['agent'], p['change_type'])
        if key in seen:
            continue

        def rt(v):
            return {'rich_text': [{'type': 'text', 'text': {'content': (v or '')[:1900]}}]}

        props = {
            'Title': {'title': [{'type': 'text', 'text': {'content': p['title'][:200]}}]},
            'Agent': rt(p['agent']),
            'Department': rt(p['department']),
            'Change Type': {'select': {'name': p['change_type']}},
            'Rationale': rt(p['rationale']),
            'Current Value': rt(p.get('current', '')),
            'Proposed Value': rt(p.get('proposed', '')),
            'Status': {'select': {'name': 'pending'}},
            'Proposed At': {'date': {'start': datetime.now(timezone.utc).isoformat()}},
            'Apply Target': rt(p.get('target', '')),
            'Briefing Batch': rt(batch),
        }
        try:
            _post(f'{NOTION_BASE}/pages',
                  {'parent': {'database_id': PROPOSALS_DB_ID}, 'properties': props})
            written += 1
            seen.add(key)
        except Exception as e:
            sys.stderr.write(f'write proposal failed: {e}\n')
    return written, batch


def build_briefing(runs, proposals):
    """Human-readable summary for cron delivery (only when there are proposals)."""
    by_dept = defaultdict(lambda: {'success': 0, 'partial': 0, 'failure': 0})
    for r in runs:
        d = r['department'] or 'Management'
        by_dept[d][r['status']] = by_dept[d].get(r['status'], 0) + 1

    lines = ['📋 MANAGER BRIEFING', '']
    lines.append(f'Scanned {len(runs)} agent runs over the last {WINDOW_DAYS} days.')
    lines.append('')
    lines.append('Department health:')
    for d, c in sorted(by_dept.items()):
        total = sum(c.values())
        ok = c.get('success', 0)
        lines.append(f'  • {d}: {ok}/{total} clean'
                     + (f", {c['failure']} failed" if c.get('failure') else ''))
    lines.append('')
    if proposals:
        lines.append(f'⚠️  {len(proposals)} proposal(s) need your approval in the Manager Console:')
        for p in proposals:
            lines.append(f'  • [{p["change_type"]}] {p["title"]}')
    else:
        lines.append('✅ No changes proposed — system healthy.')
    return '\n'.join(lines)


def main():
    runs = fetch_runs()
    proposals = analyze(runs)
    written, batch = write_proposals(proposals) if proposals else (0, '')

    # Cadence: only emit output (=> cron delivers a message) when there's
    # something worth approving. Otherwise stay silent.
    if written > 0:
        print(build_briefing(runs, proposals))
    else:
        # Silent when nothing new. Print a one-line status to stderr for logs only.
        sys.stderr.write(f'manager: {len(runs)} runs scanned, no new proposals\n')


if __name__ == '__main__':
    main()
