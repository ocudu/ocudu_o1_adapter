# SPDX-License-Identifier: BSD 3-Clause Open MPI variant

"""Application state shared across tasks."""

import asyncio
from dataclasses import dataclass


@dataclass()
class AppState:
    """Simple class to state shared across tasks."""

    session_state = {"nc_connected": False, "ws_connected": False}  # NETCONF and Websocket status
    app_command_queue: asyncio.Queue[dict] = asyncio.Queue()  # NETCONF commands to be sent

    ws_send_queue: asyncio.Queue[str] = asyncio.Queue()  # outgoing WS messages
    ws_received_queue: asyncio.Queue[dict] = asyncio.Queue()  # incoming WS messages

    ptp_stats_queue: asyncio.Queue[dict] = asyncio.Queue()  # PTP stats updates

    restart_req = False
