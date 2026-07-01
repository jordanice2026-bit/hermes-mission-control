#!/usr/bin/env python3
"""
command_worker.py — VPS-side agent control worker for Mission Control.

Runs on the VPS (where the agents/cron jobs live). Every tick it:
  1. Reads the current cron job state from ~/.hermes/cron (or HERMES cron dir)
  2. Polls Render's /api/agent-control/poll, pushing that state + any results
  3. Executes any queued commands via the `hermes cron` CLI
     (start_all/stop_all/pause_job/resume_job/run_job)
  4. Reports each command's result on the next poll

Designed to run as a Hermes cron job on a short interval (every 1 minute),
mirroring sync_kanban.py. It is idempotent and safe to run repeatedly.

Env:
  MISSION_CONTROL_URL    - https://hermes-mission-control.onrender.com
  MISSION_CONTROL_TOKEN  - the SYNC_TOKEN set in Render env
  HERMES_HOME            - defaults to ~/.hermes ; cron dir resolved from here
"""
import os
import re
import ast
import json
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

import sys as _sys
_sys.path.insert(0, '/opt/data')
_sys.path.insert(0, '/opt/data/mission-control')

HERMES_BIN = '/opt/hermes/.venv/bin/hermes'
DASHBOARD_URL = os.environ.get('MISSION_CONTROL_URL', '').rstrip('/')
SYNC_TOKEN = os.environ.get('MISSION_CONTROL_TOKEN', '')

