#!/usr/bin/python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
This module provides an O1 adapter for srsRAN, which manages and updates the configuration of a gNB / CU / DU .
It includes functionalities for retrieving configurations, detecting changes,
updating runtime configurations and triggering full restarts if necessary.

Usage:
    This module can be executed as a standalone script.
"""

# pylint: disable=logging-fstring-interpolation

import argparse
import asyncio
import copy
import io
import json
import logging
import threading
import xml.etree.ElementTree as ET
from contextlib import suppress

import websockets
from flask import Flask, jsonify
from ncclient import manager
from ncclient.transport.errors import AuthenticationError, SessionCloseError, SSHError

from alarm_defs import AlarmDefinitions
from alarm_manager import AlarmEvent, AlarmManager
from config_manager import ConfigManager
from ptp_monitor import ptp_health_checker_consumer, ptp_log_monitor
from ru_controller import RuConfig
from state import AppState
from ves import VesMessages

# Flask app
app = Flask(__name__)

RETRY_INTERVAL = 5  # seconds
WORKER_SLEEP_INTERVAL = 5  # seconds
SYNC_ALLOWED_NAMESPACES = {
    "urn:ietf:params:xml:ns:netconf:base:1.0",
    "urn:ietf:params:xml:ns:yang:iana-if-type",
    "urn:ietf:params:xml:ns:yang:ietf-interfaces",
    "urn:o-ran:interfaces:1.0",
    "urn:o-ran:performance-management:1.0",
    "urn:o-ran:processing-element:1.0",
    "urn:o-ran:uplane-conf:1.0",
}


# TODO: WS message handler processing
async def handle_ws_message(msg: str):
    """
    Dispatch WS messages by component type.
    """
    try:
        data = json.loads(msg)
    except json.JSONDecodeError:
        logging.error(f"WS non-JSON message: {msg}")
        return

    # Handle command return values
    if data.get("cmd"):
        logging.debug(f"WS command response: {data}")
        return

    cu_cp_metrics = data.get("cu-cp", {})
    if cu_cp_metrics:
        logging.debug(f"WS cu-cp metrics: {cu_cp_metrics}")
        # TODO: check for AMF connection status

    if data.get("du"):
        logging.debug(f"WS du metrics: {data['du']}")

    if data.get("cells"):
        logging.debug(f"WS cell metrics: {data['cells']}")


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
        # run blocking ncclient call in a background thread
        def connect():
            # pylint: disable=duplicate-code
            m = manager.connect(
                host=args.netconf_host,
                port=args.netconf_port,
                username=args.netconf_username,
                password=args.netconf_password,
                hostkey_verify=False,
                allow_agent=False,
                look_for_keys=False,
                timeout=RETRY_INTERVAL,
            )
            # pylint: enable=duplicate-code
            logging.info("Connected to NETCONF server")
            alarm_mgr.clear_alarm(
                1001,
                message="NETCONF connection restored",
            )
            return m

        return await asyncio.to_thread(connect)

    except (
        SSHError,
        AuthenticationError,
        SessionCloseError,
    ) as e:
        logging.warning(f"NETCONF connection failed: {e}")
        alarm_mgr.set_alarm(
            1001,
            message="NETCONF connection lost",
        )
        return None


async def netconf_main(state: AppState, args, alarm_mgr):
    """
    Main loop for managing the NETCONF connection.
    """
    startup_ru_sync_done = False
    while True:
        netconf_session = await try_connect(args, alarm_mgr)
        if netconf_session:
            state.session_state["nc_connected"] = True
            stop_event = asyncio.Event()

            if args.ru_forward and not startup_ru_sync_done:
                await sync_source_netconf_from_ru(netconf_session, args, alarm_mgr)
                startup_ru_sync_done = True

            writer = ConfigManager(
                state,
                netconf_session,
                args.datastore,
                args.config,
                args.template,
                args.ru_forward,
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


def _data_xml_to_edit_config_xml(data_xml, include_namespaces=None):
    """Convert NETCONF <data> payload to NETCONF edit-config <config> payload."""
    data_root, prefixed_ns = _parse_data_xml(data_xml)
    children = list(data_root)
    if include_namespaces is not None:
        allowed_namespaces = set(include_namespaces)
        children = [child for child in children if _extract_xml_namespace(child.tag) in allowed_namespaces]

    if not children:
        raise ValueError("No top-level config nodes left after filters")

    return _build_edit_config_xml(children, prefixed_ns)


def _parse_data_xml(data_xml):
    """Parse NETCONF payload and return resolved <data> root with prefixed namespaces."""
    prefixed_ns = {}
    for _, (prefix, uri) in ET.iterparse(io.StringIO(data_xml), events=("start-ns",)):
        if prefix and prefix not in prefixed_ns:
            prefixed_ns[prefix] = uri

    source_root = ET.fromstring(data_xml)
    source_local_name = source_root.tag.split("}", maxsplit=1)[-1]

    if source_local_name == "data":
        return source_root, prefixed_ns

    if source_local_name == "rpc-reply":
        for child in list(source_root):
            if child.tag.split("}", maxsplit=1)[-1] == "data":
                return child, prefixed_ns
        raise ValueError("Expected NETCONF <data> inside <rpc-reply>")

    raise ValueError(f"Expected NETCONF <data> root, got <{source_local_name}>")


def _extract_xml_namespace(tag):
    """Extract namespace URI from ElementTree tag."""
    if isinstance(tag, str) and tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", maxsplit=1)[0]
    return ""


def _apply_prefixed_namespaces(root, prefixed_ns):
    """Attach prefixed namespace declarations to XML root element."""
    for prefix, uri in prefixed_ns.items():
        root.set(f"xmlns:{prefix}", uri)


def _build_edit_config_xml(children, prefixed_ns):
    """Build NETCONF <config> payload from top-level child elements."""
    netconf_ns = "urn:ietf:params:xml:ns:netconf:base:1.0"
    config_root = ET.Element(f"{{{netconf_ns}}}config")
    _apply_prefixed_namespaces(config_root, prefixed_ns)
    for child in children:
        config_root.append(copy.deepcopy(child))
    return ET.tostring(config_root, encoding="unicode")


async def sync_source_netconf_from_ru(source_session, args, alarm_mgr):
    """
    One-time bootstrap: apply RU NETCONF running config to source NETCONF server.
    """
    logging.info("Starting one-time RU -> source NETCONF startup sync")
    ru_session = await try_connect_ru(args, alarm_mgr)
    if ru_session is None:
        logging.warning("Startup sync skipped: unable to connect RU NETCONF server")
        return False

    try:
        ru_data_xml = await asyncio.to_thread(lambda: ru_session.get_config(source=args.ru_datastore).data_xml)
        ru_data_root, prefixed_ns = _parse_data_xml(ru_data_xml)
        attempted = 0
        applied = 0
        skipped = 0
        for child in list(ru_data_root):
            ns = _extract_xml_namespace(child.tag)
            local = child.tag.split("}", maxsplit=1)[-1]
            if ns not in SYNC_ALLOWED_NAMESPACES:
                skipped += 1
                logging.debug(f"Startup selective sync skipped '{local}' due to namespace filter '{ns}'")
                continue

            attempted += 1
            try:
                await asyncio.to_thread(
                    source_session.edit_config,
                    config=_build_edit_config_xml([child], prefixed_ns),
                    format="xml",
                    target=args.datastore,
                    default_operation="merge",
                )
                applied += 1
            except Exception as child_err:  # pylint: disable=broad-exception-caught
                logging.warning(f"Startup selective sync failed for '{local}': {child_err}")

        if applied:
            logging.info(
                f"Startup selective sync completed: applied {applied}/{attempted} top-level config element(s), "
                f"skipped {skipped} filtered namespace element(s)"
            )
            return True
        logging.warning(
            f"Startup selective sync did not apply any config (attempted={attempted}, skipped={skipped})"
        )
        return False
    except (ValueError, ET.ParseError) as e:
        logging.warning(f"Startup sync failed while converting RU NETCONF payload: {e}")
        return False
    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.warning(f"Startup sync failed while applying RU NETCONF payload: {e}")
        return False
    finally:
        with suppress(Exception):
            ru_session.close_session()


async def try_connect_ru(args, alarm_mgr):
    """
    Try to connect to the RU NETCONF server once.
    Returns manager instance if successful, None otherwise
    """
    try:
        # run blocking ncclient call in a background thread
        def connect():
            # pylint: disable=duplicate-code
            m = manager.connect(
                host=args.ru_netconf_host,
                port=args.ru_netconf_port,
                username=args.ru_netconf_username,
                password=args.ru_netconf_password,
                hostkey_verify=False,
                allow_agent=False,
                look_for_keys=False,
                # timeout=RETRY_INTERVAL,
            )
            # pylint: enable=duplicate-code
            logging.info("Connected to RU NETCONF server")
            alarm_mgr.clear_alarm(
                1003,
                message="RU NETCONF connection restored",
            )
            return m

        return await asyncio.to_thread(connect)

    except (
        SSHError,
        AuthenticationError,
        SessionCloseError,
    ) as e:
        logging.warning(f"RU NETCONF connection failed: {e}")
        alarm_mgr.set_alarm(
            1003,
            message="RU NETCONF connection lost",
        )
        return None


async def ru_forwarder_main(state: AppState, args, alarm_mgr):
    """Main loop for forwarding source NETCONF updates to the RU NETCONF server."""
    while True:
        ru_session = await try_connect_ru(args, alarm_mgr)
        if ru_session:
            state.session_state["ru_nc_connected"] = True
            ru_config = RuConfig(ru_session, args.ru_datastore)

            while ru_session.connected:
                try:
                    data_xml = await asyncio.wait_for(state.ru_update_queue.get(), timeout=1)
                except asyncio.TimeoutError:
                    continue

                try:
                    edit_payload = _data_xml_to_edit_config_xml(
                        data_xml,
                        include_namespaces=SYNC_ALLOWED_NAMESPACES,
                    )
                    await asyncio.to_thread(ru_config.edit_config, edit_payload, "Forwarded NETCONF config")
                    logging.info("Forwarded NETCONF update to RU")
                except (ValueError, ET.ParseError) as e:
                    logging.warning(f"Skipping RU forward update due to payload conversion error: {e}")
                except Exception as e:  # pylint: disable=broad-exception-caught
                    logging.warning(f"Failed forwarding NETCONF update to RU: {e}")
                    if not ru_session.connected:
                        break
                finally:
                    state.ru_update_queue.task_done()

            with suppress(Exception):
                ru_session.close_session()

        if state.session_state.get("ru_nc_connected"):
            state.session_state["ru_nc_connected"] = False

        alarm_mgr.set_alarm(
            1003,
            message="RU NETCONF connection lost",
        )
        logging.debug(f"Retrying RU NETCONF forwarder in {RETRY_INTERVAL} seconds...")
        await asyncio.sleep(RETRY_INTERVAL)


async def ws_handler(state: AppState, args, alarm_mgr):
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
                            await handle_ws_message(msg)
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

                sender_task = asyncio.create_task(sender())
                receiver_task = asyncio.create_task(receiver())
                keepalive_task = asyncio.create_task(keepalive())

                done, pending = await asyncio.wait(
                    [sender_task, receiver_task, keepalive_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in pending:
                    task.cancel()

                for task in pending:
                    with suppress(asyncio.CancelledError):
                        await task

                for task in done:
                    exc = task.exception()
                    if exc:
                        raise exc

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


async def orchestrator(args, alarm_mgr):
    """Orchestrator: run NETCONF + WebSocket tasks."""
    # Create shared state
    state = AppState()

    configure_app(state, args.autoheal)

    await asyncio.gather(
        netconf_main(state, args, alarm_mgr),
        ru_forwarder_main(state, args, alarm_mgr) if args.ru_forward else asyncio.sleep(0),
        ws_handler(state, args, alarm_mgr),
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
    parser = argparse.ArgumentParser(description="srsRAN Enterprise O1 adapter.")

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
        "--datastore",
        type=str,
        default="running",
        help="Datastore to use",
    )
    parser.add_argument(
        "-t",
        "--template",
        type=str,
        default="gnb.yaml",
        help="Config filename",
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

    if cmd_args.ru_forward:
        missing_ru_args = []
        if not cmd_args.ru_netconf_host:
            missing_ru_args.append("--ru_netconf_host")
        if not cmd_args.ru_netconf_username:
            missing_ru_args.append("--ru_netconf_username")
        if not cmd_args.ru_netconf_password:
            missing_ru_args.append("--ru_netconf_password")
        if missing_ru_args:
            parser.error("Missing required RU NETCONF arguments when --ru_forward is set: " + ", ".join(missing_ru_args))

    logging.basicConfig(
        format="%(asctime)s \x1b[32;20m[%(levelname)s]\x1b[0m %(message)s",
        level=cmd_args.loglevel,
    )
    logging.info("srsRAN O1 adapter")

    # Reduce ncclient verbosity
    logger = logging.getLogger("ncclient")
    logger.setLevel(logging.WARNING)

    ves = VesMessages(
        host=cmd_args.ves_host,
        port=cmd_args.ves_port,
        username=cmd_args.ves_username,
        password=cmd_args.ves_password,
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
