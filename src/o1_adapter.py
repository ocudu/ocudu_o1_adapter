#!/usr/bin/python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
This module provides an O1 adapter for OCUDU, which manages and updates the configuration of a gNB / CU / DU .
It includes functionalities for retrieving configurations, detecting changes,
updating runtime configurations and triggering full restarts if necessary.

Usage:
    This module can be executed as a standalone script.
"""

# pylint: disable=logging-fstring-interpolation

import argparse
import asyncio
import json
import logging
import os
import ssl
import threading
from contextlib import suppress

import websockets
import yaml
from flask import Flask, jsonify
from ncclient import manager
from ncclient.transport.errors import AuthenticationError, SessionCloseError, SSHError, SSHUnknownHostError, TLSError

from alarm_defs import AlarmDefinitions
from alarm_manager import AlarmEvent, AlarmManager
from config_manager import ConfigManager
from mplane_session import du_facing_loops_enabled, MplaneSession
from pm_metrics import PmMetrics
from ptp_monitor import ptp_health_checker_consumer, ptp_log_monitor
from rpc_log import add_rpc_log_argument, enable_rpc_log
from ru_config import ROLE_SUDO, ROLES
from ru_forwarder import RuForwarder
from ru_provisioner import load_provision_config, RuProvisioner
from ssh_algorithms import restrict_ssh_algorithms
from state import AppState
from ves import VesMessages

# Flask app
app = Flask(__name__)

RETRY_INTERVAL = 5  # seconds

# ncclient reads ~/.ssh/known_hosts and takes another location only through an ssh_config, so
# --netconf_known_hosts is passed to it in one written here. Fixed path: rewritten every start.
NETCONF_SSH_CONFIG = "/tmp/netconf_ssh_config"


def configure_app(state: AppState, auto_heal=False):
    """
    Configures the given Flask application with specific routes for health checks and state management.

    Args:
        app (Flask): The Flask application instance to configure.
        auto_heal (bool, optional): Flag to enable automatic healing by resetting the restart request. Defaults to False

    Returns:
        Flask: The configured Flask application instance.

    Routes:
        /config-healthy (GET): Checks the configuration health. If `auto_heal` is enabled and a restart is required,
            it resets the restart request and returns a failure response.
        /status (GET): Provides a simple health check of the O1 adapter,
            always returning success if the service is reachable.
        /restarted (POST): Resets the restart request state and returns a success response.
    """

    @app.route("/config-healthy")
    def get_config_healthy():
        # Config health check
        if state.restart_req:
            if auto_heal:
                state.restart_req = False
            return (
                jsonify({"success": "NOK"}),
                400,
            )
        return jsonify({"success": "OK"})

    @app.route("/status", methods=["GET"])
    def get_status():
        # Simple health-check of the O1 adapter itself
        # Always return success if the service is reachable
        return jsonify({"success": "OK"})

    @app.route("/restarted", methods=["POST"])
    def reset_state():
        state.restart_req = False
        return jsonify({"success": "OK"})

    return app


async def try_connect(args, alarm_mgr):
    """
    Try to connect to the NETCONF server once.
    Returns manager instance if successful, None otherwise
    """
    try:

        def connect():
            if args.netconf_tls:
                cert_dir = args.netconf_tls_cert_dir
                m = manager.connect_tls(
                    host=args.netconf_host,
                    port=args.netconf_tls_port,
                    keyfile=os.path.join(cert_dir, "client.key"),
                    certfile=os.path.join(cert_dir, "client.crt"),
                    ca_certs=os.path.join(cert_dir, "ca.crt"),
                    protocol=ssl.PROTOCOL_TLS_CLIENT,
                    check_hostname=False,
                    timeout=RETRY_INTERVAL,
                )
            else:
                m = manager.connect(
                    host=args.netconf_host,
                    port=args.netconf_port,
                    username=args.netconf_username,
                    password=args.netconf_password,
                    hostkey_verify=args.netconf_hostkey_verify,
                    ssh_config=args.netconf_ssh_config,
                    allow_agent=False,
                    look_for_keys=False,
                    timeout=RETRY_INTERVAL,
                )
            logging.info("Connected to NETCONF server")
            alarm_mgr.clear_alarm(
                1001,
                message="NETCONF connection restored",
            )
            return m

        return await asyncio.to_thread(connect)
    except AuthenticationError as e:
        logging.warning(f"NETCONF authentication failed for user '{args.netconf_username}': {e}")
        alarm_mgr.set_alarm(1001, message="NETCONF connection lost")
        return None
    except SSHUnknownHostError as e:
        logging.warning(f"NETCONF unknown host key for {args.netconf_host}: {e}")
        alarm_mgr.set_alarm(1001, message="NETCONF connection lost")
        return None
    except SessionCloseError as e:
        logging.warning(f"NETCONF session closed unexpectedly while connecting to {args.netconf_host}: {e}")
        alarm_mgr.set_alarm(1001, message="NETCONF connection lost")
        return None
    except SSHError as e:
        logging.warning(f"NETCONF SSH error while connecting to {args.netconf_host}:{args.netconf_port}: {e}")
        alarm_mgr.set_alarm(1001, message="NETCONF connection lost")
        return None
    except TLSError as e:
        logging.warning(f"NETCONF TLS error while connecting to {args.netconf_host}:{args.netconf_tls_port}: {e}")
        alarm_mgr.set_alarm(1001, message="NETCONF connection lost")
        return None


async def netconf_main(state: AppState, args, alarm_mgr, ru_forwarder=None):
    """
    Main loop for managing the NETCONF connection.
    """
    startup_ru_sync_done = False
    while True:
        netconf_session = await try_connect(args, alarm_mgr)
        if netconf_session:
            state.session_state["nc_connected"] = True
            stop_event = asyncio.Event()

            if ru_forwarder and not startup_ru_sync_done:
                await ru_forwarder.sync_source_netconf_from_ru(netconf_session)
                startup_ru_sync_done = True

            writer = ConfigManager(
                state,
                netconf_session,
                args.datastore,
                args.config,
                args.template,
                args.ru_forward,
                profile=args.profile,
            )
            worker = asyncio.create_task(writer.run(stop_event))
            writer.write_full_config(None)

            # Monitor connection in main loop
            while netconf_session.connected:
                await asyncio.sleep(2)

            logging.info("Connection dropped (main loop)")
            state.session_state["nc_connected"] = False
            alarm_mgr.set_alarm(
                1001,
                message="NETCONF connection lost",
            )
            stop_event.set()
            await worker

        logging.debug(f"Retrying in {RETRY_INTERVAL} seconds...")
        await asyncio.sleep(RETRY_INTERVAL)


async def _run_ws_session(ws, state: AppState, pm_metrics: PmMetrics):
    """Drive one WS connection's sender/receiver/keepalive tasks until one of them exits."""
    # clear pending messages
    logging.debug(f"Clearing {state.ws_send_queue.qsize()} pending WS messages")
    while not state.ws_send_queue.empty():
        state.ws_send_queue.get_nowait()
        state.ws_send_queue.task_done()

    # Subscribe to metrics
    state.ws_send_queue.put_nowait(json.dumps({"cmd": "metrics_subscribe"}))

    # Sender task: push messages from queue to WS
    async def sender():
        while True:
            msg = await state.ws_send_queue.get()
            try:
                await ws.send(msg)
            except websockets.exceptions.ConnectionClosed:
                break
            logging.debug(f"TXed WS: {msg}")

    # Receiver task: print WS incoming messages
    async def receiver():
        try:
            async for msg in ws:
                await pm_metrics.handle_ws_message(msg)
        except websockets.exceptions.ConnectionClosed:
            return

    async def keepalive():
        while True:
            try:
                pong_waiter = await ws.ping()
                await asyncio.wait_for(pong_waiter, timeout=5)
            except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
                break
            await asyncio.sleep(5)

    tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver()), asyncio.create_task(keepalive())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()

    for task in pending:
        with suppress(asyncio.CancelledError):
            await task

    for task in done:
        exc = task.exception()
        if exc:
            raise exc


