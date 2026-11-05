# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Application state shared across tasks."""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Deque, Dict


@dataclass()
class AppState:  # pylint: disable=too-many-instance-attributes
    """Simple class to state shared across tasks."""

    session_state = {
        "nc_connected": False,
        "ru_nc_connected": False,
        "ws_connected": False,
        "ru_supervised": False,  # persistent M-plane session holds a supervised RU
        "ru_mplane_state": "DISCONNECTED",  # mplane_session.RuSessionState surface
    }  # NETCONF and Websocket status
    ru_update_queue: asyncio.Queue[str] = asyncio.Queue()  # outgoing RU NETCONF updates as XML payloads

    ws_send_queue: asyncio.Queue[str] = asyncio.Queue()  # outgoing WS messages
    ws_received_queue: asyncio.Queue[dict] = asyncio.Queue()  # incoming WS messages
    # cmd -> futures awaiting its reply, oldest first (the gNB answers each command in order)
    ws_replies: Dict[str, Deque[asyncio.Future]] = field(default_factory=dict)

    ptp_stats_queue: asyncio.Queue[dict] = asyncio.Queue()  # PTP stats updates

    pm_metrics_queue: asyncio.Queue = asyncio.Queue()  # (target, envelope) tuples awaiting HTTP push
    pm_jobs: Dict[str, dict] = field(default_factory=dict)  # PerfMetricJob id -> attrs

    # the O-RU provisioner's outcome for the current connect cycle (ru_provisioner.ru_status reads it)
    ru_provision: Dict[str, Any] = field(
        default_factory=lambda: {"phase": "NOT_PROVISIONED", "sync_state": None, "carriers": {}, "updated_at": None}
    )

    restart_req = False
    ws_connects = 0  # WS sessions opened so far; a new one may face a restarted gNB
