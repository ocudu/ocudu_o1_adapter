#!/usr/bin/python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
This module is a stand-alone application for configuring ORAN radio units over Mplane.

The RuConfig class it drives lives in ru_config, shared with the O1 adapter's --ru_forward path;
this module owns the command-line front end.

Usage:
    This module can be executed as a standalone script.
"""

import argparse
import errno
import logging
import sys
import time

from ncclient import manager
from ncclient.transport import errors as transport_errors

from ru_config import RuConfig
from ssh_algorithms import restrict_ssh_algorithms

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OCUDU O-RU controller.")

    # Netconf configs
    parser.add_argument("--host", type=str, default="localhost", help="The device IP or DN")
    parser.add_argument("-u", "--username", type=str, default="root", help="SSH user")
    parser.add_argument("-p", "--password", type=str, default="M1!T1!mt", help="SSH pass")
    parser.add_argument("--port", type=int, default=830, help="Specify this if you want a non-default port")
    parser.add_argument(
        "--callhome",
        action="store_true",
        help="Wait for an inbound NETCONF call-home (RFC 8071) instead of connecting out",
    )
    parser.add_argument("--callhome-port", type=int, default=4334, help="TCP port to listen on for call-home")
    parser.add_argument("--callhome-bind", type=str, default="0.0.0.0", help="Address to bind the call-home listener")
    parser.add_argument("-d", "--datastore", type=str, default="running", help="Datastore to use")
    parser.add_argument("--get_config", action="store_true", help="Get current RU config")
    parser.add_argument("--set_full_config", action="store_true", help="Set full RU config")

    # IETF interface config
    parser.add_argument("--set_interface", action="store_true", help="Set IETF interface config")
    parser.add_argument("--ru_mac_addr", type=str, default="aa:bb:cc:dd:ee:ff", help="RU MAC address")
    parser.add_argument("--vlan", type=int, default=127, help="C and U plane VLAN")

    # Processing elements
    parser.add_argument("--set_proc_elem", action="store_true", help="Set ORAN processing elements")
    parser.add_argument("--du_mac_addr", type=str, default="00:11:22:33:44:55", help="DU MAC address")

    # Endpoint configuration
    parser.add_argument("--set_endpoints", action="store_true", help="Set ORAN Uplane Tx/Rx endpoints")
    parser.add_argument("--iq_bitwidth", type=int, default=9, help="BFP compression bit width")
    parser.add_argument("--compression_type", type=str, default="STATIC", help="Compression type")
    parser.add_argument(
        "--rf_bandwidth_hz", type=int, default=100000000, help="Channel bandwidth in Hz (for DL and UL)"
    )

    # Carrier configuration
    parser.add_argument("--set_carriers", action="store_true", help="Configure RF carriers")
    parser.add_argument("--dl_arfcn", type=int, default=640000, help="DL ARFCN")
    parser.add_argument("--dl_freq", type=int, default=3600000000, help="DL frequency in Hz")
    parser.add_argument("--tx_gain", type=float, default=27.0, help="Tx gain")
    parser.add_argument("--ul_arfcn", type=int, default=640000, help="DL ARFCN")
    parser.add_argument("--ul_freq", type=int, default=3600000000, help="UL frequency in Hz")

    # Carrier activation
    parser.add_argument("--activate_carriers", action="store_true", help="Whether to apply Tx/Rx carriers are active")
    parser.add_argument(
        "--carrier_state", choices=["ACTIVE", "INACTIVE"], default="ACTIVE", help="Whether Tx/Rx carriers are active"
    )

    # Supervision watchdog keep-alive
    parser.add_argument(
        "--supervise",
        action="store_true",
        help="Keep the O-RU supervision session alive by resetting its watchdog on each supervision-notification",
    )
    parser.add_argument(
        "--supervision-interval", type=int, default=60, help="supervision-notification-interval in seconds"
    )
    parser.add_argument("--supervision-guard", type=int, default=10, help="guard-timer-overhead in seconds")

    # Misc arguments
    parser.add_argument("--dry-run", action="store_true", help="Just print config but don't apply")
    parser.add_argument(
        "--log-level",
        choices=("CRITICAL", "FATAL", "ERROR", "WARN", "WARNING", "INFO", "DEBUG", "NOTSET"),
        default="INFO",
        help="Log level",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Do I really need to explain?")

    args = parser.parse_args()

    logging.basicConfig(format="%(asctime)s \x1b[32;20m[%(levelname)s]\x1b[0m %(message)s", level=args.log_level)
    logging.info("OCUDU mplane controller")

    # Reduce ncclient verbosity
    logger = logging.getLogger("ncclient")
    logger.setLevel(logging.WARNING)

    restrict_ssh_algorithms()

    session = None  # pylint: disable=invalid-name,duplicate-code
    if not args.dry_run:
        # Let's go
        if args.callhome:
            logging.info("Waiting for NETCONF call-home on %s:%d", args.callhome_bind, args.callhome_port)
            while True:
                try:
                    session = manager.call_home(
                        host=args.callhome_bind,
                        port=args.callhome_port,
                        username=args.username,
                        password=args.password,
                        hostkey_verify=False,
                        look_for_keys=False,
                        allow_agent=False,
                        # Block in accept() until the O-RU calls home. ncclient defaults to a
                        # 10s accept timeout, shorter than the 60s default re-call-home timer.
                        timeout=None,
                    )
                    break
                except OSError as e:
                    # manager.call_home() does not set SO_REUSEADDR and leaves the listening
                    # socket open on failure, so a retry can hit EADDRINUSE until the previous
                    # socket is released (ncclient issue #578). Wait briefly and retry; any
                    # other OSError is unexpected and is re-raised.
                    if e.errno != errno.EADDRINUSE:
                        raise
                    logging.debug("call-home port still in use (%s), retrying", e)
                    time.sleep(1)
                    continue
        else:
            try:
                session = manager.connect(
                    host=args.host,
                    port=args.port,
                    username=args.username,
                    password=args.password,
                    hostkey_verify=False,
                    look_for_keys=False,
                    allow_agent=False,
                )  # pylint: enable=duplicate-code
                logging.info("Connected to %s:%d as %s", args.host, args.port, args.username)
            except transport_errors.AuthenticationError as e:
                logging.error("Authentication failed for user '%s': %s", args.username, e)
                sys.exit(1)
            except transport_errors.SSHUnknownHostError as e:
                logging.error(
                    "Unknown host key for %s — add it to known_hosts or use hostkey_verify=False: %s", args.host, e
                )
                sys.exit(1)
            except transport_errors.SessionCloseError as e:
                logging.error("Session closed unexpectedly while connecting to RU: %s", e)
                sys.exit(1)
            except transport_errors.SSHError as e:
                logging.error("SSH error while connecting to %s:%d: %s", args.host, args.port, e)
                sys.exit(1)
            except (ConnectionError, TimeoutError) as e:
                logging.error("Couldn't connect to sysrepo on RU: %s", e)
                sys.exit(1)

    ru_controller = RuConfig(session, args.datastore)

    if args.get_config:
        ru_controller.get_full_config()

    # Enable all base configs
    if args.set_full_config:
        args.set_interface = True
        args.set_proc_elem = True
        args.set_endpoints = True
        args.set_carriers = True
        args.activate_carriers = True

    if args.set_interface:
        ietf_interface_config = {"ru_mac_addr": args.ru_mac_addr, "vlan": args.vlan}
        ru_controller.set_ietf_interfaces(ietf_interface_config)

    if args.set_proc_elem:
        oran_processing_config = {"ru_mac_addr": args.ru_mac_addr, "du_mac_addr": args.du_mac_addr, "vlan": args.vlan}
        ru_controller.set_oran_processing_elements(oran_processing_config)

    if args.set_endpoints:

        def _get_num_prb(rf_bandwidth_mhz):
            prb_lookup = {100: 273, 80: 217, 40: 106, 20: 51, 10: 24}
            try:
                return prb_lookup[rf_bandwidth_mhz]
            except KeyError:
                logging.error("Unsupported RF bandwidth: %s MHz", rf_bandwidth_mhz)
                return None

        # TODO: verify frame structure values
        def _get_frame_struct(rf_bandwidth_mhz):
            if rf_bandwidth_mhz == 100:
                return 193
            if rf_bandwidth_mhz == 40:
                return 177
            if rf_bandwidth_mhz == 20:
                return 161
            if rf_bandwidth_mhz == 10:
                return 145
            return None

        uplane_endpoint_config = {
            "iq_bitwidth": args.iq_bitwidth,
            "compression_type": args.compression_type,
            "num_prb": _get_num_prb(args.rf_bandwidth_hz / 1e6),
            "frame_structure": _get_frame_struct(args.rf_bandwidth_hz / 1e6),
        }
        ru_controller.set_oran_uplane_tx_endpoints(uplane_endpoint_config)
        ru_controller.set_oran_uplane_rx_endpoints(uplane_endpoint_config)

    if args.set_carriers:
        uplane_carrier_config = {
            "dl_arfcn": args.dl_arfcn,
            "dl_freq": args.dl_freq,
            "ul_arfcn": args.ul_arfcn,
            "ul_freq": args.ul_freq,
            "tx_gain": args.tx_gain,
            "rf_bandwidth_hz": args.rf_bandwidth_hz,
        }
        ru_controller.set_oran_uplane_tx_array_carriers(uplane_carrier_config)
        ru_controller.set_oran_uplane_rx_array_carriers(uplane_carrier_config)
        ru_controller.set_oran_uplane_low_level_tx_links()
        ru_controller.set_oran_uplane_low_level_rx_links()
        ru_controller.set_oran_uplane_tdd_7d1s2u_slot_6_4_4()

    if args.activate_carriers:
        carrier_activation_config = {"state": args.carrier_state}
        ru_controller.set_oran_uplane_carrier_active(carrier_activation_config)

    if args.supervise:
        ru_controller.supervise(args.supervision_interval, args.supervision_guard)

    if session is not None:
        session.close_session()
