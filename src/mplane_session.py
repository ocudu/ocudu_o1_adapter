# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Persistent O-RU M-plane session for the O1 adapter.

Owns the adapter's long-lived NETCONF relationship with an O-RU: a command
session for config RPCs and edits plus a notification session subscribed to
the RU's event stream. Supervision is the exception to that split:
o-ran-supervision watchdog timers are per NETCONF session, held by the
session that subscribed, so supervision-watchdog-reset RPCs are dispatched
on the NOTIFICATION session (an O-RU answers resets arriving on any other
session with an rpc-error; its watchdog then starves and the O-RU tears the
sessions down itself). Non-supervision notifications are dispatched to
registered handlers.

Call-home (RFC 8071): some O-RUs accept no inbound NETCONF at all and
instead dial the manager (WG4 M-plane startup discovers the manager address
via DHCP). With ru_callhome the session holds a listener and waits for the
O-RU's dial-in; the transport roles invert while the NETCONF/SSH roles stay
put (the O-RU remains the SSH server on the connection it initiated). The
O-RU controls how many connections exist — a persistent call-home policy
makes exactly one — so both roles run on that single session, which is only
legal when the O-RU advertises :interleave (RFC 5277: RPCs while a
subscription is active); a call-home peer without it is closed and waited
out rather than half-driven. Reconnect inverts too: the listener stays
bound across cycles and the O-RU re-dials on its re-call-home timer.

State machine, surfaced via AppState.session_state["ru_mplane_state"]:

    DISCONNECTED -> CONNECTING -> SUPERVISED <-> DEGRADED
          ^_______________________________________|

- SUPERVISED: both sessions up, subscription active, watchdog fed.
- DEGRADED: no supervision-notification within the notification-interval +
  guard budget; a recovery watchdog reset is attempted and the listener keeps
  running. DEGRADED exits only when a watchdog reset is ACCEPTED again — a
  notification alone does not restore SUPERVISED.
- Any transport failure tears both sessions down and re-enters the reconnect
  path. Teardown is bounded: the close-session RPC gets a short deadline and
  a transport still up afterwards is closed directly, so a dead O-RU cannot
  hold the reconnect loop.

Watchdog semantics: one reset is sent immediately after subscribing (the
supervision budget starts when the O-RU enters supervised mode, which happens
on subscription), then resets are notification-driven — a timer-based reset
would mask a genuine O-RU failure. An rpc-error reply means the server is
alive but rejected the request; it is counted, not fatal (the simulated O-RU
has no application behind the RPC and answers every reset that way) — but
only an ACCEPTED reset enters SUPERVISED: a rejected reset never fed the
O-RU's watchdog, and reporting SUPERVISED on its strength would mask a total
supervision failure. Only transport errors kill the session. The reply's
next-update-at may extend the local starvation budget up to twice its
length, never shrink it; a promise beyond that is treated as an O-RU clock
running ahead and the local budget applies.

