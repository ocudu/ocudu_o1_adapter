#!/usr/bin/python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
This module is a stand-alone application for configuring ORAN radio units over Mplane.

Usage:
    This module can be executed as a standalone script.
"""

import argparse
import errno
import logging
import sys
import time
import xml.dom.minidom
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.parsers.expat import ExpatError

import xmltodict
from jinja2 import Environment, FileSystemLoader
from ncclient import manager
from ncclient.operations import rpc as rpc_ops
from ncclient.transport import errors as transport_errors
from ncclient.xml_ import to_ele

from ofh_config_builder import build_ofh_config, print_ofh_config


def _extract_bad_element(rpc_error_info):
    """Extract the bad-element name from an RPCError's info field, or return None."""
    if not rpc_error_info:
        return None
    # ncclient parses error-info with xmltodict, so info is usually a dict
    if isinstance(rpc_error_info, dict):
        return rpc_error_info.get("bad-element")
    # Fall back to XML parsing when info arrives as a raw string
    if isinstance(rpc_error_info, str):
        try:
            root = ET.fromstring(rpc_error_info)
            el = root.find(".//{*}bad-element") or root.find(".//bad-element")
            return el.text if el is not None else None
        except ET.ParseError:
            return None
    return None


class RuConfig:  # pylint: disable=too-many-public-methods
    """
    A class for configuring ORAN radio units over NETCONF/Mplane interface.

    This class provides methods to configure various aspects of radio units including
    interfaces, processing elements, endpoints, carriers, and activation states.
    """

    def __init__(self, netconf_manager, datastore):
        self.netconf_manager = netconf_manager
        self.datastore = datastore
        self.operation = "merge"  # 'merge' or 'replace'
        self.dry_run = self.netconf_manager is None
        template_dir = Path(__file__).resolve().parent.parent / "templates" / "mplane"
        self._jinja_env = Environment(loader=FileSystemLoader(str(template_dir)))
        self._namespaces = {
            "urn:ietf:params:xml:ns:netconf:base:1.0": None,
            "urn:o-ran:uplane-conf:1.0": None,
            "urn:o-ran:processing-element:1.0": None,
            "urn:ietf:params:xml:ns:yang:ietf-interfaces": None,
            "urn:o-ran:interfaces:1.0": None,
            "urn:ietf:params:xml:ns:yang:ietf-hardware": None,
            "urn:o-ran:sync:1.0": None,
        }

    def edit_config(self, xml_request, description="XML config"):
        """Edit configuration via NETCONF."""
        logging.info("Editing %s", description)
        logging.debug("%s", xml_request)
        if not self.dry_run:
            try:
                self.netconf_manager.edit_config(
                    config=xml_request, format="xml", target=self.datastore, default_operation=self.operation
                )
            except rpc_ops.RPCError as e:
                details = []
                if e.path:
                    details.append(f"path: {e.path}")
                bad_element = _extract_bad_element(e.info)
                if bad_element:
                    details.append(f"bad-element: {bad_element}")
                if details:
                    logging.error(
                        "NETCONF RPC error editing %s (%s) — %s",
                        description,
                        e.message or e.tag,
                        ", ".join(details),
                    )
                else:
                    logging.error("NETCONF RPC error editing %s: %s", description, e)
                sys.exit(1)
            except (ConnectionError, TimeoutError, transport_errors.SessionCloseError) as e:
                logging.error("Error occurred during operation: %s", e)
                sys.exit(1)

    def set_full_config(self, config_dict):
        """Set the complete configuration for the radio unit."""
        self.set_ietf_interfaces(config_dict["interface"])
        self.set_oran_processing_elements(config_dict["processing"])
        self.set_oran_uplane_tx_endpoints(config_dict["endpoint"])
        self.set_oran_uplane_rx_endpoints(config_dict["endpoint"])
        self.set_oran_uplane_tx_array_carriers(config_dict["carrier"])
        self.set_oran_uplane_rx_array_carriers(config_dict["carrier"])
        self.set_oran_uplane_low_level_tx_links()
        self.set_oran_uplane_low_level_rx_links()
        # self.set_oran_perf_measurement()
        self.set_oran_uplane_carrier_active(config_dict["activation"])
        self.set_oran_uplane_tdd_7d1s2u_slot_6_4_4()

    def _render_template(self, template_name, **kwargs):
        template = self._jinja_env.get_template(template_name)
        return template.render(**kwargs)

    def _set_config_from_template(self, template_name, description, config_data=None, **template_kwargs):
        if config_data is not None:
            template_kwargs["config"] = config_data
        xml_request = self._render_template(template_name, **template_kwargs)
        self.edit_config(xml_request, description)

    def set_ietf_interfaces(self, ietf_config):
        """Set IETF interfaces configuration."""
        self._set_config_from_template("ietf_interfaces.xml", "IETF interfaces", interface=ietf_config)

    def set_oran_processing_elements(self, proc_config):
        """Set ORAN processing elements configuration."""
        self._set_config_from_template("oran_processing_elements.xml", "ORAN processing elements", proc_config)

    def set_oran_uplane_tx_endpoints(self, tx_config):
        """Set ORAN U-plane TX endpoints configuration."""
        self._set_config_from_template("oran_uplane_tx_endpoints.xml", "ORAN Uplane Tx endpoints elements", tx_config)

    def set_oran_uplane_rx_endpoints(self, rx_config):
        """Set ORAN U-plane RX endpoints configuration."""
        self._set_config_from_template("oran_uplane_rx_endpoints.xml", "ORAN Uplane Rx endpoints elements", rx_config)

    def set_oran_uplane_tx_array_carriers(self, tx_carrier_config):
        """Set ORAN U-plane TX array carriers configuration."""
        self._set_config_from_template(
            "oran_uplane_tx_array_carriers.xml", "ORAN Uplane Tx array carriers", tx_carrier_config
        )

    def set_oran_uplane_rx_array_carriers(self, rx_carrier_config):
        """Set ORAN U-plane RX array carriers configuration."""
        self._set_config_from_template(
            "oran_uplane_rx_array_carriers.xml", "ORAN Uplane Rx array carriers", rx_carrier_config
        )

    def set_oran_uplane_low_level_tx_links(self):
        """Set ORAN U-plane low level TX links configuration."""
        self._set_config_from_template("oran_uplane_low_level_tx_links.xml", "ORAN Uplane low level Tx links")

    def set_oran_uplane_low_level_rx_links(self):
        """Set ORAN U-plane low level RX links configuration."""
        self._set_config_from_template("oran_uplane_low_level_rx_links.xml", "ORAN Uplane low level Rx links")

    def set_oran_perf_measurement(self):
        """Set ORAN performance measurement configuration."""
        self._set_config_from_template("oran_perf_measurement.xml", "ORAN Performance measurments")

    def set_oran_uplane_carrier_active(self, active_config):
        """Set ORAN U-plane carrier activation configuration."""
        self._set_config_from_template("oran_uplane_carrier_active.xml", "ORAN Uplane carrier active", active_config)

    def _is_configurable_tdd_supported(self):
        """Return True if the O-RU advertises CONFIGURABLE-TDD-PATTERN-SUPPORTED.

        Feature support is read from ietf-yang-library (RFC 7895/8525), the
        standard way a NETCONF server reports the YANG modules and per-module
        features it implements. The feature is scoped to the module that
        declares it, o-ran-module-cap. o-ran-uplane-conf references it via
        if-feature mcap:CONFIGURABLE-TDD-PATTERN-SUPPORTED. modules-state is
        state data, so it is retrieved with <get>, not <get-config>.
        """
        if self.dry_run:
            return True
        module_name = "o-ran-module-cap"
        feature = "CONFIGURABLE-TDD-PATTERN-SUPPORTED"
        yang_library_filter = '<modules-state xmlns="urn:ietf:params:xml:ns:yang:ietf-yang-library"/>'
        try:
            result = self.netconf_manager.get(filter=("subtree", yang_library_filter))
        except (transport_errors.TransportError, rpc_ops.RPCError) as err:  # pragma: no cover - network errors
            logging.error("Failed to read ietf-yang-library modules-state: %s", err)
            return False
        namespaces = {"yanglib": "urn:ietf:params:xml:ns:yang:ietf-yang-library"}
        try:
            root = ET.fromstring(getattr(result, "xml", str(result)))
        except ET.ParseError as err:  # pragma: no cover - parsing failure
            logging.error("Unable to parse ietf-yang-library payload: %s", err)
            return False
        for module in root.findall(".//yanglib:module", namespaces):
            if module.findtext("yanglib:name", default="", namespaces=namespaces) != module_name:
                continue
            return any((feat.text or "").strip() == feature for feat in module.findall("yanglib:feature", namespaces))
        return False

    def set_oran_uplane_tdd_7d1s2u_slot_6_4_4(self):
        """Set ORAN U-plane TDD to 7d1s2u."""
        if not self._is_configurable_tdd_supported():
            logging.info("O-RU does not advertise CONFIGURABLE-TDD-PATTERN-SUPPORTED; skipping TDD u-plane config")
            return
        self._set_config_from_template("oran_uplane_tdd_7d1s2u_slot_6_4_4.xml", "ORAN Uplane configure TDD to 7d1s2u")

    def was_operation_successful(self, result):
        """Check if NETCONF operation was successful."""
        # Define the namespace
        namespaces = {"nc": "urn:ietf:params:xml:ns:netconf:base:1.0"}

        # Parse the XML
        root = ET.fromstring(result)

        # Check for success
        ok_element = root.find("nc:ok", namespaces)
        if ok_element is not None:
            print("NETCONF operation was successful.")
            return True

        print("NETCONF operation failed or returned a different response.")
        return False

    def get_uplane_config(self):
        """Get U-plane configuration from the radio unit."""
        uplane_filter = """<user-plane-configuration xmlns="urn:o-ran:uplane-conf:1.0"/>"""
        return self._get_and_print_config(uplane_filter, "U-plane configuration")

    def get_processing_elements(self):
        """Get processing elements configuration from the radio unit."""
        processing_filter = """<processing-elements xmlns="urn:o-ran:processing-element:1.0"/>"""
        return self._get_and_print_config(processing_filter, "processing elements")

    def get_ietf_interfaces(self):
        """Get IETF interfaces configuration from the radio unit."""
        interfaces_filter = """<interfaces xmlns="urn:ietf:params:xml:ns:yang:ietf-interfaces"/>"""
        return self._get_and_print_config(interfaces_filter, "IETF interfaces")

    def get_ietf_hardware(self):
        """Get IETF hardware configuration from the radio unit."""
        hardware_filter = """<hardware xmlns="urn:ietf:params:xml:ns:yang:ietf-hardware"/>"""
        return self._get_and_print_config(hardware_filter, "IETF hardware")

    def get_oran_sync(self):
        """Get ORAN sync configuration from the radio unit."""
        sync_filter = """<sync xmlns="urn:o-ran:sync:1.0"/>"""
        return self._get_and_print_config(sync_filter, "ORAN sync")

    def get_full_config(self):
        """Get the complete configuration from the radio unit."""
        if not self.dry_run:
            uplane_data = self.get_uplane_config() or {}
            processing_data = self.get_processing_elements() or {}
            interfaces_data = self.get_ietf_interfaces() or {}
            self.get_ietf_hardware()
            self.get_oran_sync()

            try:
                ofh_cell, cell_cfg = build_ofh_config(
                    uplane_data.get("user-plane-configuration", {}),
                    processing_data.get("processing-elements", {}),
                    interfaces_data.get("interfaces", {}),
                )
                if ofh_cell:
                    print_ofh_config(ofh_cell, cell_cfg)
            except (KeyError, TypeError, ValueError) as err:  # pragma: no cover - defensive
                logging.error("Failed to render OCUDU OFH config snippet: %s", err)

    def _get_and_print_config(self, filter_xml, description):
        """Fetch a NETCONF subtree, pretty print it and return the parsed dict."""
        try:
            result = self.netconf_manager.get_config(source=self.datastore, filter=("subtree", filter_xml))
        except (transport_errors.TransportError, rpc_ops.RPCError) as err:  # pragma: no cover - network errors
            logging.error("Failed to retrieve %s: %s", description, err)
            return {}

        xml_payload = getattr(result, "xml", str(result))
        try:
            logging.debug(xml.dom.minidom.parseString(xml_payload).toprettyxml())
        except ExpatError:  # pragma: no cover - pretty print failure
            logging.error("Failed to pretty print %s payload", description)
            logging.debug(xml_payload)

        try:
            parsed = xmltodict.parse(xml_payload, process_namespaces=True, namespaces=self._namespaces)
        except (ValueError, ExpatError) as err:  # pragma: no cover - parsing failure
            logging.debug("Unable to parse %s payload: %s", description, err)
            return {}
        return parsed.get("rpc-reply", {}).get("data", {})

    def _reset_supervision_watchdog(self, interval, guard):
        """Send an o-ran-supervision supervision-watchdog-reset RPC.

        interval maps to supervision-notification-interval and guard to
        guard-timer-overhead; the O-RU arms its watchdog for interval + guard
        seconds and returns next-update-at.
        """
        rendered = self._render_template(
            "oran_supervision_watchdog_reset.xml", config={"interval": interval, "guard": guard}
        )
        reply = self.netconf_manager.dispatch(to_ele(rendered))
        logging.debug("supervision-watchdog-reset reply: %s", getattr(reply, "xml", reply))

    def supervise(self, interval, guard):
        """Keep an O-RU supervision session alive, driven by notifications.

        Reset the O-RU watchdog each time a supervision-notification arrives, never on
        a fixed timer (a timer based reset would mask a real O-RU failure). No initial
        reset is sent: the O-RU supervises with its default timers and notifies on its
        own, so the client only reacts.
        """
        if self.dry_run:
            logging.info("Dry run: skipping supervision loop")
            return
        supervision_tag = "{urn:o-ran:supervision:1.0}supervision-notification"
        timeout = interval + guard
        try:
            self.netconf_manager.create_subscription()
            logging.info("Supervision started; waiting for supervision-notifications")
            while True:
                notification = self.netconf_manager.take_notification(block=True, timeout=timeout)
                if notification is None:
                    logging.warning("No supervision-notification within %ss; O-RU may be unresponsive", timeout)
                    continue
                if ET.fromstring(notification.notification_xml).find(".//" + supervision_tag) is None:
                    logging.debug("Ignoring non-supervision notification")
                    continue
                logging.info("supervision-notification received; resetting watchdog")
                self._reset_supervision_watchdog(interval, guard)
        except (transport_errors.TransportError, rpc_ops.RPCError) as err:
            logging.error("Supervision failed: %s", err)
            sys.exit(1)


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
                    )
                    break
                except TimeoutError:
                    # manager.call_home() uses a hard-coded 10s accept timeout, while the
                    # O-RU re-call-home timer defaults to 60s, so a single accept would
                    # usually time out first. Keep waiting for the O-RU to connect.
                    logging.debug("call-home accept timed out, still waiting for the O-RU")
                    continue
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
