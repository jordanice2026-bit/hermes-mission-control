#!/usr/bin/env python3
"""
command_worker_daemon.py — persistent low-latency poll loop for Mission
Control's VPS-side agent control worker.

This is the actual "notice a pending Jarvis/chat message fast" loop.
Previously this work only happened inside the Hermes cron job
("agent-control-worker", every 1 minute) — so a message could sit
"pending" on the dashboard for up to 60 seconds before ANYTHING even
launched jarvis_runner.py for it, regardless of how fast the reply itself
was to generate. That compounded into 30-60+ second end-to-end waits that
had nothing to do with model/API speed.

This daemon polls every POLL_INTERVAL_SECONDS (~2s) in a tight loop instead,
so the worst-case "time until a runner is launched" drops from ~60s to
~2s. command_worker.py's cron-triggered main() now only checks whether
this daemon is alive and restarts it if not (watchdog pattern) — it no
longer does the poll/dispatch work itself.

Writes its PID to DAEMON_PIDFILE on startup so the watchdog can check
liveness, and removes it on clean exit (SIGTERM/SIGINT).

Run manually for debugging:
    python3 command_worker_daemon.py
"""
import os
import sys
import time
import signal

sys.path.insert(0, '/opt/data')
sys.path.insert(0, '/opt/data/mission-control')

import command_worker as CW  # reuse all the shared tick logic — one implementation

POLL_INTERVAL_SECONDS = 2
FULL_TICK_EVERY_SECONDS = 15   # proposal_applier (Notion API) cadence — must stay coarse
PIDFILE = CW.DAEMON_PIDFILE

_stop = False


def _handle_signal(signum, frame):
    global _stop
    _stop = True


def _write_pidfile():
    try:
        with open(PIDFILE, 'w') as f:
            f.write(str(os.getpid()))
    except Exception as e:
        print(f'Failed to write pidfile: {e}', flush=True)


def _remove_pidfile():
    try:
        if os.path.isfile(PIDFILE):
            os.remove(PIDFILE)
    except Exception:
        pass


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    _write_pidfile()
    print(f'command_worker_daemon started (pid={os.getpid()}, poll every {POLL_INTERVAL_SECONDS}s)', flush=True)

    try:
        last_full_tick = 0.0
        while not _stop:
            tick_start = time.time()
            try:
                if tick_start - last_full_tick >= FULL_TICK_EVERY_SECONDS:
                    CW.run_full_tick()
                    last_full_tick = tick_start
                else:
                    CW.run_light_tick()
            except Exception as e:
                # A single bad tick (transient network blip, etc.) must never
                # kill the daemon — that would silently regress back to the
                # 60s cron-only cadence until the next watchdog check.
                print(f'ERROR: tick failed (continuing): {e!r}', flush=True)

            elapsed = time.time() - tick_start
            sleep_for = max(0.0, POLL_INTERVAL_SECONDS - elapsed)
            # Sleep in small increments so SIGTERM is honored promptly
            # instead of blocking for the full interval.
            slept = 0.0
            while slept < sleep_for and not _stop:
                step = min(0.5, sleep_for - slept)
                time.sleep(step)
                slept += step
    finally:
        _remove_pidfile()
        print('command_worker_daemon stopped', flush=True)


if __name__ == '__main__':
    main()
