# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
This module provides a class `WsRemoteCommands` to handle remote commands via WebSocket.
"""

import asyncio
import json
import logging
from collections import deque
from typing import Any, Deque, Dict

WS_REPLY_TIMEOUT = 5  # seconds


def resolve_ws_reply(replies: Dict[str, Deque[asyncio.Future]], msg: str):
    """Hand a gNB command reply to the oldest request still waiting on that command."""
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    waiting = replies.get(str(data.get("cmd")))
    if waiting:
        future = waiting.popleft()
        # A timed-out request stays queued (cancelled), so a late reply still pops its own slot.
        if not future.done():
            future.set_result(data)


def fail_ws_replies(replies: Dict[str, Deque[asyncio.Future]]):
    """Fail every pending request: a new WS session never answers the previous session's commands."""
    for waiting in replies.values():
        for future in waiting:
            if not future.done():
                future.set_exception(ConnectionError("WS session restarted"))
    replies.clear()


# pylint: disable=too-few-public-methods
class WsRemoteCommands:
    """
    A class to handle remote commands via WebSocket.

    Attributes:
        ws_send_queue (queue): The WebSocket send queue.
    """

    def __init__(self, ws_send_queue=None, ws_replies=None):
        self.send_queue = ws_send_queue
        self.replies = ws_replies

    def send_ssb_command(self, ssb_cell_config) -> Any:
        """
        Sends an SSB (Synchronization Signal Block) configuration command.

        Args:
            ssb_cell_config (list): A list of cell configurations for the SSB.
        """
        return self._send_ws_message(
            {
                "cmd": "ssb_set",
                "cells": ssb_cell_config,
            }
        )

    def send_rrm_policy_ratio_command(self, rrm_policy_ratio_config) -> Any:
        """
        Sends an RRM (Radio Resource Management) policy ratio configuration command.

        Args:
            rrm_policy_ratio_config (list): A list of RRM policy ratio configurations.
        """
        return self._send_ws_message(
            {
                "cmd": "rrm_policy_ratio_set",
                "policies": rrm_policy_ratio_config,
            }
        )

    def send_quit_command(self) -> Any:
        """
        Sends a quit command to the server and waits for connection close.

        Returns:
            bool: True if quit command was successful and connection closed properly,
        """
        return self._send_ws_message({"cmd": "quit"})

    def send_metrics_subscribe(self) -> Any:
        """Request the gNB to (re-)emit its metric snapshot."""
        return self._send_ws_message({"cmd": "metrics_subscribe"})

    async def request(self, command: Dict) -> dict:
        """Send a command and return the gNB's reply; it carries "error" when the command failed."""
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self.replies.setdefault(command["cmd"], deque()).append(future)
        self._send_ws_message(command)
        reply = await asyncio.wait_for(future, WS_REPLY_TIMEOUT)
        if "error" in reply:
            logging.warning("gNB rejected %s: %s", command["cmd"], reply["error"])
        return reply

    def _send_ws_message(self, command: Dict) -> Any:
        # Put quit command on WS queue and return
        self.send_queue.put_nowait(json.dumps(command))
        return True
