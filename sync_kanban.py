#!/usr/bin/env python3
"""
Hermes Mission Control — Kanban Sync Script
Reads from the local kanban.db and pushes tasks to the dashboard.
Designed to run as a Hermes cron job or standalone.

Usage:
  python sync_kanban.py

Required environment variables:
  MISSION_CONTROL_URL   - e.g. https://hermes-mission-control.onrender.com
  MISSION_CONTROL_TOKEN - the SYNC_TOKEN set in Render env vars
"""

import os
import json
import sqlite3
import urllib.request
import urllib.error
from pathlib import Path

HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
KANBAN_DB = os.path.join(HERMES_HOME, "kanban.db")
DASHBOARD_URL = os.environ.get("MISSION_CONTROL_URL", "").rstrip("/")
SYNC_TOKEN = os.environ.get("MISSION_CONTROL_TOKEN", "")


def load_tasks():
    conn = sqlite3.connect(KANBAN_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT
            id, title, body, assignee, status, priority,
            created_by, created_at, started_at, completed_at,
            tenant, result, consecutive_failures, worker_pid,
            last_failure_error, last_heartbeat_at, session_id,
            skills, current_step_key
        FROM tasks
        ORDER BY created_at DESC
        LIMIT 500
    """)
    rows = cur.fetchall()
    conn.close()
    tasks = []
    for r in rows:
        t = dict(r)
        # Remove large/sensitive fields we don't need in the dashboard
        tasks.append(t)
    return tasks


def push_tasks(tasks):
    if not DASHBOARD_URL:
        print("ERROR: MISSION_CONTROL_URL not set")
        return False
    if not SYNC_TOKEN:
        print("ERROR: MISSION_CONTROL_TOKEN not set")
        return False

    payload = json.dumps({"tasks": tasks}).encode("utf-8")
    req = urllib.request.Request(
        f"{DASHBOARD_URL}/api/sync",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SYNC_TOKEN}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            print(f"OK: {body['total']} tasks synced, {body['changed']} changed")
            return True
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}")
        return False
    except Exception as e:
        print(f"ERROR: {e}")
        return False


if __name__ == "__main__":
    tasks = load_tasks()
    print(f"Loaded {len(tasks)} tasks from kanban.db")
    push_tasks(tasks)
