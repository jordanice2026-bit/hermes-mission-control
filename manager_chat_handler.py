#!/usr/bin/env python3
"""
manager_chat_handler.py — process a Manager chat message from the dashboard.

The dashboard (Render) has no LLM key, so the VPS worker calls this for each
pending chat message. It:
  1. Classifies the message: is it a task/project request, or a question?
  2. If a task: picks the best-fit agent, creates + dispatches a kanban task
     via manager_dispatch.py, returns a confirmation reply.
  3. If a question about the operation: answers briefly using `hermes -z`.

Returns a dict: {"reply": "<text>", "status": "done"}

Usage (called programmatically by command_worker, or standalone):
    python3 manager_chat_handler.py "get comps for 400 W 8th St Anderson"
"""
import sys
import json
import subprocess

sys.path.insert(0, '/opt/data')
sys.path.insert(0, '/opt/data/mission-control')

HERMES = '/opt/hermes/.venv/bin/hermes'
DISPATCH = '/opt/data/manager_dispatch.py'

try:
    import agent_learning as AL
    AGENT_DEPARTMENTS = {k: v for k, v in AL.AGENT_DEPARTMENTS.items()
                         if k not in ('manager', 'scout')}
except Exception:
    AGENT_DEPARTMENTS = {
        'property-sourcer': 'Sourcing', 'owner-researcher': 'Sourcing', 'buyer-sourcer': 'Sourcing',
        'underwriter': 'Underwriting', 'deal-screener': 'Underwriting',
        'matchmaker': 'Matchmaking', 'investor-profiler': 'Matchmaking',
        'prospector': 'Outreach', 'lead-agent': 'Outreach', 'marketing-agent': 'Outreach', 'client-agent': 'Outreach',
        'inbox-monitor': 'Transaction Coordination',
        'research-agent': 'Management',
    }

AGENT_LIST = ", ".join(sorted(AGENT_DEPARTMENTS.keys()))

CLASSIFY_PROMPT = """You are the Manager (COO) of a solo real-estate investment brokerage in Indiana. \
The broker (Jordan) sent you this message from the Mission Control dashboard:

"{msg}"

Decide how to handle it. You manage these worker agents (assign to exactly one):
- property-sourcer, owner-researcher, buyer-sourcer (Sourcing: find Indiana properties, owner info, buyers)
- underwriter, deal-screener (Underwriting: financial models, screening)
- matchmaker, investor-profiler (Matchmaking: match deals to buyers, investor intake)
- prospector, lead-agent, marketing-agent, client-agent (Outreach: owner/prospect outreach, listing copy, client comms)
- inbox-monitor (Transaction Coordination: deal/deadline monitoring)
- research-agent (Management: market/neighborhood research, comps, general research)

Respond with ONLY a JSON object, no other text:
- If it's an actionable task/project to assign: {{"action":"task","agent":"<one agent>","title":"<short action title>","body":"<full details incl any address, Indiana-scoped>"}}
- If it's just a question to answer directly: {{"action":"answer","reply":"<concise answer>"}}
- If unclear: {{"action":"clarify","reply":"<one short clarifying question>"}}

Comps/market research -> research-agent. Listing copy/marketing -> marketing-agent. \
Owner lookups -> owner-researcher. Financial analysis -> underwriter. Keep tasks Indiana-only."""


def _run(cmd, timeout=120):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or ''), (r.stderr or '')


def classify(msg: str) -> dict:
    """Ask the LLM to classify + spec the message. Returns parsed JSON or fallback."""
    prompt = CLASSIFY_PROMPT.format(msg=msg.replace('"', "'"))
    rc, out, err = _run([HERMES, '-z', prompt], timeout=120)
    text = (out or '').strip()
    # extract JSON blob
    import re
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {"action": "answer",
            "reply": "I couldn't parse that — could you rephrase the task? "
                     "(e.g. 'get comps for 123 Main St, Muncie')"}


def handle(msg: str) -> dict:
    decision = classify(msg)
    action = decision.get("action")

    if action == "task":
        agent = (decision.get("agent") or "").strip().lower()
        title = (decision.get("title") or msg)[:200]
        bodytext = decision.get("body") or msg
        if agent not in AGENT_DEPARTMENTS:
            return {"reply": f"I wasn't sure which agent should handle that. "
                             f"Available: {AGENT_LIST}. Try naming the work more specifically.",
                    "status": "done"}
        rc, out, err = _run([sys.executable if False else 'python3', DISPATCH,
                             '--agent', agent, '--title', title, '--body', bodytext], timeout=150)
        try:
            result = json.loads((out or '').strip().splitlines()[-1])
            reply = result.get("message") or "Task created."
        except Exception:
            reply = (out or err or "Task dispatch attempted.").strip()[:500]
        return {"reply": reply, "status": "done"}

    # answer or clarify
    return {"reply": decision.get("reply", "OK."), "status": "done"}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"reply": "no message", "status": "done"}))
        return
    msg = sys.argv[1]
    print(json.dumps(handle(msg)))


if __name__ == '__main__':
    main()
