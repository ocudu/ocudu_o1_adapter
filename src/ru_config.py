# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
This module provides the RuConfig class that configures an O-RU over NETCONF/Mplane.

It is a library module shared by the stand-alone RU controller CLI and by the O1 adapter's
--ru_forward path.
"""

import logging
import sys
import xml.dom.minidom
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.parsers.expat import ExpatError

import xmltodict
from jinja2 import Environment, FileSystemLoader
from ncclient.operations import rpc as rpc_ops
from ncclient.operations.errors import TimeoutExpiredError
from ncclient.transport import errors as transport_errors
from ncclient.xml_ import to_ele

from ofh_config_builder import build_ofh_config, print_ofh_config
from xml_utils import describe_rpc_errors


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
        """Push one edit-config to the O-RU.

        A rejected edit raises the ncclient RPCError after its rpc-errors are
        logged; connection, reply-timeout and transport failures are logged
        once and re-raised. A dry run only logs the payload.
        """
        logging.info("Editing %s", description)
        logging.debug("%s", xml_request)
        if not self.dry_run:
            try:
                self.netconf_manager.edit_config(
                    config=xml_request, format="xml", target=self.datastore, default_operation=self.operation
                )
            except rpc_ops.RPCError as e:
                for line in describe_rpc_errors(e):
                    logging.error("NETCONF RPC error editing %s: %s", description, line)
                raise
            except (ConnectionError, TimeoutError, TimeoutExpiredError, transport_errors.TransportError) as e:
                logging.error("Error editing %s: %s", description, e)
                raise

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
