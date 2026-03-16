#!/usr/bin/python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
This module is a stand-alone application for configuring ORAN radio units over Mplane.

Usage:
    This module can be executed as a standalone script.
"""
import argparse
import logging
import sys
from pathlib import Path
import xml.dom.minidom
import xml.etree.ElementTree as ET
from xml.parsers.expat import ExpatError

import xmltodict
from jinja2 import Environment, FileSystemLoader
from ncclient import manager
from ncclient.operations import rpc as rpc_ops
from ncclient.transport import errors as transport_errors

from ofh_config_builder import build_ofh_config, print_ofh_config


class RuConfig:
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
            except (ConnectionError, TimeoutError) as e:
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

    def set_oran_uplane_tdd_7d1s2u_slot_6_4_4(self):
        """Set ORAN U-plane TDD to 7d1s2u."""
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
                logging.error("Failed to render srsRAN OFH config snippet: %s", err)

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="srsRAN Enterprise O-RU controller.")

    # Netconf configs
    parser.add_argument("--host", type=str, default="localhost", help="The device IP or DN")
    parser.add_argument("-u", "--username", type=str, default="root", help="SSH user")
    parser.add_argument("-p", "--password", type=str, default="M1!T1!mt", help="SSH pass")
    parser.add_argument("--port", type=int, default=830, help="Specify this if you want a non-default port")
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
    logging.info("srsRAN mplane controller")

    # Reduce ncclient verbosity
    logger = logging.getLogger("ncclient")
    logger.setLevel(logging.WARNING)

    session = None  # pylint: disable=invalid-name,duplicate-code
    if not args.dry_run:
        # Let's go
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

    if session is not None:
        session.close_session()