async def ws_handler(state: AppState, args, alarm_mgr, pm_metrics: PmMetrics):
    """WebSocket handler main loop."""
    while True:
        try:
            async with websockets.connect(
                f"ws://{args.ws_host}:{args.ws_port}",
                ping_interval=5,
                ping_timeout=5,
            ) as ws:
                logging.info("Connected to WebSocket server")
                state.session_state["ws_connected"] = True
                alarm_mgr.clear_alarm(
                    1002,
                    message="WS connection restored",
                )
                await _run_ws_session(ws, state, pm_metrics)

        except (
            OSError,
            websockets.exceptions.ConnectionClosedError,
            websockets.exceptions.ConnectionClosedOK,
            websockets.exceptions.InvalidURI,
        ) as e:
            logging.error(f"WS connection error: {e}")
        finally:
            if state.session_state.get("ws_connected"):
                state.session_state["ws_connected"] = False
                alarm_mgr.set_alarm(
                    1002,
                    message="WS connection closed",
                )
            await asyncio.sleep(RETRY_INTERVAL)


def load_ru_provisioning(arg_parser, args):
    """Validate the provisioning flags and load the profile, or exit through arg_parser.error.

    A malformed profile is a startup error, not something to rediscover on
    every connect cycle; returns the validated dict, or None without
    --ru_provision_config.
    """
    if not args.ru_provision_config:
        return None
    if not args.ru_supervise:
        arg_parser.error("--ru_provision_config requires --ru_supervise (provisioning runs on the M-plane session)")
    if args.ru_forward:
        arg_parser.error("--ru_provision_config cannot be combined with --ru_forward (two writers of the O-RU config)")
    if args.ru_sync_timeout < 0:
        arg_parser.error("--ru_sync_timeout must be >= 0 seconds (0 reads the sync state once and never waits)")
    if args.ru_interface_timeout < 0:
        arg_parser.error("--ru_interface_timeout must be >= 0 seconds (0 reads the interfaces once and never waits)")
    config = None
    try:
        config = load_provision_config(args.ru_provision_config, yaml.safe_load)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as err:
        arg_parser.error(f"--ru_provision_config: {err}")
    return config


