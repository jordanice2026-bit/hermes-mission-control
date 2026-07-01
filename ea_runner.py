#!/usr/bin/env python3
"""
ea_runner.py — Executive Assistant ("Jarvis") runner.

Runs a FULL agentic Hermes session using the `default` profile — the same
agent (Claude Opus, all tools, all skills, terminal, full infra access) that
built this whole system. Full autonomy (--yolo): executes anything immediately.

Invoked by command_worker.py for each pending EA chat message. It:
  1. Loads the persistent EA session id (for Jarvis-style memory across messages).
  2. Runs `hermes chat -q <message>` (resume if we have a session, else fresh).
  3. Persists the (possibly new) session id.
  4. POSTs the assistant's reply back to the dashboard.

Usage:
    python3 ea_runner.py --message-id <id> --text "<user message>"
"""
import os
import re
import sys
import json
import time
import argparse
import subprocess
import urllib.request

HERMES = '/opt/hermes/.venv/bin/hermes'
SESSION_FILE = '/opt/data/ea_session.json'
EA_SOUL = '/opt/data/ea_soul.md'
DASHBOARD_URL = os.environ.get('MISSION_CONTROL_URL', 'https://hermes-mission-control.onrender.com').rstrip('/')
SYNC_TOKEN = os.environ.get('MISSION_CONTROL_TOKEN', '') or os.environ.get('SYNC_TOKEN', '')
MAX_TURNS = 40
TIMEOUT = 600  # 10 min hard cap per message


def load_session_id():
    try:
        return json.load(open(SESSION_FILE)).get('session_id', '')
    except Exception:
        return ''


def save_session_id(sid):
    try:
        json.dump({'session_id': sid, 'updated_at': int(time.time())}, open(SESSION_FILE, 'w'))
    except Exception as e:
        sys.stderr.write(f'save_session_id failed: {e}\n')


def load_soul():
    try:
        return open(EA_SOUL).read()
    except Exception:
        return ''


def run_agent(text: str) -> tuple[str, str]:
    """Run one agentic turn. Returns (reply_text, session_id)."""
    sid = load_session_id()
    # Prepend the persona on the FIRST message only (fresh session)
    prompt = text
    if not sid:
        soul = load_soul()
        if soul:
            prompt = soul + "\n\n---\n\nJordan's message:\n" + text

    cmd = [HERMES, 'chat', '-q', prompt, '--yolo', '-Q', '--max-turns', str(MAX_TURNS)]
    if sid:
        cmd += ['--resume', sid]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd='/opt/data')
        out = proc.stdout or ''
        err = proc.stderr or ''
    except subprocess.TimeoutExpired:
        return ("I ran out of time on that one (10-min cap). Try breaking it into smaller steps.", sid)
    except Exception as e:
        return (f"Runner error: {e}", sid)

    # Extract session id from trailing "session_id: XXX" (emitted on stderr in -Q mode)
    new_sid = sid
    m = re.search(r'session_id:\s*(\S+)', err) or re.search(r'session_id:\s*(\S+)', out)
    if m:
        new_sid = m.group(1)
        save_session_id(new_sid)

    # The reply is clean on stdout in -Q mode; strip any stray session/banner lines
    reply_lines = []
    for line in out.splitlines():
        if re.match(r'\s*session_id:\s*\S+', line):
            continue
        if line.startswith('↻ Resumed session'):
            continue
        reply_lines.append(line)
    reply = '\n'.join(reply_lines).strip()
    if not reply:
        reply = (err.strip()[:500] or "Done.")
    return (reply, new_sid)


def post_reply(message_id: str, reply: str, status: str = 'done', hermes_sid: str = ''):
    if not SYNC_TOKEN:
        sys.stderr.write('no sync token; cannot post reply\n')
        print(reply)
        return
    payload = json.dumps({'chat_updates': [{'id': message_id, 'status': status, 'reply': reply}],
                          'hermes_sid': hermes_sid,
                          'jobs': [], 'system_status': 'ea', 'results': [],
                          'ea': True}).encode()
    req = urllib.request.Request(
        f'{DASHBOARD_URL}/api/ea/chat/reply',
        data=payload,
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {SYNC_TOKEN}'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status in (200, 201)
    except Exception as e:
        sys.stderr.write(f'post_reply failed: {e}\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--message-id', required=True)
    ap.add_argument('--text', required=True)
    args = ap.parse_args()

    reply, sid = run_agent(args.text)
    post_reply(args.message_id, reply, hermes_sid=sid)
    print(f'[ea] replied to {args.message_id} (session {sid}): {reply[:80]}')


if __name__ == '__main__':
    main()
