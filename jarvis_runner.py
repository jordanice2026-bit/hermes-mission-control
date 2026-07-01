#!/usr/bin/env python3
"""
jarvis_runner.py — Executive Assistant ("Jarvis") runner.

Runs a FULL agentic Hermes session using the `default` profile — the same
agent (all tools, all skills, terminal, full infra access) that built this
whole system. Full autonomy (--yolo): executes anything immediately.

Model routing: before running the real turn, a cheap direct Anthropic API
call (bypassing the `hermes` CLI's ~8-10s fixed startup cost entirely, so
this adds well under a second) classifies the message as FAST (casual chat,
general knowledge, quick math — no need to touch real systems) or COMPLEX
(needs tools: checking data, running commands, multi-step work). FAST
messages run the actual hermes turn on a small/quick model
(claude-haiku-4-5); COMPLEX messages use the configured default model
(currently claude-sonnet-5, picked up automatically by omitting -m). Session
continuity is unaffected — the model can change turn-to-turn within one
resumed session.

Invoked by command_worker.py for each pending Jarvis chat message. It:
  1. Loads the persistent Jarvis session id (for Jarvis-style memory across messages).
  2. Classifies the message's complexity (direct API call, ~1s).
  3. Runs `hermes chat -q <message>` with the routed model (resume if we have
     a session, else fresh).
  4. Persists the (possibly new) session id.
  5. POSTs the assistant's reply back to the dashboard — WITH RETRIES, so a
     transient network blip can never leave a message stuck at "thinking"
     forever with no trace of what happened.

Usage:
    python3 jarvis_runner.py --message-id <id> --text "<user message>"
"""
import os
import re
import sys
import json
import time
import argparse
import subprocess
import urllib.request
import urllib.error

HERMES = '/opt/hermes/.venv/bin/hermes'
SESSION_FILE = '/opt/data/jarvis_session.json'
EA_SOUL = '/opt/data/jarvis_soul.md'
DASHBOARD_URL = os.environ.get('MISSION_CONTROL_URL', 'https://hermes-mission-control.onrender.com').rstrip('/')
SYNC_TOKEN = os.environ.get('MISSION_CONTROL_TOKEN', '') or os.environ.get('SYNC_TOKEN', '')
MAX_TURNS = 40
TIMEOUT = 600  # 10 min hard cap per message

ERROR_LOG = '/opt/data/jarvis_runner_errors.log'   # persistent — command_worker.py DEVNULLs stdout/stderr

POST_RETRIES = 5
POST_RETRY_BACKOFF = [2, 5, 10, 20, 30]  # seconds between attempts

# ── Model routing ────────────────────────────────────────────────────────────
FAST_MODEL = 'claude-haiku-4-5-20251001'   # small/quick — casual chat, general knowledge
# COMPLEX messages fall through to the hermes config default (claude-sonnet-5)
# by omitting -m entirely, so any future default-model change is picked up
# automatically without touching this file.
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
if not ANTHROPIC_API_KEY:
    # command_worker.py may spawn this process via cron with a stripped-down
    # env that doesn't include everything from /opt/data/.env — read it
    # directly as a fallback rather than assume the parent process exported it.
    try:
        with open('/opt/data/.env') as _f:
            for _line in _f:
                _line = _line.strip()
                if _line.startswith('ANTHROPIC_API_KEY='):
                    ANTHROPIC_API_KEY = _line.split('=', 1)[1].strip().strip('"').strip("'")
                    break
    except Exception:
        pass
CLASSIFY_TIMEOUT = 8  # seconds — generous; a real call takes ~1s
CLASSIFY_PROMPT = (
    "You are a fast triage layer in front of Jarvis, an executive assistant "
    "with full access to real systems (databases, servers, code, deployments, "
    "live data).\n\n"
    "Decide if the user's message is FAST or COMPLEX.\n"
    "FAST = casual chat, greetings, small talk, opinions, general knowledge, "
    "quick math, brainstorming, or anything fully answerable from general "
    "knowledge with no need to check or change anything real.\n"
    "COMPLEX = anything requiring checking current/live data, running "
    "commands, querying a database, reading/editing files, deploying, or any "
    "multi-step real work.\n"
    "When in doubt, answer COMPLEX — a wrong FAST guess means a confidently "
    "wrong answer about real data, which is worse than a slightly slower "
    "reply.\n\n"
    "Reply with EXACTLY one word, FAST or COMPLEX, and nothing else.\n\n"
    "User message: {text}"
)