async def orchestrator(args, alarm_mgr):
    """Orchestrator: run NETCONF + WebSocket tasks."""
    # Create shared state
    state = AppState()
    ru_forwarder = RuForwarder(state, args, alarm_mgr, RETRY_INTERVAL) if args.ru_forward else None
    mplane_session = MplaneSession(state, args, alarm_mgr, RETRY_INTERVAL) if args.ru_supervise else None
    if mplane_session and args.ru_provision:
        # registered first: the base configuration must exist before anything
        # else that registers on the cycle seam touches the O-RU
        provisioner = RuProvisioner(
            args.ru_provision,
            sync_timeout_s=args.ru_sync_timeout,
            interface_timeout_s=args.ru_interface_timeout,
            supervision_interval=args.ru_supervision_interval,
            supervision_guard=args.ru_supervision_guard,
        )
        mplane_session.register_cycle_handler(provisioner.provision)
    pm_metrics = PmMetrics(state, args.profile)

    configure_app(state, args.autoheal)

    # Skip the DU-facing loops (NETCONF session toward the DU, PM websocket)
    # when managing an O-RU — see mplane_session.du_facing_loops_enabled.
    # pm_metrics.run_pusher is an independent queue consumer and stays.
    run_netconf_main, run_ws_handler = du_facing_loops_enabled(args.profile, ru_forwarder is not None)

    await asyncio.gather(
        netconf_main(state, args, alarm_mgr, ru_forwarder) if run_netconf_main else asyncio.sleep(0),
        ru_forwarder.run() if ru_forwarder else asyncio.sleep(0),
        mplane_session.run() if mplane_session else asyncio.sleep(0),
        ws_handler(state, args, alarm_mgr, pm_metrics) if run_ws_handler else asyncio.sleep(0),
        pm_metrics.run_pusher(),
        ptp_log_monitor(args.ptp_log, state.ptp_stats_queue) if args.ptp_log else asyncio.sleep(0),
        (
            ptp_health_checker_consumer(
                state.ptp_stats_queue,
                args.ptp_max_latency,
                args.ptp_max_consecutive,
                args.ptp_master_clear_consecutive,
                alarm_mgr,
            )
            if args.ptp_log
            else asyncio.sleep(0)
        ),
    )


