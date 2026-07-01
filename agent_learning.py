#!/usr/bin/env python3
"""
agent_learning.py — self-correction + run ledger for the agent corporation.

Two responsibilities, both designed to keep token usage LEAN:

1. RUN LEDGER — log_run() writes a structured record of each agent run to the
   Notion "Agent Runs" DB. This is cheap metadata (no LLM), and becomes the
   raw material the Manager Agent reads.

2. SELF-CORRECTION — each agent keeps a per-profile LESSONS.md. Agents:
     • read_lessons(agent)  -> inject terse past lessons into their next run
     • log_lesson(agent, …) -> append a new lesson after a failure/degradation
   Because each agent only ever loads ITS OWN lessons (not the whole org's),
   context stays small.

Usable as a library (import) or CLI:
    python3 agent_learning.py log-run --agent scout --department Sourcing \
        --status success --summary "Added 12 leads" --items 12 --duration 45
    python3 agent_learning.py log-lesson --agent scout \
        --lesson "Zillow IN filter URL changed; use /IN/ path segment"
    python3 agent_learning.py read-lessons --agent scout
"""
import os
import sys
import json
import argparse
from datetime import datetime, timezone

# Notion config (reuse mission-control token resolution if importable)
NOTION_BASE = 'https://api.notion.com/v1'
NOTION_VERSION = '2025-09-03'
RUNS_DB_ID = 'be5dfa84-c9f9-471b-9490-929374b8619b'

PROFILES_DIR = '/opt/data/profiles'

# Agent key -> Department mapping (single source of truth)
AGENT_DEPARTMENTS = {
    'property-sourcer': 'Sourcing',
    'scout': 'Sourcing',
    'owner-researcher': 'Sourcing',
    'buyer-sourcer': 'Sourcing',
    'underwriter': 'Underwriting',
    'deal-screener': 'Underwriting',
    'matchmaker': 'Matchmaking',
    'investor-profiler': 'Matchmaking',
    'prospector': 'Outreach',
    'lead-agent': 'Outreach',
    'marketing-agent': 'Outreach',
    'client-agent': 'Outreach',
    'inbox-monitor': 'Transaction Coordination',
    'research-agent': 'Management',
    'manager': 'Management',
}


def department_for(agent: str) -> str:
    return AGENT_DEPARTMENTS.get(agent, 'Management')


# ---------------------------------------------------------------------------
# Notion token
# ---------------------------------------------------------------------------
def _notion_token() -> str:
    # 1. env
    for k in ('NOTION_API_KEY', 'NOTION_TOKEN'):
        if os.environ.get(k):
            return os.environ[k]
    # 2. cli-config.yaml (ntn_...)
    import re
    cfg = '/opt/data/.hermes/cli-config.yaml'
    if os.path.isfile(cfg):
        m = re.search(r'ntn_[A-Za-z0-9]+', open(cfg).read())
        if m:
            return m.group(0)
    # 3. .env
    for env_path in ('/opt/data/.env', os.path.expanduser('~/.hermes/.env')):
        if os.path.isfile(env_path):
            for line in open(env_path):
                if line.startswith('NOTION_API_KEY=') or line.startswith('NOTION_TOKEN='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    return ''


def _headers() -> dict:
    return {
        'Authorization': f'Bearer {_notion_token()}',
        'Notion-Version': NOTION_VERSION,
        'Content-Type': 'application/json',
    }


# ---------------------------------------------------------------------------
# RUN LEDGER
# ---------------------------------------------------------------------------
def log_run(agent: str, status: str, summary: str = '', error: str = '',
            items: int = 0, duration: float = 0, department: str = '',
            lesson_logged: bool = False) -> bool:
    """Write a run record to the Notion Agent Runs DB. Returns True on success."""
    import urllib.request
    dept = department or department_for(agent)
    now = datetime.now(timezone.utc)
    title = f"{agent} {now.strftime('%Y-%m-%d %H:%M UTC')}"

    def rt(v):
        return {'rich_text': [{'type': 'text', 'text': {'content': (v or '')[:1900]}}]}

    props = {
        'Run': {'title': [{'type': 'text', 'text': {'content': title}}]},
        'Agent': rt(agent),
        'Department': {'select': {'name': dept}},
        'Status': {'select': {'name': status}},
        'Summary': rt(summary),
        'Run At': {'date': {'start': now.isoformat()}},
        'Lesson Logged': {'checkbox': bool(lesson_logged)},
    }
    if error:
        props['Error'] = rt(error)
    if items:
        props['Items Processed'] = {'number': items}
    if duration:
        props['Duration Sec'] = {'number': round(duration, 1)}

    body = json.dumps({'parent': {'database_id': RUNS_DB_ID}, 'properties': props}).encode()
    req = urllib.request.Request(f'{NOTION_BASE}/pages', data=body, headers=_headers(), method='POST')
    try:
        import urllib.error
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status in (200, 201)
    except Exception as e:
        sys.stderr.write(f'log_run failed: {e}\n')
        return False


# ---------------------------------------------------------------------------
# SELF-CORRECTION — per-agent LESSONS.md
# ---------------------------------------------------------------------------
def _lessons_path(agent: str) -> str:
    d = os.path.join(PROFILES_DIR, agent)
    if not os.path.isdir(d):
        # fall back to a shared lessons dir for non-profile agents
        d = os.path.join('/opt/data/mission-control', 'agent_lessons')
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, f'{agent}.LESSONS.md')
    return os.path.join(d, 'LESSONS.md')


