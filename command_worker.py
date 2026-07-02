#!/usr/bin/env python3
"""
command_worker.py — VPS-side agent control worker for Mission Control.

WATCHDOG ENTRY POINT: this file's main() no longer does the polling work
itself. It only checks whether the persistent daemon (command_worker_daemon.py)
is alive and starts it if not, then exits immediately. The Hermes cron job
("agent-control-worker") still fires this every 1 minute as a cheap safety
net — restarting the daemon within ~60s if it ever crashes or the container
restarted — but the ACTUAL polling that matters for latency (noticing a
pending Jarvis/chat message and launching a runner for it) now happens
continuously inside the daemon on a ~2s interval, not gated by the cron
tick. See command_worker_daemon.py for the real loop.

Why this exists: a voice/chat message sitting "pending" used to wait up to
60s (the cron tick interval) before ANYTHING noticed it and launched
jarvis_runner.py — on top of the runner's own (now ~2-6s) reply time. That
compounded into 30-60+ second end-to-end waits that had nothing to do with
model speed. The daemon removes that 60s ceiling; typical worst case is now
~2s (the daemon's own poll interval) instead of ~60s.

This module still exports all the shared logic (load_jobs, poll,
execute_command, handle_chat_messages, launch_jarvis_messages,
handle_jarvis_control, system_status) — command_worker_daemon.py imports
and reuses these directly so there is exactly one implementation of each,
not two copies that can drift.

Env:
  MISSION_CONTROL_URL    - https://hermes-mission-control.onrender.com
  MISSION_CONTROL_TOKEN  - the SYNC_TOKEN set in Render env
  HERMES_HOME            - defaults to ~/.hermes ; cron dir resolved from here
"""
import os
import re
import ast
import json
import signal
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

DAEMON_SCRIPT = '/opt/data/mission-control/command_worker_daemon.py'
DAEMON_PIDFILE = '/opt/data/command_worker_daemon.pid'
DAEMON_LOG = '/opt/data/command_worker_daemon.log'

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
                ['python3', '/opt/data/mission-control/jarvis_runner.py',
                 '--message-id', m['id'], '--text', m.get('text', '')],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                start_new_session=True, cwd='/opt/data', env=dict(os.environ),
            )
            launched += 1
            print(f"Jarvis runner launched for {m['id']}: {m.get('text','')[:50]}")
        except Exception as e:
            print(f"Jarvis launch failed for {m['id']}: {e}")
    return launched


def run_light_tick():
    """Cheap, latency-critical tick: poll + dispatch pending Jarvis/chat
    messages ONLY. No proposal_applier (hits the Notion API), no job-state
    re-read from disk beyond what's needed for the poll payload. This is
    what the daemon calls every ~2s — it must stay cheap enough to run that
    often without hammering any external API.

    Command execution (pause/resume/run_job/etc.) and jarvis_control
    markers are still handled here since they're purely local/instant
    (subprocess calls to `hermes cron`, or a file delete) — only the
    Notion-backed proposal sync is deferred to the slower full tick.
    """
    jobs, err = load_jobs()
    if err:
        print(err)
    status = system_status(jobs)

    data = poll(jobs, status, [])
    commands = data.get('commands', [])
    chat_messages = data.get('chat_messages', [])
    jarvis_messages = data.get('jarvis_messages', [])
    jarvis_control = data.get('jarvis_control', [])

    handle_jarvis_control(jarvis_control)
    chat_updates = handle_chat_messages(chat_messages)
    jarvis_launched = launch_jarvis_messages(jarvis_messages)

    if not commands and not chat_updates and not jarvis_launched and not jarvis_control:
        return

    results = []
    for cmd in commands:
        res = execute_command(cmd, jobs)
        results.append(res)
        print(f"Executed {cmd.get('action')} {cmd.get('job_id') or ''} -> {res['status']}")

    jobs, _ = load_jobs()
    status = system_status(jobs)
    poll(jobs, status, results, chat_updates)
    print(f'OK: executed {len(results)} command(s), {len(chat_updates)} chat reply(ies), '
          f'{jarvis_launched} Jarvis launch(es), status now {status}')


def run_full_tick():
    """Heavier periodic tick: everything run_light_tick does, PLUS syncing
    approved manager proposals (hits the Notion API — must NOT run on the
    daemon's ~2s cadence). Called by the daemon every FULL_TICK_EVERY_N
    light ticks instead of every tick."""
    try:
        import proposal_applier
        n = proposal_applier.apply_all_approved()
        if n:
            print(f'applied {n} approved proposal(s)')
    except Exception as e:
        print(f'proposal applier skipped: {e}')
    run_light_tick()


def run_one_tick():
    """Backward-compatible alias for the full tick — kept so any external
    caller (manual debugging, older references) still gets the complete
    behavior this module used to run on every invocation."""
    run_full_tick()


def _daemon_pid_alive():
    """Return True if the pidfile points at a live command_worker_daemon.py process."""
    try:
        with open(DAEMON_PIDFILE) as f:
            pid = int(f.read().strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)   # signal 0: existence check only, doesn't actually kill
    except OSError:
        return False
    # Confirm it's actually our daemon and not a recycled PID reused by something else
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            cmdline = f.read().decode(errors='replace')
        return 'command_worker_daemon.py' in cmdline
    except Exception:
        # /proc not available (non-Linux) — fall back to trusting the pidfile
        return True


def _start_daemon():
    """Spawn the persistent polling daemon, detached, writing its own pidfile."""
    try:
        subprocess.Popen(
            ['python3', DAEMON_SCRIPT],
            stdout=open(DAEMON_LOG, 'a'),
            stderr=subprocess.STDOUT,
            start_new_session=True, cwd='/opt/data', env=dict(os.environ),
        )
        print('Started command_worker_daemon.py')
    except Exception as e:
        print(f'Failed to start command_worker_daemon.py: {e}')


def main():
    """Watchdog entry point (invoked by the 'agent-control-worker' cron job
    every 1 minute): ensure the persistent low-latency daemon is running,
    starting it if it died or was never started. Does NOT do the poll/
    dispatch work itself — see run_one_tick() / command_worker_daemon.py."""
    if _daemon_pid_alive():
        print('OK: command_worker_daemon.py already running')
        return
    print('command_worker_daemon.py not running — starting it')
    _start_daemon()



if __name__ == '__main__':
    main()
