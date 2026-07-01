#!/usr/bin/env python3
"""
agent_run_wrapper.py — wraps an existing agent stage so every run is:
  1. Prefixed with the agent's accumulated LESSONS (lean self-correction context)
  2. Executed
  3. Logged to the Notion Agent Runs ledger (success/partial/failure + summary)
  4. On failure, a terse lesson is auto-appended for next time

Usage:
    python3 agent_run_wrapper.py <agent_key> <department> -- <command...>

Example (drop-in for notion_run.sh stages):
    python3 agent_run_wrapper.py property-sourcer Sourcing -- \
        bash /opt/data/investment-pipeline/notion/notion_run.sh scout

Keeps the original agent untouched; just brackets it with learning + logging.
"""
import os
import sys
import time
import subprocess

sys.path.insert(0, '/opt/data')
sys.path.insert(0, '/opt/data/mission-control')
import agent_learning as AL


def classify(returncode: int, output: str) -> str:
    low = output.lower()
    if returncode != 0:
        return 'failure'
    # Heuristics for partial success
    if any(w in low for w in ('error', 'failed', 'exception', 'traceback', 'could not', 'no results', '0 added', 'skipped all')):
        # zero-work or soft errors -> partial
        return 'partial'
    return 'success'


def extract_items(output: str) -> int:
    import re
    for pat in (r'added\s+(\d+)', r'(\d+)\s+leads', r'(\d+)\s+card', r'processed\s+(\d+)', r'(\d+)\s+propert'):
        m = re.search(pat, output, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return 0


def main():
    if '--' not in sys.argv:
        print('usage: agent_run_wrapper.py <agent> <department> -- <command...>')
        sys.exit(2)
    split = sys.argv.index('--')
    head = sys.argv[1:split]
    cmd = sys.argv[split + 1:]
    if len(head) < 1 or not cmd:
        print('usage: agent_run_wrapper.py <agent> <department> -- <command...>')
        sys.exit(2)
    agent = head[0]
    department = head[1] if len(head) > 1 else AL.department_for(agent)

    # 1. Inject lessons into the child's environment (agents can read AGENT_LESSONS)
    lessons = AL.read_lessons(agent)
    team = AL.read_team_lessons()
    env = os.environ.copy()
    combined = ''
    if team:
        combined += 'SHARED TEAM LESSONS (apply to all agents):\n' + team + '\n\n'
    if lessons:
        combined += f'YOUR LESSONS ({agent}):\n' + lessons
    if combined:
        env['AGENT_LESSONS'] = combined
        print(f"[learning] injected {len(combined)} chars of lessons "
              f"({'team+own' if team and lessons else 'team' if team else 'own'}) for {agent}")

    # 2. Run the agent
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1800)
        output = (proc.stdout or '') + '\n' + (proc.stderr or '')
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        output = 'TIMEOUT after 1800s'
        rc = 124
    except Exception as e:
        output = f'wrapper error: {e}'
        rc = 1
    dur = time.time() - t0

    # Echo child output so cron delivery still shows it
    print(output[-4000:])

    # 3. Classify + log the run
    status = classify(rc, output)
    items = extract_items(output)
    summary = output.strip().splitlines()[-1][:300] if output.strip() else ''
    err_text = ''
    lesson_logged = False

    if status in ('failure', 'partial'):
        # last non-empty error-ish line
        for line in reversed(output.splitlines()):
            if any(w in line.lower() for w in ('error', 'failed', 'exception', 'traceback', 'timeout')):
                err_text = line.strip()[:300]
                break
        # 4. Auto-append a lesson on hard failure (so it self-corrects next run)
        if status == 'failure' and err_text:
            AL.log_lesson(agent, f"Run failed: {err_text}", context=f"rc={rc}")
            lesson_logged = True

    AL.log_run(agent, status, summary=summary, error=err_text,
               items=items, duration=dur, department=department, lesson_logged=lesson_logged)
    print(f"[learning] logged run: {agent} [{status}] items={items} dur={dur:.0f}s"
          + (" +lesson" if lesson_logged else ""))

    sys.exit(rc)


if __name__ == '__main__':
    main()