def classify_complexity(text: str) -> str:
    """Classify a message as 'fast' or 'complex' via a direct Anthropic API
    call — bypasses the hermes CLI's fixed startup cost so this adds well
    under a second. Defaults to 'complex' (the original, safe, unrouted
    behavior) on any failure, missing key, or ambiguous response."""
    if not ANTHROPIC_API_KEY:
        return 'complex'
    try:
        payload = json.dumps({
            'model': FAST_MODEL,
            'max_tokens': 6,
            'temperature': 0,
            'messages': [{'role': 'user', 'content': CLASSIFY_PROMPT.format(text=text[:2000])}],
        }).encode()
        req = urllib.request.Request(
            'https://api.anthropic.com/v1/messages',
            data=payload,
            headers={
                'Content-Type': 'application/json',
                'x-api-key': ANTHROPIC_API_KEY,
                'anthropic-version': '2023-06-01',
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=CLASSIFY_TIMEOUT) as r:
            data = json.loads(r.read())
            answer = ''.join(
                block.get('text', '') for block in data.get('content', [])
                if block.get('type') == 'text'
            ).strip().upper()
            if answer.startswith('FAST'):
                return 'fast'
            return 'complex'
    except Exception as e:
        log_error(f'classify_complexity failed (defaulting to complex): {e!r}')
        return 'complex'


def log_error(msg: str):
    """Append to a persistent error log — command_worker.py DEVNULLs this
    process's stdout/stderr, so anything not logged here vanishes silently."""
    try:
        with open(ERROR_LOG, 'a') as f:
            f.write(f'[{time.strftime("%Y-%m-%d %H:%M:%S")}] {msg}\n')
    except Exception:
        pass


def load_session_id():
    try:
        return json.load(open(SESSION_FILE)).get('session_id', '')
    except Exception:
        return ''


def save_session_id(sid):
    try:
        json.dump({'session_id': sid, 'updated_at': int(time.time())}, open(SESSION_FILE, 'w'))
    except Exception as e:
        log_error(f'save_session_id failed: {e}')


def load_soul():
    try:
        return open(EA_SOUL).read()
    except Exception:
        return ''


def run_agent(text: str) -> tuple[str, str]:
    """Run one agentic turn. Returns (reply_text, session_id). Never raises —
    every failure path returns a user-visible reply string instead."""
    sid = load_session_id()
    # Prepend the persona on the FIRST message only (fresh session)
    prompt = text
    if not sid:
        soul = load_soul()
        if soul:
            prompt = soul + "\n\n---\n\nJordan's message:\n" + text

    route = classify_complexity(text)
    cmd = [HERMES, 'chat', '-q', prompt, '--yolo', '-Q', '--max-turns', str(MAX_TURNS)]
    if route == 'fast':
        cmd += ['-m', FAST_MODEL, '--provider', 'anthropic']
    # 'complex' omits -m entirely, so it always uses whatever the hermes
    # config default model is (currently claude-sonnet-5) without this file
    # needing to know or track that value.
    if sid:
        cmd += ['--resume', sid]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd='/opt/data')
        out = proc.stdout or ''
        err = proc.stderr or ''
    except subprocess.TimeoutExpired:
        log_error(f'TIMEOUT after {TIMEOUT}s for message ({route}): {text[:100]}')
        return ("I ran out of time on that one (10-min cap). Try breaking it into smaller steps.", sid)
    except Exception as e:
        log_error(f'subprocess.run failed ({route}): {e!r} for message: {text[:100]}')
        return (f"Runner error: {e}", sid)

    # Extract session id from trailing "session_id: XXX" (emitted on stderr in -Q mode)
    new_sid = sid
    try:
        m = re.search(r'session_id:\s*(\S+)', err) or re.search(r'session_id:\s*(\S+)', out)
        if m:
            new_sid = m.group(1)
            save_session_id(new_sid)
    except Exception as e:
        log_error(f'session_id extraction failed: {e!r}')

    # The reply is clean on stdout in -Q mode; strip any stray session/banner lines
    try:
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
    except Exception as e:
        log_error(f'reply extraction failed: {e!r}; raw stdout len={len(out)}')
        reply = err.strip()[:500] or out.strip()[:500] or "Done (reply parsing failed — check logs)."

    return (reply, new_sid)