def start_flask():
    """Run Flask in a background thread."""
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OCUDU O1 adapter.")

    parser.add_argument(
        "--netconf_host",
        type=str,
        default="localhost",
        help="The device IP or DN",
    )
    parser.add_argument(
        "--netconf_port",
        type=int,
        default=830,
        help="Specify this if you want a non-default port",
    )
    parser.add_argument(
        "--netconf_username",
        type=str,
        default="root",
        help="SSH user",
    )
    parser.add_argument(
        "--netconf_password",
        type=str,
        default="root",
        help="SSH pass",
    )
    parser.add_argument(
        "--netconf_hostkey_verify",
        action="store_true",
        help="Verify the NETCONF server's SSH host key against --netconf_known_hosts",
    )
    parser.add_argument(
        "--netconf_known_hosts",
        type=str,
        default="/etc/netconf-ssh/known_hosts",
        help="known_hosts file carrying the NETCONF server's SSH host key",
    )
    parser.add_argument(
        "--netconf_tls",
        action="store_true",
        help="Connect to the NETCONF server over TLS (RFC 7589) instead of SSH",
    )
    parser.add_argument(
        "--netconf_tls_port",
        type=int,
        default=6513,
        help="NETCONF-over-TLS port",
    )
    parser.add_argument(
        "--netconf_tls_cert_dir",
        type=str,
        default="/etc/netconf-tls-client",
        help="Directory holding client.crt, client.key and ca.crt for NETCONF-over-TLS",
    )
    parser.add_argument(
        "--ru_forward",
        action="store_true",
        help="Forward source NETCONF config updates to RU NETCONF server",
    )
    parser.add_argument(
        "--ru_netconf_host",
        type=str,
        default="10.10.0.192",
        help="RU NETCONF host IP Address",
    )
    parser.add_argument(
        "--ru_netconf_port",
        type=int,
        default=830,
        help="RU NETCONF port",
    )
    parser.add_argument(
        "--ru_netconf_username",
        type=str,
        default="",
        help="RU NETCONF username",
    )
    parser.add_argument(
        "--ru_netconf_password",
        type=str,
        default="",
        help="RU NETCONF password",
    )
    parser.add_argument(
        "--ru_datastore",
        type=str,
        default="running",
        help="RU datastore to use",
    )
    parser.add_argument(
        "--ru_supervise",
        action="store_true",
        help="Maintain a persistent supervised M-plane session to the RU NETCONF server",
    )
    parser.add_argument(
        "--ru_callhome",
        action="store_true",
        help="Accept the RU's NETCONF call-home (RFC 8071) instead of connecting out — "
        "for O-RUs that accept no inbound connections; requires the RU to advertise :interleave",
    )
    parser.add_argument(
        "--ru_callhome_port",
        type=int,
        default=4334,
        help="TCP port to listen on for the RU's call-home",
    )
    parser.add_argument(
        "--ru_callhome_bind",
        type=str,
        default="0.0.0.0",
        help="Address to bind the call-home listener",
    )
    parser.add_argument(
        "--ru_supervision_interval",
        type=int,
        default=60,
        help="o-ran-supervision notification interval in seconds",
    )
    parser.add_argument(
        "--ru_supervision_guard",
        type=int,
        default=10,
        help="o-ran-supervision guard timer overhead in seconds",
    )
    parser.add_argument(
        "--ru_provision_config",
        type=str,
        default=None,
        help="Full-config YAML (interface, processing, endpoint, carrier; optional tdd, activation) applied to "
        "the RU on every connect cycle of the M-plane session",
    )
    parser.add_argument(
        "--ru_sync_timeout",
        type=int,
        default=300,
        help="Seconds to wait for the RU's sync-state LOCKED before leaving the carriers inactive (0 = read once)",
    )
    parser.add_argument(
        "--ru_interface_timeout",
        type=int,
        default=300,
        help=(
            "Seconds to wait, as hybrid-odu, for the management plane's VLAN interface before deferring "
            "provisioning to the next connect cycle (0 = read once)"
        ),
    )
    # the validated profile dict, attached after parsing (load_ru_provisioning)
    parser.set_defaults(ru_provision=None)
    parser.add_argument(
        "--ru_role",
        choices=ROLES,
        default=None,
        help="NACM account group the M-plane session acts as (O-RAN WG4 M-plane specification, Table 6.5-1): "
        "sudo (default) or hybrid-odu; writes the role may not perform are skipped",
    )
    add_rpc_log_argument(parser)

    parser.add_argument(
        "--datastore",
        type=str,
        default="running",
        help="Datastore to use",
    )
    parser.add_argument(
        "--profile",
        choices=("gnb", "cu", "cucp", "cuup", "du", "ru"),
        default="gnb",
        help=(
            "NETCONF YANG profile served by the upstream server. Selects the "
            "default template (<profile>.yaml) and, for 'ru', skips yaml "
            "rendering entirely (raw config is just forwarded downstream)."
        ),
    )
    parser.add_argument(
        "-t",
        "--template",
        type=str,
        default=None,
        help="Config template filename (default: <profile>.yaml)",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        default="/tmp/config.yaml",
        help="Config filename",
    )
    parser.add_argument(
        "-a",
        "--autoheal",
        type=bool,
        default=False,
        help="Whether to reset health after one health check",
    )
    # WS parameters
    parser.add_argument(
        "--ws_host",
        type=str,
        default="localhost",
        help="WebSocket host",
    )
    parser.add_argument(
        "--ws_port",
        type=int,
        default=8001,
        help="WebSocket port",
    )

    # VES parameters
    parser.add_argument(
        "--ves_host",
        type=str,
        default="localhost",
        help="VES host",
    )
    parser.add_argument(
        "--ves_port",
        type=int,
        default=8443,
        help="VES port",
    )
    parser.add_argument(
        "--ves_username",
        type=str,
        default="sample1",
        help="VES username",
    )
    parser.add_argument(
        "--ves_password",
        type=str,
        default="sample1",
        help="VES password",
    )
    parser.add_argument(
        "--ves_scheme",
        type=str,
        default="https",
        choices=["http", "https"],
        help="VES connection scheme (http or https)",
    )
    parser.add_argument(
        "--oam_ipv4_address",
        type=str,
        default="11.22.33.44",
        help="OAM IPv4 address",
    )
    parser.add_argument(
        "-r",
        "--registration",
        type=bool,
        default=False,
        help="Send PNF registration on startup",
    )

    # PTP monitor
    parser.add_argument(
        "--ptp_log",
        type=str,
        default="",
        help="Path to ptp4l log file to monitor (disabled if empty)",
    )
    parser.add_argument("--ptp_max_latency", type=int, default=120, help="Max PTP latency (ns) before raising alarm")
    parser.add_argument(
        "--ptp_max_consecutive", type=int, default=3, help="Number of consecutive breaches before raising alarm"
    )
    parser.add_argument(
        "--ptp_master_clear_consecutive",
        type=int,
        default=3,
        help="Number of consecutive good samples before clearing master->local alarm",
    )

    # Logging configuration
    parser.add_argument(
        "--loglevel",
        choices=(
            "CRITICAL",
            "FATAL",
            "ERROR",
            "WARN",
            "WARNING",
            "INFO",
            "DEBUG",
            "NOTSET",
        ),
        default="INFO",
        help="Log level",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Do I really need to explain?",
    )

    cmd_args = parser.parse_args()

    if cmd_args.template is None:
        # ru profile doesn't render a yaml; ConfigManager won't read this.
        cmd_args.template = f"{cmd_args.profile}.yaml"

    if cmd_args.ru_forward or cmd_args.ru_supervise:
        missing_ru_args = []
        # the flag carries a default, so only an explicitly emptied host is
        # rejected — and not with call-home, where the O-RU dials in
        if not cmd_args.ru_netconf_host and not cmd_args.ru_callhome:
            missing_ru_args.append("--ru_netconf_host")
        if not cmd_args.ru_netconf_username:
            missing_ru_args.append("--ru_netconf_username")
        if not cmd_args.ru_netconf_password:
            missing_ru_args.append("--ru_netconf_password")
        if missing_ru_args:
            parser.error(
                "Missing required RU NETCONF arguments when --ru_forward/--ru_supervise is set: "
                + ", ".join(missing_ru_args)
            )
        # o-ran-supervision models both timers as uint16 seconds
        if not 1 <= cmd_args.ru_supervision_interval <= 65535 or not 0 <= cmd_args.ru_supervision_guard <= 65535:
            parser.error(
                "--ru_supervision_interval must be 1..65535 and --ru_supervision_guard 0..65535 "
                "(o-ran-supervision uint16 seconds)"
            )

    if cmd_args.ru_callhome:
        if not cmd_args.ru_supervise:
            parser.error("--ru_callhome requires --ru_supervise (the call-home listener is the M-plane session's)")
        if not 1 <= cmd_args.ru_callhome_port <= 65535:
            parser.error("--ru_callhome_port must be 1..65535 (a TCP port)")

    if cmd_args.ru_role is not None:
        if not cmd_args.ru_supervise:
            parser.error("--ru_role requires --ru_supervise (the role is the M-plane session's account group)")
        if cmd_args.ru_role != ROLE_SUDO and cmd_args.ru_forward:
            parser.error(
                f"--ru_role {cmd_args.ru_role} cannot be combined with --ru_forward (the forwarder writes as sudo)"
            )

    cmd_args.ru_provision = load_ru_provisioning(parser, cmd_args)

    logging.basicConfig(
        format="%(asctime)s \x1b[32;20m[%(levelname)s]\x1b[0m %(message)s",
        level=cmd_args.loglevel,
    )
    logging.info("OCUDU O1 adapter")

    if cmd_args.rpc_log:
        enable_rpc_log(cmd_args.rpc_log)
        logging.info("Recording the NETCONF conversation to %s", cmd_args.rpc_log)
    else:
        # Reduce ncclient verbosity
        logger = logging.getLogger("ncclient")
        logger.setLevel(logging.WARNING)

    restrict_ssh_algorithms()

    cmd_args.netconf_ssh_config = None
    if cmd_args.netconf_hostkey_verify and cmd_args.netconf_tls:
        # Helm passes both flags whenever a host key secret exists, but connect_tls has no host
        # key to check.
        logging.info("--netconf_hostkey_verify ignored: --netconf_tls authenticates the server by certificate")
    elif cmd_args.netconf_hostkey_verify:
        if not os.access(cmd_args.netconf_known_hosts, os.R_OK):
            parser.error(f"--netconf_known_hosts '{cmd_args.netconf_known_hosts}' is missing or unreadable")
        # ncclient narrows the transport to the algorithm names known_hosts yielded, and an RSA
        # key is recorded as 'ssh-rsa' while the server offers only rsa-sha2-256/512 - so key
        # exchange would fail with "no acceptable host key", naming neither RSA nor this file.
        with open(cmd_args.netconf_known_hosts, encoding="utf-8") as known_hosts:
            for line in known_hosts:
                fields = line.split()
                if len(fields) >= 3 and fields[1] == "ssh-rsa":
                    parser.error(
                        f"--netconf_known_hosts '{cmd_args.netconf_known_hosts}' records an RSA host key for "
                        f"{fields[0]}, which cannot be verified. Provision an ed25519 or ecdsa host key instead."
                    )
        with open(NETCONF_SSH_CONFIG, "w", encoding="utf-8") as ssh_config:
            ssh_config.write(f"Host *\n    UserKnownHostsFile {cmd_args.netconf_known_hosts}\n")
        cmd_args.netconf_ssh_config = NETCONF_SSH_CONFIG
        logging.info("Verifying the NETCONF server host key against %s", cmd_args.netconf_known_hosts)

    ves = VesMessages(
        host=cmd_args.ves_host,
        port=cmd_args.ves_port,
        username=cmd_args.ves_username,
        password=cmd_args.ves_password,
        scheme=cmd_args.ves_scheme,
        logging=logging,
    )
    if cmd_args.registration:
        ves.send_pnf_registration()

    # Simple stdout notifier
    def alarm_notifier(evt: AlarmEvent) -> None:
        """Simple alarm notifier that logs state changes and sends to VES."""
        logging.info(
            f"{'ACTIVE' if evt.became_active else 'CLEARED'} "
            f"{evt.old_severity.name} -> {evt.new_severity.name} "
            f"trend={evt.trend.name} msg={evt.message or '-'}"
        )

        ves.send_alarm(
            alarm_id=evt.alarm_id,
            alarm=evt.name,
            alarm_type=evt.alarm_type,
            severity=evt.new_severity.name,
        )

    alarms = AlarmManager(
        AlarmDefinitions.defs,
        notifier=alarm_notifier,
    )

    # Let's go
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    try:
        # Run NETCONF + WebSocket together
        asyncio.run(orchestrator(cmd_args, alarms))
    except KeyboardInterrupt:
        logging.info("Exiting...")
