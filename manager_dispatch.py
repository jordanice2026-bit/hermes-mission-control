#!/usr/bin/env python3
"""
manager_dispatch.py — turn a Jordan text into an assigned, running task.

Called by the Manager gateway skill after it has classified a request. Does the
mechanical work:
  1. Validate the target agent/profile.
  2. Create a kanban task assigned to that agent (via `hermes kanban create`).
  3. Kick off dispatch so the agent starts immediately (`hermes kanban dispatch`).
  4. Print a concise confirmation for the manager to text back.

Usage:
    python3 manager_dispatch.py --agent <profile> --title "..." [--body "..."]
                                [--priority N] [--no-dispatch]

Prints JSON: {"ok":true,"task_id":"...","agent":"...","department":"...","dispatched":true,"message":"..."}
"""
import sys
import json
import argparse
import subprocess

sys.path.insert(0, '/opt/data')
sys.path.insert(0, '/opt/data/mission-control')

HERMES = '/opt/hermes/.venv/bin/hermes'

# Valid agent profiles + their departments (mirror of agent_learning.AGENT_DEPARTMENTS)
# Restricted to REAL assignable worker profiles (excludes 'manager' and the
# 'scout' alias, which are not dispatchable kanban assignees).
_EXCLUDE = {'manager', 'scout'}
try:
    import agent_learning as AL
    AGENT_DEPARTMENTS = {k: v for k, v in AL.AGENT_DEPARTMENTS.items() if k not in _EXCLUDE}
except Exception:
    AGENT_DEPARTMENTS = {
        'property-sourcer': 'Sourcing', 'owner-researcher': 'Sourcing', 'buyer-sourcer': 'Sourcing',
        'underwriter': 'Underwriting', 'deal-screener': 'Underwriting',
        'matchmaker': 'Matchmaking', 'investor-profiler': 'Matchmaking',
        'prospector': 'Outreach', 'lead-agent': 'Outreach', 'marketing-agent': 'Outreach', 'client-agent': 'Outreach',
        'inbox-monitor': 'Transaction Coordination',
        'research-agent': 'Management',
    }

VALID_AGENTS = set(AGENT_DEPARTMENTS.keys())


def create_task(agent, title, body, priority):
    cmd = [HERMES, 'kanban', 'create', title, '--assignee', agent,
           '--created-by', 'manager', '--json']
    if body:
        cmd += ['--body', body]
    if priority:
        cmd += ['--priority', str(priority)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f'create failed: {(r.stderr or r.stdout).strip()[:300]}')
    # parse the task id from JSON output
    out = (r.stdout or '').strip()
    try:
        data = json.loads(out)
        return data.get('id') or data.get('task_id') or ''
    except Exception:
        # fall back: look for a t_ token
        import re
        m = re.search(r'\bt_[0-9a-f]+\b', out)
        return m.group(0) if m else ''


def dispatch():
    r = subprocess.run([HERMES, 'kanban', 'dispatch', '--json'],
                       capture_output=True, text=True, timeout=120)
    return r.returncode == 0, (r.stdout or r.stderr or '').strip()[:200]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--agent', required=True)
    ap.add_argument('--title', required=True)
    ap.add_argument('--body', default='')
    ap.add_argument('--priority', type=int, default=0)
    ap.add_argument('--no-dispatch', action='store_true')
    args = ap.parse_args()

    agent = args.agent.strip().lower()
    if agent not in VALID_AGENTS:
        print(json.dumps({
            'ok': False,
            'error': f'unknown agent "{agent}"',
            'valid_agents': sorted(VALID_AGENTS),
            'message': f'I don\'t have an agent named "{agent}". Valid agents: {", ".join(sorted(VALID_AGENTS))}.'
        }))
        sys.exit(1)

    dept = AGENT_DEPARTMENTS.get(agent, 'Management')
    try:
        task_id = create_task(agent, args.title, args.body, args.priority)
    except Exception as e:
        print(json.dumps({'ok': False, 'error': str(e),
                          'message': f'Failed to create the task: {e}'}))
        sys.exit(1)

    dispatched = False
    dispatch_note = ''
    if not args.no_dispatch:
        dispatched, dispatch_note = dispatch()

    msg = (f'✅ Task created and assigned to {agent} ({dept} dept).\n'
           f'"{args.title}"\n'
           + (f'🚀 {agent} is working on it now.' if dispatched
              else '📋 Queued on the board; it will run on the next dispatch cycle.'))

    print(json.dumps({
        'ok': True, 'task_id': task_id, 'agent': agent, 'department': dept,
        'dispatched': dispatched, 'dispatch_note': dispatch_note, 'message': msg
    }))


if __name__ == '__main__':
    main()