def post_reply(message_id: str, reply: str, status: str = 'done', hermes_sid: str = '') -> bool:
    """POST the reply back to the dashboard. Retries with backoff on any
    failure (network blip, cold-start, DNS hiccup, 5xx) so a transient issue
    can never silently strand a message in 'processing' forever."""
    if not SYNC_TOKEN:
        log_error(f'no sync token; cannot post reply for {message_id}. Reply was: {reply[:200]}')
        print(reply)
        return False

    payload = json.dumps({'chat_updates': [{'id': message_id, 'status': status, 'reply': reply}],
                          'hermes_sid': hermes_sid,
                          'jobs': [], 'system_status': 'ea', 'results': [],
                          'jarvis': True}).encode()

    last_err = None
    for attempt in range(1, POST_RETRIES + 1):
        try:
            req = urllib.request.Request(
                f'{DASHBOARD_URL}/api/jarvis/chat/reply',
                data=payload,
                headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {SYNC_TOKEN}'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status in (200, 201):
                    if attempt > 1:
                        log_error(f'post_reply succeeded for {message_id} on attempt {attempt}')
                    return True
                last_err = f'HTTP {r.status}'
        except Exception as e:
            last_err = repr(e)
            log_error(f'post_reply attempt {attempt}/{POST_RETRIES} failed for {message_id}: {last_err}')

        if attempt < POST_RETRIES:
            time.sleep(POST_RETRY_BACKOFF[min(attempt - 1, len(POST_RETRY_BACKOFF) - 1)])

    # All retries exhausted — this is the one case a reply can still be lost.
    # Persist it to disk so it's at minimum recoverable/inspectable, and log
    # loudly so it shows up if anyone checks the error log.
    log_error(f'POST_REPLY GAVE UP after {POST_RETRIES} attempts for {message_id}. '
              f'Last error: {last_err}. Reply was: {reply[:500]}')
    try:
        with open('/opt/data/jarvis_lost_replies.jsonl', 'a') as f:
            f.write(json.dumps({'message_id': message_id, 'reply': reply, 'sid': hermes_sid,
                                 'ts': int(time.time())}) + '\n')
    except Exception:
        pass
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--message-id', required=True)
    ap.add_argument('--text', required=True)
    args = ap.parse_args()

    # Top-level safety net: NO exception here can ever leave the frontend
    # stuck showing "thinking" forever with zero trace of what happened.
    try:
        reply, sid = run_agent(args.text)
    except Exception as e:
        log_error(f'UNCAUGHT exception in run_agent for {args.message_id}: {e!r}')
        reply, sid = (f"Something went wrong on my end and I couldn't finish that "
                      f"(internal error: {e}). Please try asking again.", load_session_id())

    ok = post_reply(args.message_id, reply, hermes_sid=sid)
    status_word = 'delivered' if ok else 'FAILED TO DELIVER (see jarvis_runner_errors.log)'
    print(f'[ea] {status_word} reply for {args.message_id} (session {sid}): {reply[:80]}')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        # Absolute last resort — even argparse or an import-time failure
        # shouldn't be able to hide silently.
        log_error(f'FATAL uncaught exception in main(): {e!r}')
        sys.exit(1)
