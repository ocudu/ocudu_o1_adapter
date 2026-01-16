#
# Copyright 2021-2026 Software Radio Systems Limited
#
# By using this file, you agree to the terms and conditions set
# forth in the LICENSE file which can be found at the top level of
# the distribution.
#

"""
PTP log monitor and alarm handler.
"""

import asyncio
import re
from datetime import datetime
from typing import Any, Dict, Optional

from alarm_manager import AlarmManager

# --------- tolerant regexes (use .search) ---------
STATS_RE = re.compile(
    r"""(?i)
        (?:^|\s)(?P<sys_ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+\S+\s+ptp4l:\s+
        \[(?P<uptime>\d+(?:\.\d+)?)\]\s+
        rms\s+(?P<rms>-?\d+)\s+max\s+(?P<max>-?\d+)\s+
        freq\s+(?P<freq>[+-]?\d+)(?:\s+\+/-\s+(?P<freq_jitter>\d+))?\s+
        (?:path\s+)?delay\s+(?P<delay>\d+)(?:\s+\+/-\s+(?P<delay_jitter>\d+))?
    """,
    re.VERBOSE,
)

BEST_MASTER_REMOTE_RE = re.compile(
    r"""(?i)
        (?:^|\s)(?P<sys_ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+\S+\s+ptp4l:\s+
        \[(?P<uptime>\d+(?:\.\d+)?)\]\s+
        selected\s+best\s+master\s+clock\s+(?P<clockid>[0-9a-f:\.]+)
    """,
    re.VERBOSE,
)

BEST_MASTER_LOCAL_RE = re.compile(
    r"""(?i)
        (?:^|\s)(?P<sys_ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+\S+\s+ptp4l:\s+
        \[(?P<uptime>\d+(?:\.\d+)?)\]\s+
        selected\s+local\s+clock\s+(?P<clockid>[0-9a-f:\.]+)\s+as\s+best\s+master
    """,
    re.VERBOSE,
)

PHC2SYS_RE = re.compile(
    r"""(?i)
        (?:^|\s)(?P<sys_ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+\S+\s+phc2sys:\s+
        \[(?P<uptime>\d+(?:\.\d+)?)\]\s+
        (?P<clock>[A-Z_]+)\s+phc\s+
        offset\s+(?P<offset>-?\d+)
        (?:\s+(?P<servo_state>s\d+))?\s+
        freq\s+(?P<phc_freq>-?\d+)\s+
        delay\s+(?P<phc_delay>\d+)
    """,
    re.VERBOSE,
)


# --------- helpers ---------
def parse_sys_ts(ts_str: str) -> str:
    """Parse syslog-style timestamp (no year) and return ISO string."""
    year = datetime.now().year
    return datetime.strptime(f"{year} {ts_str}", "%Y %b %d %H:%M:%S").isoformat()


async def tail_file(path: str):
    """Asynchronously tail a file, yielding new lines as they are written."""
    loop = asyncio.get_running_loop()
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)  # tail from end
        while True:
            line = await loop.run_in_executor(None, f.readline)
            if not line:
                await asyncio.sleep(0.2)
                continue
            yield line.rstrip("\n")