Alarms: RU_NETCONF_CONNECTION_LOSS (1003) on connect failure or session loss,
cleared as soon as both sessions are up and subscribed (RuForwarder keeps its
own session but leaves this alarm to the resident session when both run),
and RU_SUPERVISION_FAILURE (1004) while DEGRADED. A stop requested during a
connect raises no alarm. Within one outage the connect failure and the retry
transitions after the first failed attempt log at DEBUG, as the adapter's
DU-facing NETCONF loop logs its retries.
"""

import asyncio
import logging
import socket
import time
import xml.etree.ElementTree as ET
from contextlib import suppress
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, List, Optional, Set

from ncclient import manager, NCClientError
from ncclient.operations import rpc as rpc_ops
from ncclient.transport import errors as transport_errors

from ru_config import ROLE_SUDO, ROLES, RuConfig
from state import AppState

# What a connect attempt can raise: ncclient's transport errors, socket errors
# and the bare EOFError paramiko's Transport.start_client() raises when the
# peer closes the connection during the SSH handshake (ncclient wraps only
# SSHException) — an O-RU resetting mid call-home does exactly that.
_CONNECT_FAILURES = (transport_errors.TransportError, OSError, EOFError)

_SUPERVISION_TAG = "{urn:o-ran:supervision:1.0}supervision-notification"

_CONNECTION_ALARM = 1003
_SUPERVISION_ALARM = 1004

# NETCONF connect/RPC-reply deadline. Deliberately independent of the
# reconnect backoff: retry_interval paces reconnect attempts, never how long
# the O-RU may take to answer an RPC.
_RPC_TIMEOUT = 30

# Deadline for the graceful close-session RPC at teardown. ncclient closes
# the transport only after that RPC is answered, so a dead peer would
# otherwise hold every teardown for _RPC_TIMEOUT per session.
_CLOSE_TIMEOUT_S = 5


def du_facing_loops_enabled(profile: str, ru_forward: bool) -> tuple[bool, bool]:
    """Whether the orchestrator runs its DU-facing loops for this profile.

    The RU-management profile's contract with the orchestrator, kept here so
    tests can import it without the adapter's web dependencies. netconf_main
    (the NETCONF session toward a DU/gNB, which it configures) and ws_handler
    (the DU/gNB PM-telemetry websocket consumer) are both DU-facing. The
    RU-management profile (--profile ru) has neither a DU to configure nor a
    telemetry websocket, so running them only spins a permanent connect-retry
    / alarm (1001/1002) flap against nothing. --ru_forward is the one
    RU-profile consumer of the NETCONF loop (it syncs RU state onto the DU
    session), so that loop stays when forwarding is enabled.

    Returns (run_netconf_main, run_ws_handler).
    """
    run_netconf_main = profile != "ru" or ru_forward
    run_ws_handler = profile != "ru"
    return run_netconf_main, run_ws_handler


class RuSessionState(Enum):
    """Lifecycle phase of the persistent M-plane session."""

    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    SUPERVISED = "SUPERVISED"
    DEGRADED = "DEGRADED"


class MplaneSession:  # pylint: disable=too-many-instance-attributes
    """Maintain a supervised NETCONF M-plane session to an O-RU."""

    # netopeer2 (and O-RUs built on it) reap an idle, unsubscribed NETCONF
    # session at ~180 s; the command session holds no subscription, so its
    # keepalive read must fire well inside this regardless of interval/2.
    _COMMAND_SESSION_REAP_S = 180.0

    def __init__(self, app_state: AppState, args, alarm_mgr, retry_interval=5):
        self.app_state = app_state
        self.args = args
        self.alarm_mgr = alarm_mgr
        self.retry_interval = retry_interval
        # Test seams: connect_factory replaces the real NETCONF connect;
        # poll_cap bounds each take_notification wait for stop responsiveness.
        self.connect_factory: Optional[Callable[[], Any]] = None
        self.poll_cap = 5.0
        self.interval = getattr(args, "ru_supervision_interval", 60)
        self.guard = getattr(args, "ru_supervision_guard", 10)
        if self.interval + self.guard <= 0:
            raise ValueError("supervision interval + guard must be a positive budget")
        # The Table 6.5-1 account role the session's client acts as; an
        # absent flag means sudo, the role with every write.
        self.role = getattr(args, "ru_role", None) or ROLE_SUDO
        if self.role not in ROLES:
            raise ValueError(f"ru_role must be one of {ROLES}, got {self.role!r}")
        self.datastore = getattr(args, "ru_datastore", "running")
        self.callhome = getattr(args, "ru_callhome", False)
        self.callhome_port = getattr(args, "ru_callhome_port", 4334)
        self.callhome_bind = getattr(args, "ru_callhome_bind", "0.0.0.0")
        self._callhome_listener: Optional[socket.socket] = None
        self.phase = RuSessionState.DISCONNECTED
        self.command_session: Any = None
        self.notification_session: Any = None
        self.stats = {
            "connect_cycles": 0,
            "supervision_notifications": 0,
            "watchdog_resets": 0,
            "watchdog_rpc_errors": 0,
            "starvations": 0,
            "other_notifications": 0,
            "callhome_accepts": 0,
        }
        self._notification_handlers: List[Callable[[str], None]] = []
        self._cycle_handlers: List[Callable[[RuConfig], None]] = []
        self._connection_alarm_active = False
        self._supervision_alarm_active = False
        # Log throttling: consecutive failed connects in the current outage,
        # the last connect failure's text, and the once-per-cycle warnings
        # already issued (see _log_connect_failure / _warn_once_per_cycle).
        self._connect_failures = 0
        self._last_connect_error: Optional[str] = None
        self._cycle_warnings: Set[str] = set()

    def register_notification_handler(self, handler):
        """Register a callable(notification_xml) for non-supervision notifications.

        The extension seam for notification consumers; a handler exception is
        logged and never kills the session.
        """
        self._notification_handlers.append(handler)

    def register_cycle_handler(self, handler):
        """Register a callable(ru_config) invoked when a cycle reaches SUPERVISED.

        Cycle handlers run blocking NETCONF calls in a worker thread — the
        extension seam for consumers that must reconcile state notifications
        alone cannot replay after a reconnect. NCClientError/OSError propagate
        and recycle the session; any other handler exception is logged and
        isolated.
        """
        self._cycle_handlers.append(handler)

    async def run(self, stop_event=None):
        """Connect, supervise and dispatch until stop_event is set.

        Reconnects with retry_interval backoff on any failure. Intended to run
        for the adapter's lifetime inside the orchestrator's gather.
        """
        while not self._stopping(stop_event):
            # Within one outage only the first failed attempt is news: the
            # retry transitions after it are logged at DEBUG (the state is
            # still updated), as the DU-facing NETCONF loop logs its retries.
            quiet_retry = self._connect_failures > 0
            self._set_phase(RuSessionState.CONNECTING, quiet=quiet_retry)
            if not await self._connect_pair(stop_event):
                if self._stopping(stop_event):
                    # a stop during connect — a call-home dial-in that never
                    # came, a dial-out cut short — is not an outage
                    break
                self._connect_failures += 1
                self._flag_connection_lost()
                self._set_phase(RuSessionState.DISCONNECTED, quiet=quiet_retry)
                await self._sleep_retry(stop_event)
                continue
            self._connect_failures = 0
            self._last_connect_error = None
            self._cycle_warnings.clear()
            self.stats["connect_cycles"] += 1
            # o-ran-supervision timers are per session, held by the session
            # that subscribed — watchdog resets must be dispatched there
            # (kicking on the command session gets rpc-error'd by the O-RU
            # and its watchdog starves until IT tears the sessions down).
            ru_config = RuConfig(
                self.command_session, self.datastore, role=self.role, supervision_manager=self.notification_session
            )
            try:
                await asyncio.to_thread(self.notification_session.create_subscription)
                # Connected and subscribed: the connection alarm is about the
                # transport, so it clears here — before the first watchdog
                # reset — and a reconnect clears it even when the O-RU goes
                # on rejecting resets (1004 tracks supervision separately).
                self._flag_connection_restored()
                # Initial feed: the supervision budget starts when the O-RU
                # enters supervised mode (on subscription), not on its first
                # notification, so waiting to react could already starve it.
                fed, deadline, accepted = await self._reset_watchdog(ru_config)
                if fed:
                    if accepted:
                        self._enter_supervised()
                    await asyncio.to_thread(self._run_cycle_handlers, ru_config)
                    await self._listen(ru_config, stop_event, deadline)
            except (NCClientError, OSError, EOFError) as err:
                # NCClientError is deliberately broad: RPC-reply timeouts and
                # missing-capability errors are direct NCClientError subclasses
                # (siblings of OperationError), and any of them must recycle
                # the session, never escape the orchestrator's gather. EOFError
                # is paramiko's bare handshake abort (_CONNECT_FAILURES): the
                # connect paths catch it, this is the backstop.
                logging.warning("RU M-plane session failed: %s", err)
            finally:
                self._set_phase(RuSessionState.DISCONNECTED)
                await asyncio.to_thread(self._close_sessions)
            if self._stopping(stop_event):
                break
            self._flag_connection_lost()
            await self._sleep_retry(stop_event)
        self._close_callhome_listener()
        self._set_phase(RuSessionState.DISCONNECTED)

    @classmethod
    def _command_keepalive_period(cls, interval):
        """Keepalive cadence for the idle command session.

        interval/2 keeps it live, but it must also beat the server's idle
        reap regardless of the interval — netopeer2-based O-RUs reap an idle
        unsubscribed session at ~180 s, so a large interval whose interval/2
        would exceed that is clamped safely under it.
        """
        return max(0.05, min(interval / 2, cls._COMMAND_SESSION_REAP_S - 30.0))

    async def _listen(self, ru_config, stop_event, deadline=None):
        """Consume the notification stream until stop, starvation-death or session loss.

        The starvation deadline tracks the last supervision-notification (or
        session entry), never take_notification's own timeout — unrelated
        notifications must not feed the watchdog budget. The O-RU's
        next-update-at reply may extend the locally computed deadline (see
        _deadline_from_next_update): the O-RU may lawfully keep its own timer
        configuration.

        The command session carries no traffic between cycle handlers, and
        NETCONF servers reap idle sessions that hold no subscription
        (netopeer2 defaults to 180 s; an O-RU built on it closes the command
        session at exactly that idle age). A cheap operational read every
        interval/2 keeps it alive and doubles as a sync-health probe; its
        failure propagates and recycles the cycle, which is the correct
        discovery of a dead command session.
        """
        budget = self.interval + self.guard
        if deadline is None:
            deadline = time.monotonic() + budget
        keepalive_period = self._command_keepalive_period(self.interval)
        next_keepalive = time.monotonic() + keepalive_period
        while not self._stopping(stop_event):
            if not (self.command_session.connected and self.notification_session.connected):
                logging.warning("RU M-plane session dropped")
                return
            next_keepalive = await self._keepalive_command_session(ru_config, next_keepalive, keepalive_period)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # a notification may already be queued (e.g. a long cycle
                # handler consumed the budget) — drain before starving
                notification = await asyncio.to_thread(self.notification_session.take_notification, block=False)
                if notification is None:
                    self._enter_degraded()
                    fed, next_deadline, _ = await self._reset_watchdog(ru_config)
                    if not fed:
                        return
                    deadline = next_deadline if next_deadline is not None else time.monotonic() + budget
                    continue
            else:
                notification = await asyncio.to_thread(
                    self.notification_session.take_notification,
                    block=True,
                    timeout=min(remaining, self.poll_cap, max(0.01, next_keepalive - time.monotonic())),
                )
                if notification is None:
                    continue
            if self._is_supervision(notification.notification_xml):
                fed, next_deadline, accepted = await self._reset_watchdog(ru_config)
                if not fed:
                    return
                self.stats["supervision_notifications"] += 1
                deadline = next_deadline if next_deadline is not None else time.monotonic() + budget
                if accepted:
                    self._enter_supervised()
            else:
                self.stats["other_notifications"] += 1
                # Off the event loop: a registered handler may block (for
                # example on a synchronous northbound HTTP post), which would
                # otherwise freeze the whole gather — keepalive, watchdog and
                # the sibling NETCONF/WS loops — for that call's timeout.
                await asyncio.to_thread(self._dispatch, notification.notification_xml)

    async def _keepalive_command_session(self, ru_config, next_keepalive, period):
        """Feed the command session's idle timer when due; returns the next due time.

        A cheap strict operational read (sync-status — doubling as a health
        probe) on the command session, so servers that reap idle unsubscribed
        sessions never see it idle. Failure propagates and recycles the cycle.
        """
        if time.monotonic() < next_keepalive:
            return next_keepalive
        await asyncio.to_thread(ru_config.get_sync_status, strict=True)
        return time.monotonic() + period

    async def _connect_pair(self, stop_event=None):
        """Establish the command and notification sessions; True when both are up.

        Direct-connect dials the O-RU twice (one session per role); call-home
        waits for the O-RU's single dial-in and runs both roles on it (see
        _connect_call_home).
        """
        if self.callhome:
            return await self._connect_call_home(stop_event)
        self.command_session = await self._connect_one("command")
        if self.command_session is None:
            return False
        self.notification_session = await self._connect_one("notification")
        if self.notification_session is None:
            await asyncio.to_thread(self._close_sessions)
            return False
        return True

    # Accept slice for the call-home listener: bounds how long a stop request
    # can go unnoticed while waiting for the O-RU to dial in.
    _CALLHOME_ACCEPT_SLICE_S = 1.0

    async def _connect_call_home(self, stop_event):
        """Wait for the O-RU's call-home and run both roles on that session.

        The O-RU decides how many connections exist and a persistent
        call-home policy makes exactly one, so command and notification roles
        share the accepted session — legal only when the O-RU advertises
        :interleave (RPCs while a subscription is active, RFC 5277). A peer
        without it is closed and the listener waits for the next dial-in
        instead of half-driving the RU.
        """
        if self.connect_factory is not None:
            try:
                session = self.connect_factory()
            except _CONNECT_FAILURES as err:
                self._log_connect_failure("call-home session setup", err)
                return False
        else:
            client_socket = await self._accept_call_home(stop_event)
            if client_socket is None:
                return False
            try:
                session = await asyncio.to_thread(self._session_from_callhome_socket, client_socket)
            except (NCClientError, OSError, EOFError) as err:
                self._log_connect_failure("call-home session setup", err)
                with suppress(Exception):
                    client_socket.close()
                return False
        if not self._supports_interleave(session):
            logging.error(
                "call-home O-RU does not advertise :interleave — commands and notifications "
                "cannot share its single session; closing and waiting for the next dial-in"
            )
            with suppress(Exception):
                await asyncio.to_thread(session.close_session)
            return False
        self.command_session = session
        self.notification_session = session
        self.stats["callhome_accepts"] += 1
        return True

    async def _accept_call_home(self, stop_event):
        """Block until the O-RU dials the listener; the socket, or None on stop.

        The listener is bound once (SO_REUSEADDR) and stays bound across
        reconnect cycles: reconnection is the O-RU re-dialing on its
        re-call-home timer, and a persistent listener never loses a dial-in
        to a bind race. The accept is sliced so a stop request is honored
        promptly.
        """
        listener = self._ensure_callhome_listener()
        if listener is None:
            return None
        while not self._stopping(stop_event):
            try:
                client_socket, peer = await asyncio.to_thread(listener.accept)
            except socket.timeout:
                continue
            except OSError as err:
                logging.warning("RU call-home accept failed: %s", err)
                self._close_callhome_listener()
                return None
            logging.info("RU call-home connection from %s:%s", peer[0], peer[1])
            return client_socket
        return None

    def _ensure_callhome_listener(self):
        """Bind the call-home listener once; None when binding fails."""
        if self._callhome_listener is not None:
            return self._callhome_listener
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.settimeout(self._CALLHOME_ACCEPT_SLICE_S)
            listener.bind((self.callhome_bind, self.callhome_port))
            listener.listen(1)
        except OSError as err:
            logging.error("cannot listen for RU call-home on %s:%s: %s", self.callhome_bind, self.callhome_port, err)
            with suppress(Exception):
                listener.close()
            return None
        logging.info("Listening for RU call-home on %s:%s", self.callhome_bind, self.callhome_port)
        self._callhome_listener = listener
        return listener

    def _close_callhome_listener(self):
        """Release the call-home listener (end of the session's lifetime)."""
        if self._callhome_listener is not None:
            with suppress(Exception):
                self._callhome_listener.close()
            self._callhome_listener = None

    def _session_from_callhome_socket(self, client_socket):
        """NETCONF session over an accepted call-home socket.

        RFC 8071 inverts only the transport: the O-RU still acts as the SSH
        server on the connection it initiated, so this authenticates exactly
        like a dial-out. The RPC deadline stays _RPC_TIMEOUT — unlike
        ncclient's manager.call_home, whose single timeout conflates the
        accept wait with the session's RPC deadline.
        """
        peer_host = client_socket.getpeername()[0]
        # the credential/hostkey parameters necessarily mirror every other
        # NETCONF connect in the tree  # pylint: disable=duplicate-code
        return manager.connect_ssh(
            host=peer_host,
            sock=client_socket,
            username=self.args.ru_netconf_username,
            password=self.args.ru_netconf_password,
            hostkey_verify=False,
            allow_agent=False,
            look_for_keys=False,
            timeout=_RPC_TIMEOUT,
        )
        # pylint: enable=duplicate-code

    @staticmethod
    def _supports_interleave(session):
        """Whether the O-RU advertises the RFC 5277 :interleave capability."""
        capabilities = getattr(session, "server_capabilities", None) or ()
        return any(":interleave" in capability for capability in capabilities)

    async def _connect_one(self, role):
        """Connect one NETCONF session to the O-RU; None on failure."""

        def sync_connect():
            if self.connect_factory is not None:
                return self.connect_factory()
            params = {
                "host": self.args.ru_netconf_host,
                "port": self.args.ru_netconf_port,
                "username": self.args.ru_netconf_username,
                "password": self.args.ru_netconf_password,
                "hostkey_verify": False,
                "allow_agent": False,
                "look_for_keys": False,
                "timeout": _RPC_TIMEOUT,
            }
            return manager.connect(**params)

        try:
            return await asyncio.to_thread(sync_connect)
        except _CONNECT_FAILURES as err:
            self._log_connect_failure(f"{role} session", err)
            return None

    def _log_connect_failure(self, what, err):
        """Log a failed connect: WARNING when it is news, DEBUG on identical repeats.

        The first failure of an outage — and any failure whose text differs
        from the previous one — is a WARNING; identical repeats until a
        connect succeeds are DEBUG, so an outage paced by retry_interval does
        not flood the log (the DU-facing NETCONF loop logs its retries at
        DEBUG as well).
        """
        # paramiko's handshake EOFError carries no message: name the type then
        reason = str(err) or type(err).__name__
        text = f"{what}: {reason}"
        level = logging.DEBUG if text == self._last_connect_error else logging.WARNING
        self._last_connect_error = text
        logging.log(level, "RU M-plane session connect failed (%s): %s", what, reason)

    def _warn_once_per_cycle(self, key, message, *args):
        """WARNING the first time per connect cycle, DEBUG on repeats within it."""
        if key in self._cycle_warnings:
            logging.debug(message, *args)
            return
        self._cycle_warnings.add(key)
        logging.warning(message, *args)

    async def _reset_watchdog(self, ru_config):
        """Send supervision-watchdog-reset; (fed, deadline_override, accepted).

        fed is False only when the O-RU did not answer at all (transport dead
        or the RPC reply timed out) — the session must be recycled. An
        rpc-error means alive-but-rejected and is counted, not fatal — but
        accepted is False: a rejected reset did NOT feed the O-RU's watchdog,
        so the session must not claim SUPERVISED on its strength (an O-RU
        whose timer is never fed will tear the sessions down itself). When the
        reply carries next-update-at, the returned deadline override reflects
        the timers the O-RU actually applied (it may lawfully keep its own
        configuration and say so via error-message).
        """
        try:
            reply = await asyncio.to_thread(ru_config.reset_supervision_watchdog, self.interval, self.guard)
            self.stats["watchdog_resets"] += 1
            if reply.get("error_message"):
                # the O-RU kept its own timers: news once per connect cycle,
                # not on every accepted reset
                self._warn_once_per_cycle(
                    "timers-adjusted", "O-RU adjusted the supervision timers: %s", reply["error_message"]
                )
            return True, self._deadline_from_next_update(reply.get("next_update_at")), True
        except rpc_ops.RPCError as err:
            self.stats["watchdog_rpc_errors"] += 1
            logging.warning("supervision-watchdog-reset rejected by the O-RU: %s", err)
            return True, None, False
        except (NCClientError, OSError) as err:
            # Reply timeout (TimeoutExpiredError is a direct NCClientError
            # subclass, NOT an OperationError) or transport failure: the O-RU
            # is not answering — recycle the session.
            logging.warning("supervision-watchdog-reset failed: %s", err)
            return False, None, False

    def _deadline_from_next_update(self, next_update_at):
        """Monotonic starvation deadline from a next-update-at leaf, or None.

        next-update-at is yang:date-and-time; the budget extends to that
        instant plus the guard. Unparseable or past values fall back to the
        locally computed budget — and so do values SHORTER than the local
        budget: next-update-at is stamped by the O-RU's clock, which need not
        be NTP-disciplined (an O-RU clock running tens of seconds behind the
        host turns a generous promise into a razor-thin deadline that flaps
        DEGRADED on every cycle). A foreign-clock promise may extend patience,
        never shrink it — and only up to twice the local budget: beyond that
        the O-RU's clock is taken to be running ahead (one WARNING per connect
        cycle) and the local budget applies, so a skewed clock cannot switch
        starvation detection off.
        """
        if not next_update_at:
            return None
        try:
            stamp = datetime.fromisoformat(str(next_update_at).replace("Z", "+00:00"))
        except ValueError:
            logging.debug("Ignoring unparseable next-update-at: %s", next_update_at)
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        budget = self.interval + self.guard
        delta = (stamp - datetime.now(timezone.utc)).total_seconds() + self.guard
        if delta <= budget:
            return None
        if delta > 2 * budget:
            self._warn_once_per_cycle(
                "clock-ahead",
                "O-RU next-update-at lies %.0f s beyond the local supervision budget of %s s; "
                "treating the O-RU clock as running ahead and keeping the local budget",
                delta - budget,
                budget,
            )
            return None
        return time.monotonic() + delta

    @staticmethod
    def _is_supervision(notification_xml):
        """Whether a raw NETCONF notification is an o-ran-supervision one."""
        try:
            return ET.fromstring(notification_xml).find(".//" + _SUPERVISION_TAG) is not None
        except ET.ParseError:
            logging.debug("Ignoring unparseable notification")
            return False

    def _dispatch(self, notification_xml):
        """Hand a non-supervision notification to every registered handler."""
        self._log_carrier_state_changes(notification_xml)
        for handler in self._notification_handlers:
            try:
                handler(notification_xml)
            except Exception:  # pylint: disable=broad-exception-caught
                logging.exception("RU notification handler failed")

    @staticmethod
    def _log_carrier_state_changes(notification_xml):
        """Surface o-ran-uplane-conf array-carriers state changes at INFO.

        Carrier activation is asynchronous (DISABLED -> BUSY -> READY); the
        O-RU announces each transition with a state-change notification.
        These are the operator's activation receipt, so they go to the log
        as first-class events instead of vanishing into handler dispatch.
        """
        try:
            root = ET.fromstring(notification_xml)
        except ET.ParseError:
            return
        namespace = "{urn:o-ran:uplane-conf:1.0}"
        for change_tag, list_tag in (
            ("tx-array-carriers-state-change", "tx-array-carriers"),
            ("rx-array-carriers-state-change", "rx-array-carriers"),
        ):
            for change in root.iter(f"{namespace}{change_tag}"):
                for carrier in change.iter(f"{namespace}{list_tag}"):
                    name = carrier.findtext(f"{namespace}name")
                    state = carrier.findtext(f"{namespace}state")
                    if name and state:
                        logging.info("O-RU carrier state change: %s -> %s", name, state)

    def _run_cycle_handlers(self, ru_config):
        """Run the cycle handlers; session-level failures propagate, bugs don't."""
        for handler in self._cycle_handlers:
            try:
                handler(ru_config)
            except (NCClientError, OSError):
                raise
            except Exception:  # pylint: disable=broad-exception-caught
                logging.exception("RU cycle handler failed")

    def _enter_supervised(self):
        """Enter (or restore) SUPERVISED, clearing the supervision alarm.

        Only an accepted watchdog reset gets here. The connection alarm was
        already cleared when the pair came up subscribed (clearing it again
        is a no-op), so this transition owns 1004 alone.
        """
        self._set_phase(RuSessionState.SUPERVISED)
        self._flag_connection_restored()
        if self._supervision_alarm_active:
            self.alarm_mgr.clear_alarm(_SUPERVISION_ALARM, message="RU supervision restored")
            self._supervision_alarm_active = False

    def _enter_degraded(self):
        """Enter DEGRADED after watchdog starvation, raising the supervision alarm."""
        if self.phase is RuSessionState.DEGRADED:
            return
        self._set_phase(RuSessionState.DEGRADED)
        self.stats["starvations"] += 1
        logging.warning("No supervision-notification within %ss; RU supervision degraded", self.interval + self.guard)
        if not self._supervision_alarm_active:
            self.alarm_mgr.set_alarm(_SUPERVISION_ALARM, message="RU supervision failure")
            self._supervision_alarm_active = True

    def _flag_connection_lost(self):
        """Raise the RU connection alarm once per outage."""
        if not self._connection_alarm_active:
            self.alarm_mgr.set_alarm(_CONNECTION_ALARM, message="RU M-plane connection lost")
            self._connection_alarm_active = True

    def _flag_connection_restored(self):
        """Clear the RU connection alarm once per recovery."""
        if self._connection_alarm_active:
            self.alarm_mgr.clear_alarm(_CONNECTION_ALARM, message="RU M-plane connection restored")
            self._connection_alarm_active = False

    def _close_sessions(self):
        """Close both sessions, tolerating already-dead transports.

        In call-home mode both roles share one session — close it once. The
        graceful close-session RPC runs under _CLOSE_TIMEOUT_S rather than the
        RPC deadline: a dead O-RU never answers it, and ncclient closes the
        transport only after a reply, so whatever is still connected
        afterwards has its transport closed directly. No session thread
        outlives the cycle and teardown is bounded regardless of the peer.
        """
        sessions = {id(session): session for session in (self.command_session, self.notification_session)}
        for session in sessions.values():
            if session is None:
                continue
            with suppress(Exception):
                session.timeout = _CLOSE_TIMEOUT_S
            with suppress(NCClientError, OSError):
                session.close_session()
            if getattr(session, "connected", False):
                with suppress(Exception):
                    session.session.close()
        self.command_session = None
        self.notification_session = None

    def _set_phase(self, phase, quiet=False):
        """Record a phase transition in the session and the shared app state.

        ru_supervised is derived from the phase, so it is True exactly while
        SUPERVISED. quiet logs the transition at DEBUG instead of INFO (retry
        churn inside one outage).
        """
        if self.phase is not phase:
            logging.log(
                logging.DEBUG if quiet else logging.INFO, "RU M-plane session: %s -> %s", self.phase.value, phase.value
            )
        self.phase = phase
        self.app_state.session_state["ru_mplane_state"] = phase.value
        self.app_state.session_state["ru_supervised"] = phase is RuSessionState.SUPERVISED

    async def _sleep_retry(self, stop_event):
        """Back off before reconnecting, waking early when stopping."""
        if stop_event is None:
            await asyncio.sleep(self.retry_interval)
            return
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=self.retry_interval)

    @staticmethod
    def _stopping(stop_event):
        """Whether a stop has been requested."""
        return stop_event is not None and stop_event.is_set()