# cron jobs.json — the source of truth for agent state
_CANDIDATES = [
    '/opt/data/cron/jobs.json',
    os.path.join(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')), 'cron', 'jobs.json'),
]


def _jobs_path():
    for p in _CANDIDATES:
        if os.path.isfile(p):
            return p
    return _CANDIDATES[0]


def load_jobs():
    """Return a compact list of jobs for the dashboard."""
    path = _jobs_path()
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        return [], f'could not read jobs.json: {e}'

    raw = data if isinstance(data, list) else data.get('jobs', data)
    if isinstance(raw, dict):
        raw = list(raw.values())

    jobs = []
    for j in (raw if isinstance(raw, list) else []):
        # enabled may be bool or the string "True"/"False"
        en = j.get('enabled', True)
        if isinstance(en, str):
            en = en.strip().lower() == 'true'
        sched = j.get('schedule_display') or ''
        if not sched:
            s = j.get('schedule')
            if isinstance(s, dict):
                sched = s.get('display', '')
            elif isinstance(s, str) and s.startswith('{'):
                try:
                    sched = ast.literal_eval(s).get('display', '')
                except Exception:
                    sched = s
        jobs.append({
            'id': j.get('id', ''),
            'name': j.get('name') or j.get('id', ''),
            'schedule': sched,
            'enabled': bool(en),
            'last_status': (j.get('last_status') or '').replace('None', '') or None,
            'last_run_at': (j.get('last_run_at') or '').replace('None', '') or None,
            'next_run_at': (j.get('next_run_at') or '').replace('None', '') or None,
        })
    return jobs, None


def system_status(jobs):
    if not jobs:
        return 'unknown'
    enabled = sum(1 for j in jobs if j['enabled'])
    if enabled == 0:
        return 'paused'
    if enabled == len(jobs):
        return 'running'
    return 'partial'


def run_cli(args):
    """Run `hermes cron <args>` and return (ok, output)."""
    try:
        r = subprocess.run([HERMES_BIN, 'cron'] + args,
                           capture_output=True, text=True, timeout=60)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out[:500]
    except Exception as e:
        return False, str(e)[:500]


def execute_command(cmd, jobs):
    """Execute one queued command. Returns a result dict."""
    action = cmd.get('action')
    job_id = cmd.get('job_id')
    cid = cmd.get('id')

    if action == 'pause_job' and job_id:
        ok, out = run_cli(['pause', job_id])
    elif action == 'resume_job' and job_id:
        ok, out = run_cli(['resume', job_id])
    elif action == 'run_job' and job_id:
        ok, out = run_cli(['run', job_id])
    elif action == 'stop_all':
        results = [run_cli(['pause', j['id']]) for j in jobs if j['enabled']]
        ok = all(r[0] for r in results) if results else True
        out = f'paused {len(results)} job(s)'
    elif action == 'start_all':
        results = [run_cli(['resume', j['id']]) for j in jobs if not j['enabled']]
        ok = all(r[0] for r in results) if results else True
        out = f'resumed {len(results)} job(s)'
    else:
        ok, out = False, f'unknown/invalid action: {action}'

    return {'id': cid, 'status': 'done' if ok else 'error', 'output': out}


def poll(jobs, status, results, chat_updates=None):
    """POST state + results to Render; return the full response dict
    ({'commands': [...], 'chat_messages': [...]})."""
    if not DASHBOARD_URL or not SYNC_TOKEN:
        print('ERROR: MISSION_CONTROL_URL / MISSION_CONTROL_TOKEN not set')
        return {'commands': [], 'chat_messages': []}
    # include shared team lessons so the dashboard can display the org brain
    team_lessons = []
    try:
        import agent_learning as _AL
        txt = _AL.read_team_lessons(max_chars=4000)
        for line in txt.splitlines():
            line = line.strip()
            if line.startswith('- ['):
                team_lessons.append(line[2:].strip())
    except Exception:
        pass
    payload = json.dumps({'jobs': jobs, 'system_status': status, 'results': results,
                          'team_lessons': team_lessons,
                          'chat_updates': chat_updates or []}).encode()
    req = urllib.request.Request(
        f'{DASHBOARD_URL}/api/agent-control/poll',
        data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {SYNC_TOKEN}'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
            return data
    except urllib.error.HTTPError as e:
        print(f'ERROR poll: HTTP {e.code} {e.read().decode()[:200]}')
    except Exception as e:
        print(f'ERROR poll: {e}')
    return {'commands': [], 'chat_messages': []}


def handle_chat_messages(chat_messages):
    """Process pending Manager chat messages -> classify + dispatch. Returns chat_updates."""
    if not chat_messages:
        return []
    try:
        import manager_chat_handler as MCH
    except Exception as e:
        return [{'id': m['id'], 'status': 'done',
                 'reply': f'(chat handler unavailable: {e})'} for m in chat_messages]
    updates = []
    for m in chat_messages:
        try:
            result = MCH.handle(m.get('text', ''))
            updates.append({'id': m['id'], 'status': 'done',
                            'reply': result.get('reply', 'Done.')})
            print(f"Chat handled: {m.get('text','')[:50]} -> {result.get('reply','')[:60]}")
        except Exception as e:
            updates.append({'id': m['id'], 'status': 'done',
                            'reply': f'Sorry, that failed: {str(e)[:200]}'})
    return updates


def handle_jarvis_control(jarvis_control):
    """Process Jarvis control markers (e.g. new_session -> reset the runner session)."""
    for marker in jarvis_control or []:
        if marker == 'new_session':
            try:
                if os.path.exists('/opt/data/jarvis_session.json'):
                    os.remove('/opt/data/jarvis_session.json')
                print('Jarvis session reset (new conversation)')
            except Exception as e:
                print(f'Jarvis session reset failed: {e}')


def launch_jarvis_messages(jarvis_messages):
    """Launch the Jarvis runner (full agentic session) as a DETACHED bg process per
    message. It can take minutes, so we don't block the worker tick; the runner
    posts its reply back to /api/jarvis/chat/reply when done."""
    if not jarvis_messages:
        return 0
    import subprocess as _sp
    launched = 0
    for m in jarvis_messages:
        try:
            _sp.Popen(
                ['python3', '/opt/data/jarvis_runner.py',
                 '--message-id', m['id'], '--text', m.get('text', '')],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                start_new_session=True, cwd='/opt/data', env=dict(os.environ),
            )
            launched += 1
            print(f"Jarvis runner launched for {m['id']}: {m.get('text','')[:50]}")
        except Exception as e:
            print(f"Jarvis launch failed for {m['id']}: {e}")
    return launched


def main():
    jobs, err = load_jobs()
    if err:
        print(err)
    status = system_status(jobs)

    # Apply any approved manager proposals (structural changes Jordan OK'd)
    try:
        import proposal_applier
        n = proposal_applier.apply_all_approved()
        if n:
            print(f'applied {n} approved proposal(s)')
            jobs, _ = load_jobs()   # state may have changed
            status = system_status(jobs)
    except Exception as e:
        print(f'proposal applier skipped: {e}')

    # First poll: push state, get queued commands + pending chat messages
    data = poll(jobs, status, [])
    commands = data.get('commands', [])
    chat_messages = data.get('chat_messages', [])
    jarvis_messages = data.get('jarvis_messages', [])
    jarvis_control = data.get('jarvis_control', [])

    # Handle Jarvis control markers first (e.g. reset session for a new conversation)
    handle_jarvis_control(jarvis_control)

    # Handle Manager chat messages (classify + dispatch tasks)
    chat_updates = handle_chat_messages(chat_messages)

    # Launch the Jarvis runner for any pending Jarvis messages (detached; replies async)
    jarvis_launched = launch_jarvis_messages(jarvis_messages)

    if not commands and not chat_updates and not jarvis_launched and not jarvis_control:
        print(f'OK: {len(jobs)} jobs, status={status}, no commands/chat/ea')
        return

    # Execute queued commands
    results = []
    for cmd in commands:
        res = execute_command(cmd, jobs)
        results.append(res)
        print(f"Executed {cmd.get('action')} {cmd.get('job_id') or ''} -> {res['status']}")

    # Re-read jobs (state changed) and report results + chat replies back
    jobs, _ = load_jobs()
    status = system_status(jobs)
    poll(jobs, status, results, chat_updates)
    print(f'OK: executed {len(results)} command(s), {len(chat_updates)} chat reply(ies), '
          f'{jarvis_launched} Jarvis launch(es), status now {status}')


if __name__ == '__main__':
    main()