# --------- producer ---------
async def ptp_log_monitor(filename: str, q: asyncio.Queue):
    """
    Monitor a ptp4l/phc2sys log file and push parsed snapshots to the queue.
    """
    # keep rolling state so each snapshot includes both sub-states
    ptp4l_state: Dict[str, Optional[Any]] = {
        "best_master_clockid": None,
        "best_master_source": None,  # "remote" | "local" | None
        "last_change_ts": None,
        "stats": None,
    }
    phc2sys_state: Optional[dict] = None

    async for line in tail_file(filename):
        # Best master (remote / local)
        m = BEST_MASTER_REMOTE_RE.search(line) or BEST_MASTER_LOCAL_RE.search(line)
        if m:
            ptp4l_state["best_master_clockid"] = m.group("clockid")
            ptp4l_state["best_master_source"] = "local" if BEST_MASTER_LOCAL_RE.search(line) else "remote"
            ptp4l_state["last_change_ts"] = parse_sys_ts(m.group("sys_ts"))

            snapshot = {
                "sys_ts": ptp4l_state["last_change_ts"],
                "uptime": float(m.group("uptime")),
                "ptp4l_state": ptp4l_state,
                "phc2sys_state": phc2sys_state,
            }
            await q.put(snapshot)
            continue

        # ptp4l stats
        m = STATS_RE.search(line)
        if m:
            ptp4l_state["stats"] = {
                "sys_ts": parse_sys_ts(m.group("sys_ts")),
                "uptime": float(m.group("uptime")),
                "rms": int(m.group("rms")),
                "max": int(m.group("max")),
                "freq": int(m.group("freq")),
                "freq_jitter": int(m.group("freq_jitter")) if m.group("freq_jitter") else None,
                "delay": int(m.group("delay")),
                "delay_jitter": int(m.group("delay_jitter")) if m.group("delay_jitter") else None,
            }
            snapshot = {
                "sys_ts": parse_sys_ts(m.group("sys_ts")),
                "uptime": float(m.group("uptime")),
                "ptp4l_state": ptp4l_state,
                "phc2sys_state": phc2sys_state,
            }
            await q.put(snapshot)
            continue

        # phc2sys stats
        m = PHC2SYS_RE.search(line)
        if m:
            phc2sys_state = {
                "sys_ts": parse_sys_ts(m.group("sys_ts")),
                "uptime": float(m.group("uptime")),
                "clock": m.group("clock"),  # e.g., CLOCK_REALTIME
                "offset": int(m.group("offset")),  # ns
                "servo_state": (m.group("servo_state") or None),  # e.g., s2
                "freq": int(m.group("phc_freq")),  # ppb
                "delay": int(m.group("phc_delay")),  # ns
            }
            snapshot = {
                "sys_ts": phc2sys_state["sys_ts"],
                "uptime": phc2sys_state["uptime"],
                "ptp4l_state": ptp4l_state,
                "phc2sys_state": phc2sys_state,
            }
            await q.put(snapshot)
            continue


# --------- consumer ---------
async def ptp_health_checker_consumer(
    q: asyncio.Queue, max_latency: int, max_consecutive: int, master_clear_consecutive: int, alarm_mgr: AlarmManager
):
    """
    - Alarm #1: MASTER SOURCE -> LOCAL
        * Fire when best_master_source is observed as "local"
        * While local, keep alarm active (no clearing)
        * When source stops being "local", start counting confirmations
          and clear after `master_clear_consecutive` consecutive non-local snapshots
    - Alarm #2: ptp4l 'max' > threshold for N consecutive stats snapshots
        * Clear on the next compliant stats snapshot (<= threshold)
    """
    # ---- master->local alarm state ----
    master_alarm_active = False
    master_clear_confirms = 0

    # ---- ptp4l max alarm state ----
    max_latency_alarm_active = False
    max_exceed_count = 0

    while True:
        snap = await q.get()
        try:
            # -------- MASTER SOURCE -> LOCAL --------
            source = snap.get("ptp4l_state", {}).get("best_master_source")

            if source == "local":
                # entering/remaining in local
                if not master_alarm_active:
                    master_alarm_active = True
                    master_clear_confirms = 0
                    alarm_mgr.set_alarm(3001, message="PTP master clock source is local")
                else:
                    # still local—no clearing progress while local
                    master_clear_confirms = 0
            else:
                # source is not local -> progress toward clearing if alarm active
                if master_alarm_active:
                    master_clear_confirms += 1
                    if master_clear_confirms >= master_clear_consecutive:
                        master_alarm_active = False
                        alarm_mgr.clear_alarm(3001, message="PTP master clock source is not local anymore")
                        master_clear_confirms = 0

            # -------- PTP4L MAX CONSECUTIVE CHECK --------
            stats = snap.get("ptp4l_state", {}).get("stats")
            if stats is not None and "max" in stats:
                cur_max = stats["max"]
                if cur_max > max_latency:
                    max_exceed_count += 1
                    if (not max_latency_alarm_active) and max_exceed_count >= max_consecutive:
                        max_latency_alarm_active = True
                        alarm_mgr.set_alarm(3002, message="PTP max latency higher than threshold")

                else:
                    # reset + clear if needed
                    if max_latency_alarm_active:
                        alarm_mgr.clear_alarm(3002, message="PTP max latency is back to normal")
                    max_latency_alarm_active = False
                    max_exceed_count = 0

        finally:
            q.task_done()
