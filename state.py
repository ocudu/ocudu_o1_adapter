#!/usr/bin/python3
#
# Copyright 2021-2025 Software Radio Systems Limited
#
# By using this file, you agree to the terms and conditions set
# forth in the LICENSE file which can be found at the top level of
# the distribution.
#

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