def read_lessons(agent: str, max_chars: int = 2000) -> str:
    """Return the agent's accumulated lessons (most recent first), capped for lean context."""
    path = _lessons_path(agent)
    if not os.path.isfile(path):
        return ''
    txt = open(path).read().strip()
    if len(txt) > max_chars:
        # keep the most recent lessons (file is append-only newest-last, so tail)
        txt = txt[-max_chars:]
        txt = txt[txt.find('\n') + 1:]  # drop partial first line
        txt = '…(older lessons trimmed)\n' + txt
    return txt


def log_lesson(agent: str, lesson: str, context: str = '') -> bool:
    """Append a terse lesson the agent learned. Keeps entries short = lean context."""
    path = _lessons_path(agent)
    header_needed = not os.path.isfile(path)
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    entry = f"- [{ts}] {lesson.strip()}"
    if context:
        entry += f" (ctx: {context.strip()[:120]})"
    try:
        with open(path, 'a') as f:
            if header_needed:
                f.write(f"# Lessons — {agent}\n"
                        f"Terse, self-authored notes. Read before each run; append after failures.\n\n")
            f.write(entry + '\n')
        return True
    except Exception as e:
        sys.stderr.write(f'log_lesson failed: {e}\n')
        return False


def prune_lessons(agent: str, keep: int = 40):
    """Keep only the most recent `keep` lessons so files never bloat."""
    path = _lessons_path(agent)
    if not os.path.isfile(path):
        return
    lines = open(path).read().splitlines()
    header = [l for l in lines if not l.startswith('- [')]
    lessons = [l for l in lines if l.startswith('- [')]
    if len(lessons) <= keep:
        return
    lessons = lessons[-keep:]
    with open(path, 'w') as f:
        f.write('\n'.join(header[:3] + [''] + lessons) + '\n')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)

    r = sub.add_parser('log-run')
    r.add_argument('--agent', required=True)
    r.add_argument('--department', default=None)
    r.add_argument('--status', required=True, choices=['success', 'partial', 'failure'])
    r.add_argument('--summary', default='')
    r.add_argument('--error', default='')
    r.add_argument('--items', type=int, default=0)
    r.add_argument('--duration', type=float, default=0)
    r.add_argument('--lesson-logged', action='store_true')

    l = sub.add_parser('log-lesson')
    l.add_argument('--agent', required=True)
    l.add_argument('--lesson', required=True)
    l.add_argument('--context', default='')

    rl = sub.add_parser('read-lessons')
    rl.add_argument('--agent', required=True)

    pr = sub.add_parser('prune-lessons')
    pr.add_argument('--agent', required=True)
    pr.add_argument('--keep', type=int, default=40)

    args = ap.parse_args()
    if args.cmd == 'log-run':
        ok = log_run(args.agent, args.status, args.summary, args.error,
                     args.items, args.duration, args.department, args.lesson_logged)
        print('logged' if ok else 'FAILED')
    elif args.cmd == 'log-lesson':
        ok = log_lesson(args.agent, args.lesson, args.context)
        print('logged' if ok else 'FAILED')
    elif args.cmd == 'read-lessons':
        print(read_lessons(args.agent) or '(no lessons yet)')
    elif args.cmd == 'prune-lessons':
        prune_lessons(args.agent, args.keep)
        print('pruned')


if __name__ == '__main__':
    main()
