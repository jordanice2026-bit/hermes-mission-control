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


def poll(jobs, status, results):
    """POST state + results to Render; return the list of queued commands."""
    if not DASHBOARD_URL or not SYNC_TOKEN:
        print('ERROR: MISSION_CONTROL_URL / MISSION_CONTROL_TOKEN not set')
        return []
    payload = json.dumps({'jobs': jobs, 'system_status': status, 'results': results}).encode()
    req = urllib.request.Request(
        f'{DASHBOARD_URL}/api/agent-control/poll',
        data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {SYNC_TOKEN}'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
            return data.get('commands', [])
    except urllib.error.HTTPError as e:
        print(f'ERROR poll: HTTP {e.code} {e.read().decode()[:200]}')
    except Exception as e:
        print(f'ERROR poll: {e}')
    return []


def main():
    jobs, err = load_jobs()
    if err:
        print(err)
    status = system_status(jobs)

    # First poll: push state, get queued commands (report no results yet)
    commands = poll(jobs, status, [])

    if not commands:
        print(f'OK: {len(jobs)} jobs, status={status}, no commands')
        return

    # Execute queued commands
    results = []
    for cmd in commands:
        res = execute_command(cmd, jobs)
        results.append(res)
        print(f"Executed {cmd.get('action')} {cmd.get('job_id') or ''} -> {res['status']}")

    # Re-read jobs (state changed) and report results back
    jobs, _ = load_jobs()
    status = system_status(jobs)
    poll(jobs, status, results)
    print(f'OK: executed {len(results)} command(s), status now {status}')


if __name__ == '__main__':
    main()
