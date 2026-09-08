# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
This module provides the RuConfig class that configures an O-RU over NETCONF/Mplane.

It is a library module shared by the stand-alone RU controller CLI and by the O1 adapter's
--ru_forward path.
"""

import logging
import re
import sys
import time
import xml.dom.minidom
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.parsers.expat import ExpatError

import xmltodict
from jinja2 import Environment, FileSystemLoader
from ncclient.operations import rpc as rpc_ops
from ncclient.operations.errors import OperationError, TimeoutExpiredError
from ncclient.transport import errors as transport_errors
from ncclient.xml_ import to_ele

from ofh_config_builder import (
    active_carrier_numerology,
    build_ofh_config,
    build_ofh_timing,
    compute_tdd_switching_points,
    endpoint_naming,
    normalize_endpoint_entries,
    parse_module_capabilities,
    print_ofh_config,
    validate_switching_points,
)
from xml_utils import describe_rpc_errors, ensure_list, with_defaults_mode


def _measurement_objects_in_error(err, candidates):
    """Measurement-object names from `candidates` the O-RU named in its rpc-error.

    Searches the error's message and <bad-element> (via describe_rpc_errors) plus
    str(err). Whole-word match so RX_ON_TIME does not also match RX_ON_TIME_C —
    '_' is a word character, so the \\b after RX_ON_TIME never falls inside
    RX_ON_TIME_C.
    """
    text = " ".join(describe_rpc_errors(err)) + " " + str(err)
    return [obj for obj in candidates if re.search(rf"\b{re.escape(obj)}\b", text)]


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
            "urn:o-ran:delay:1.0": None,
            "urn:o-ran:module-cap:1.0": None,
            "urn:ietf:params:xml:ns:yang:ietf-yang-library": None,
            "urn:o-ran:performance-management:1.0": None,
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

    def set_full_config(self, config_dict, skip_activation=False):
        """Set the complete configuration for the radio unit.

        The endpoint dict may carry explicit tx_endpoints/rx_endpoints/
        prach_endpoints lists ([{name, eaxc_id}]) addressing the O-RU's fixed
        endpoint names, or dl_port_id/ul_port_id/prach_port_id eAxC lists for
        generated names (defaults preserve the legacy 4x4 layout); entry and
        carrier counts follow the resolved endpoints. skip_activation=True
        leaves the carriers inactive so activation can be gated on sync (see
        activate_full_config).
        """
        endpoint_config = config_dict["endpoint"]
        plan = self._resolve_endpoints(endpoint_config)

        self.set_ietf_interfaces(config_dict["interface"])
        self.set_oran_processing_elements(config_dict["processing"])
        self.set_oran_uplane_tx_endpoints(endpoint_config)
        self.set_oran_uplane_rx_endpoints(endpoint_config)
        tx_carrier_config = dict(config_dict["carrier"])
        rx_carrier_config = dict(config_dict["carrier"])
        tx_carrier_config.setdefault("nof_carriers", len(plan["tx"]))
        rx_carrier_config.setdefault("nof_carriers", len(plan["rx"]))
        self.set_oran_uplane_tx_array_carriers(tx_carrier_config)
        self.set_oran_uplane_rx_array_carriers(rx_carrier_config)
        self.set_oran_uplane_low_level_tx_links(endpoint_names=[entry["name"] for entry in plan["tx"]])
        self.set_oran_uplane_low_level_rx_links(
            rx_endpoint_names=[entry["name"] for entry in plan["rx"]],
            prach_endpoint_names=[entry["name"] for entry in plan["prach"]],
        )
        # TDD before activation: patterns are validated when carriers
        # activate and apply only to carriers referencing them. Both steps
        # are optional per config: some firmware manages TDD from its own
        # device configuration and misbehaves on these writes. The pattern
        # itself comes from the tdd section (OCUDU tdd_ul_dl_cfg fields, or
        # raw switching_points for O-RUs whose validated boundaries differ),
        # taken as a complete spec — a pattern is a coherent whole, so the
        # canonical 7d1s2u (6/4/4) default applies only when the section
        # carries no pattern content at all.
        tdd_cfg = config_dict.get("tdd") or {}
        if tdd_cfg.get("pattern_upload", True):
            # binding is the carrier_binding step's job here, so the
            # binding-related keys never reach the pattern push
            excluded = ("pattern_upload", "carrier_binding", "nof_tx_carriers", "nof_rx_carriers")
            pattern_config = {key: value for key, value in tdd_cfg.items() if key not in excluded}
            if not pattern_config.keys() - {"tdd_pattern_id"}:
                pattern_config = {**self._DEFAULT_TDD_PATTERN, **pattern_config}
            self.set_oran_uplane_tdd_pattern(pattern_config)
        if tdd_cfg.get("carrier_binding", True):
            # same id the pattern push above used: the carriers must
            # reference the pattern that was uploaded, not the default
            self.bind_tdd_pattern_to_carriers(
                tdd_pattern_id=tdd_cfg.get("tdd_pattern_id", 1),
                nof_tx_carriers=len(plan["tx"]),
                nof_rx_carriers=len(plan["rx"]),
            )
        if not skip_activation:
            self.activate_full_config(config_dict)

    def activate_full_config(self, config_dict):
        """Apply a full-config dict's activation block to the carriers —
        split out so activation can wait for sync-state LOCKED (a WG4
        activation precondition).

        activation.tolerate_reply_timeout=true downgrades an unanswered
        activation edit from fatal to a warning: some O-RU NETCONF servers
        accept the edit but never reply when carriers are already active, and
        carrier state is asynchronous either way — the array-carriers state
        readback, not the edit reply, is the activation receipt.
        """
        plan = self._resolve_endpoints(config_dict["endpoint"])
        activation_config = dict(config_dict["activation"])
        tolerate_timeout = bool(activation_config.pop("tolerate_reply_timeout", False))
        activation_config.setdefault("nof_tx_carriers", len(plan["tx"]))
        activation_config.setdefault("nof_rx_carriers", len(plan["rx"]))
        try:
            self.set_oran_uplane_carrier_active(activation_config)
        except TimeoutExpiredError:
            if not tolerate_timeout:
                raise
            logging.warning(
                "No reply to the carrier-activation edit within the RPC timeout; "
                "tolerated (activation.tolerate_reply_timeout) — verify via the "
                "array-carriers state readback"
            )

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

    # Legacy 4x4 fronthaul layout, preserved as the default eAxC assignment
    _DEFAULT_DL_PORT_IDS = (0, 1, 2, 3)
    _DEFAULT_UL_PORT_IDS = (0, 1, 2, 3)
    _DEFAULT_PRACH_PORT_IDS = (6, 7, 8, 9)
    _DEFAULT_PRACH_FRAME_STRUCTURE = 129  # FFT 256 + 30 kHz
    _DEFAULT_PRACH_NUM_PRB = 12

    def _resolve_endpoints(self, endpoint_config):
        """Resolve the tx/rx/prach endpoint groups to {name, eaxc_id} lists.

        Endpoint names are device data — many O-RUs ship fixed static
        endpoints addressable only by their exact names, and those names are
        not always expressible as prefix+index (zero-padded, non-consecutive,
        type-blind). Explicit endpoint.tx_endpoints/rx_endpoints/
        prach_endpoints lists take precedence; otherwise names are generated
        from the port lists and naming prefixes (defaults preserve the legacy
        4x4 layout).
        """
        cfg = endpoint_config or {}
        naming = endpoint_naming(cfg)
        resolved = {}
        for group, explicit_key, ports_key, default_ports, prefix in (
            ("tx", "tx_endpoints", "dl_port_id", self._DEFAULT_DL_PORT_IDS, naming["tx"]),
            ("rx", "rx_endpoints", "ul_port_id", self._DEFAULT_UL_PORT_IDS, naming["rx"]),
            ("prach", "prach_endpoints", "prach_port_id", self._DEFAULT_PRACH_PORT_IDS, naming["prach"]),
        ):
            explicit = cfg.get(explicit_key)
            if explicit is not None:
                resolved[group] = normalize_endpoint_entries(explicit, f"endpoint.{explicit_key}")
            else:
                ports = list(cfg.get(ports_key) or default_ports)
                resolved[group] = [
                    {"name": f"{prefix}{naming['base'] + index}", "eaxc_id": port} for index, port in enumerate(ports)
                ]
        return resolved

    # o-ran-uplane-conf rx-array-carriers n-ta-offset is mandatory (units Tc).
    # 3GPP TS 38.133 Table 7.1.2-2 fixes n-TimingAdvanceOffset at 25600 for
    # FR1 FDD and for FR1 TDD without LTE-NR coexistence; 0 applies to FR1 FDD
    # with LTE-NR coexistence, 39936 to FR1 TDD with LTE-NR coexistence and
    # 13792 to FR2 — override via the carrier config (n_ta_offset).
    _DEFAULT_N_TA_OFFSET_TC = 25600

    def set_oran_uplane_tx_endpoints(self, tx_config):
        """Set ORAN U-plane TX endpoints configuration.

        tx_config may carry an explicit tx_endpoints list ([{name, eaxc_id}])
        addressing the O-RU's fixed endpoint names, or dl_port_id (eAxC ids;
        default 0-3) for generated names — one endpoint is written per entry.
        """
        if tx_config.get("num_prb") is None or tx_config.get("frame_structure") is None:
            raise ValueError("tx endpoint config requires num_prb and frame_structure")
        rendered = dict(tx_config)
        rendered["endpoints"] = self._resolve_endpoints(tx_config)["tx"]
        self._set_config_from_template("oran_uplane_tx_endpoints.xml", "ORAN Uplane Tx endpoints elements", rendered)

    def set_oran_uplane_rx_endpoints(self, rx_config):
        """Set ORAN U-plane RX endpoints configuration.

        rx_config may carry explicit rx_endpoints/prach_endpoints lists
        ([{name, eaxc_id}]) addressing the O-RU's fixed endpoint names, or
        ul_port_id (default 0-3) / prach_port_id (default 6-9) for generated
        names; prach_frame_structure and prach_num_prb set the PRACH frame
        parameters — one endpoint is written per entry.
        """
        if rx_config.get("num_prb") is None or rx_config.get("frame_structure") is None:
            raise ValueError("rx endpoint config requires num_prb and frame_structure")
        plan = self._resolve_endpoints(rx_config)
        rendered = dict(rx_config)
        rendered["endpoints"] = [
            {
                **entry,
                "frame_structure": rx_config["frame_structure"],
                "num_prb": rx_config["num_prb"],
                "ul_fft_sampling_offset": 8,
            }
            for entry in plan["rx"]
        ] + [
            {
                **entry,
                "frame_structure": rx_config.get("prach_frame_structure", self._DEFAULT_PRACH_FRAME_STRUCTURE),
                "num_prb": rx_config.get("prach_num_prb", self._DEFAULT_PRACH_NUM_PRB),
                "ul_fft_sampling_offset": 0,
            }
            for entry in plan["prach"]
        ]
        self._set_config_from_template("oran_uplane_rx_endpoints.xml", "ORAN Uplane Rx endpoints elements", rendered)

    def set_oran_uplane_tx_array_carriers(self, tx_carrier_config):
        """Set ORAN U-plane TX array carriers configuration (nof_carriers, default 4)."""
        count = int(tx_carrier_config.get("nof_carriers") or len(self._DEFAULT_DL_PORT_IDS))
        rendered = dict(tx_carrier_config)
        rendered["carrier_names"] = [f"Tx-Array-Carrier-{index:02d}" for index in range(count)]
        self._set_config_from_template("oran_uplane_tx_array_carriers.xml", "ORAN Uplane Tx array carriers", rendered)

    def set_oran_uplane_rx_array_carriers(self, rx_carrier_config):
        """Set ORAN U-plane RX array carriers configuration.

        rx_carrier_config may carry nof_carriers (default 4) and n_ta_offset
        in Tc units (default 25600; _DEFAULT_N_TA_OFFSET_TC lists the
        TS 38.133 Table 7.1.2-2 cases).
        """
        count = int(rx_carrier_config.get("nof_carriers") or len(self._DEFAULT_UL_PORT_IDS))
        rendered = dict(rx_carrier_config)
        rendered.setdefault("n_ta_offset", self._DEFAULT_N_TA_OFFSET_TC)
        rendered["carrier_names"] = [f"Rx-Array-Carrier-{index:02d}" for index in range(count)]
        self._set_config_from_template("oran_uplane_rx_array_carriers.xml", "ORAN Uplane Rx array carriers", rendered)

    def set_oran_uplane_low_level_tx_links(self, dl_port_id=None, naming=None, endpoint_names=None):
        """Set ORAN U-plane low level TX links (one per DL endpoint).

        endpoint_names, when given, are the exact endpoint names to link
        (carrier N pairs with the Nth name); otherwise names are generated
        from the naming prefixes over the port count.
        """
        if endpoint_names is None:
            count = len(list(dl_port_id or self._DEFAULT_DL_PORT_IDS))
            naming = naming or endpoint_naming(None)
            endpoint_names = [f"{naming['tx']}{naming['base'] + index}" for index in range(count)]
        links = [
            {
                "name": f"Low-Level-Tx-Links-{index:03d}",
                "carrier": f"Tx-Array-Carrier-{index:02d}",
                "endpoint": endpoint_name,
            }
            for index, endpoint_name in enumerate(endpoint_names)
        ]
        self._set_config_from_template(
            "oran_uplane_low_level_tx_links.xml", "ORAN Uplane low level Tx links", {"links": links}
        )

    def set_oran_uplane_low_level_rx_links(  # pylint: disable=too-many-arguments
        self, ul_port_id=None, prach_port_id=None, *, naming=None, rx_endpoint_names=None, prach_endpoint_names=None
    ):
        """Set ORAN U-plane low level RX links.

        One link per UL endpoint plus one per PRACH endpoint; explicit name
        lists take precedence over generated prefix+index names. PRACH
        endpoints keep the legacy crossed carrier pairing (prach N maps to
        the carrier of its partner in each rx pair).
        """
        naming = naming or endpoint_naming(None)
        if rx_endpoint_names is None:
            rx_count = len(list(ul_port_id or self._DEFAULT_UL_PORT_IDS))
            rx_endpoint_names = [f"{naming['rx']}{naming['base'] + index}" for index in range(rx_count)]
        if prach_endpoint_names is None:
            prach_count = len(list(prach_port_id or self._DEFAULT_PRACH_PORT_IDS))
            prach_endpoint_names = [f"{naming['prach']}{naming['base'] + index}" for index in range(prach_count)]
        rx_count = len(rx_endpoint_names)
        entries = [(f"Rx-Array-Carrier-{index:02d}", name) for index, name in enumerate(rx_endpoint_names)]
        for index, name in enumerate(prach_endpoint_names):
            # crossed pairing; always clamp into the existing carrier range so
            # the rx-array-carrier leafref can never dangle
            partner = index ^ 1
            crossed = partner if partner < rx_count else index % rx_count
            entries.append((f"Rx-Array-Carrier-{crossed:02d}", name))
        links = [
            {"name": f"Low-Level-Rx-Links-{index:03d}", "carrier": carrier, "endpoint": endpoint}
            for index, (carrier, endpoint) in enumerate(entries)
        ]
        self._set_config_from_template(
            "oran_uplane_low_level_rx_links.xml", "ORAN Uplane low level Rx links", {"links": links}
        )

    # Fronthaul reception-window counters (o-ran-pm-rx-windows-stats) activated by default
    _DEFAULT_RX_WINDOW_OBJECTS = (
        "RX_ON_TIME",
        "RX_EARLY",
        "RX_LATE",
        "RX_CORRUPT",
        "RX_DUPL",
        "RX_TOTAL",
        "RX_ON_TIME_C",
        "RX_EARLY_C",
        "RX_LATE_C",
    )

    def set_oran_perf_measurement(self, pm_config=None):
        """Activate O-RU performance measurements (o-ran-performance-management).

        pm_config keys (all optional): rx_window_objects (measurement-object
        names to activate as RU-level COUNT counters), rx_window_interval and
        notification_interval (seconds). File upload stays disabled; results
        are reported via measurement-result-stats notifications.
        """
        pm_config = pm_config or {}
        rendered_config = {
            "rx_window_objects": list(pm_config.get("rx_window_objects", self._DEFAULT_RX_WINDOW_OBJECTS)),
            "rx_window_interval": pm_config.get("rx_window_interval", 60),
            "notification_interval": pm_config.get("notification_interval", 60),
        }
        # Two-phase: configure the measurement objects while inactive, then
        # activate in a separate edit-config, so object parameter changes
        # never coincide with an active measurement.
        rendered_config["pm_active"] = "false"
        self._set_config_from_template("oran_perf_measurement.xml", "ORAN Performance measurements", rendered_config)
        self._set_config_from_template(
            "oran_perf_measurement_activate.xml", "ORAN Performance measurement activation", rendered_config
        )

    def configure_perf_measurement(self, pm_config=None):
        """Configure PM, degrading gracefully around partial O-RU support.

        O-RUs implement only a subset of the o-ran-performance-management
        measurement objects, and the measurement edit is all-or-nothing (one
        unsupported object rejects the whole set). Attempt the requested set; on
        an rpc-error that names configured object(s), drop them and retry,
        converging on the objects the O-RU actually supports — so no per-RU
        object list is required. An rpc-error that names no configured object is
        not an object-support failure and propagates unchanged; transport
        failures always propagate. Returns the list of active objects (empty if
        the O-RU supported none).
        """
        pm_config = dict(pm_config or {})
        objects = list(pm_config.get("rx_window_objects", self._DEFAULT_RX_WINDOW_OBJECTS))
        dropped: list = []
        while objects:
            try:
                self.set_oran_perf_measurement({**pm_config, "rx_window_objects": objects})
            except rpc_ops.RPCError as err:
                rejected = _measurement_objects_in_error(err, objects)
                if not rejected:
                    raise
                dropped.extend(rejected)
                objects = [obj for obj in objects if obj not in rejected]
                logging.info("O-RU rejected PM measurement object(s) %s; retrying without them", rejected)
                continue
            if dropped:
                logging.warning(
                    "PM configured with a reduced object set — O-RU rejected %s; active: %s", dropped, objects
                )
            return objects
        logging.warning("O-RU rejected every configured PM measurement object; PM inactive (rejected %s)", dropped)
        return []

    def get_perf_measurement_config(self):
        """Get the RU's performance-measurement configuration ({} when unset)."""
        pm_filter = """<performance-measurement-objects xmlns="urn:o-ran:performance-management:1.0"/>"""
        return self._get_and_print_config(pm_filter, "ORAN Performance measurement config") or {}

    def set_oran_uplane_carrier_active(self, active_config):
        """Set ORAN U-plane carrier activation (nof_tx_carriers/nof_rx_carriers, default 4)."""
        tx_count = int(active_config.get("nof_tx_carriers") or len(self._DEFAULT_DL_PORT_IDS))
        rx_count = int(active_config.get("nof_rx_carriers") or len(self._DEFAULT_UL_PORT_IDS))
        rendered = dict(active_config)
        rendered["tx_carrier_names"] = [f"Tx-Array-Carrier-{index:02d}" for index in range(tx_count)]
        rendered["rx_carrier_names"] = [f"Rx-Array-Carrier-{index:02d}" for index in range(rx_count)]
        self._set_config_from_template("oran_uplane_carrier_active.xml", "ORAN Uplane carrier active", rendered)

    # The canonical 7d1s2u (6 DL / 4 guard / 4 UL special slot) pattern at
    # 30 kHz — the legacy hardcoded template's shape, kept as the default
    # for full-config provisioning and the 7d1s2u convenience method.
    _DEFAULT_TDD_PATTERN = {
        "scs_khz": 30,
        "dl_ul_tx_period": 10,
        "nof_dl_slots": 7,
        "nof_dl_symbols": 6,
        "nof_ul_slots": 2,
        "nof_ul_symbols": 4,
    }

    def set_oran_uplane_tdd_pattern(self, tdd_config):
        """Set a configurable TDD pattern on the O-RU.

        tdd_config mirrors the OCUDU tdd_ul_dl_cfg shape: scs_khz,
        dl_ul_tx_period (slots), nof_dl_slots, nof_dl_symbols, nof_ul_slots,
        nof_ul_symbols, plus an optional tdd_pattern_id (default 1). An
        explicit switching_points list ([{direction, frame_offset[,
        switching_point_id]}], ids all-or-none, else positional) bypasses
        computation entirely — the escape hatch for O-RUs that validate
        boundaries differently than TS 38.211 CP-inclusive symbol edges.
        Returns False without pushing when the O-RU does not advertise
        CONFIGURABLE-TDD-PATTERN-SUPPORTED; True after a push.
        """
        if not self._is_configurable_tdd_supported():
            logging.info("O-RU does not advertise CONFIGURABLE-TDD-PATTERN-SUPPORTED; skipping TDD pattern")
            return False
        if tdd_config.get("switching_points") is not None:
            switching_points = validate_switching_points(tdd_config["switching_points"])
        else:
            try:
                switching_points = compute_tdd_switching_points(
                    tdd_config["scs_khz"],
                    tdd_config["dl_ul_tx_period"],
                    tdd_config["nof_dl_slots"],
                    nof_dl_symbols=tdd_config.get("nof_dl_symbols", 0),
                    nof_ul_symbols=tdd_config.get("nof_ul_symbols", 0),
                    nof_ul_slots=tdd_config.get("nof_ul_slots"),
                )
            except KeyError as err:
                raise ValueError(
                    f"tdd pattern config requires scs_khz, dl_ul_tx_period and nof_dl_slots (missing {err})"
                ) from err
        self._set_config_from_template(
            "oran_uplane_tdd_pattern.xml",
            "ORAN Uplane TDD pattern",
            {"tdd_pattern_id": tdd_config.get("tdd_pattern_id", 1), "switching_points": switching_points},
        )
        if tdd_config.get("nof_tx_carriers") or tdd_config.get("nof_rx_carriers"):
            self.bind_tdd_pattern_to_carriers(
                tdd_pattern_id=tdd_config.get("tdd_pattern_id", 1),
                nof_tx_carriers=tdd_config.get("nof_tx_carriers"),
                nof_rx_carriers=tdd_config.get("nof_rx_carriers"),
            )
        return True

    def _is_configurable_tdd_supported(self):
        """Return True if the O-RU advertises CONFIGURABLE-TDD-PATTERN-SUPPORTED.

        The feature is declared by o-ran-module-cap (o-ran-uplane-conf gates
        its TDD subtree on it via if-feature) and advertised through
        ietf-yang-library. Delegates to get_advertised_features, which reads
        the RFC 8525 yang-library tree with a fallback to the deprecated
        RFC 7895 modules-state layout. Dry runs render everything.
        """
        if self.dry_run:
            return True
        return "CONFIGURABLE-TDD-PATTERN-SUPPORTED" in self.get_advertised_features("o-ran-module-cap")

    def set_oran_uplane_tdd_7d1s2u_slot_6_4_4(self):
        """Set ORAN U-plane TDD to the canonical 7d1s2u (6/4/4) pattern.

        Thin wrapper over set_oran_uplane_tdd_pattern so the pattern has a
        single source of truth; the retired hardcoded template carried the
        same shape with mid-symbol offsets (see compute_tdd_switching_points).
        """
        self.set_oran_uplane_tdd_pattern(dict(self._DEFAULT_TDD_PATTERN))

    def bind_tdd_pattern_to_carriers(self, tdd_pattern_id=1, nof_tx_carriers=None, nof_rx_carriers=None):
        """Bind a configurable TDD pattern to the tx/rx array carriers.

        Writes the o-ran-uplane-conf configurable-tdd-pattern leafref on every
        carrier that will be activated: pattern validation happens at carrier
        activation, and an unbound pattern is never applied. The leaf exists
        only when the O-RU supports CONFIGURABLE-TDD-PATTERN-SUPPORTED, so this
        self-gates on the advertised feature (like the pattern-upload step) —
        binding an unsupported leafref rpc-error's the edit.
        """
        if not self._is_configurable_tdd_supported():
            logging.info("O-RU does not advertise CONFIGURABLE-TDD-PATTERN-SUPPORTED; skipping TDD carrier binding")
            return
        tx_count = int(nof_tx_carriers or len(self._DEFAULT_DL_PORT_IDS))
        rx_count = int(nof_rx_carriers or len(self._DEFAULT_UL_PORT_IDS))
        rendered = {
            "tdd_pattern_id": tdd_pattern_id,
            "tx_carrier_names": [f"Tx-Array-Carrier-{index:02d}" for index in range(tx_count)],
            "rx_carrier_names": [f"Rx-Array-Carrier-{index:02d}" for index in range(rx_count)],
        }
        self._set_config_from_template(
            "oran_uplane_tdd_carrier_binding.xml", "ORAN Uplane TDD carrier binding", rendered
        )

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

    def get_oran_delay_management(self):
        """Get ORAN delay management data from the radio unit.

        Uses an operational <get>: the ru-delay-profile advertised by the O-RU
        is config false, so it never appears in <get-config> responses.
        """
        delay_filter = """<delay-management xmlns="urn:o-ran:delay:1.0"/>"""
        return self._get_and_print_config(delay_filter, "ORAN delay management", operational=True)

    def get_oran_module_capabilities(self):
        """Get o-ran-module-cap data from the radio unit.

        Uses an operational <get>: ru-capabilities and band-capabilities are
        config-false state advertised by the O-RU.
        """
        cap_filter = """<module-capability xmlns="urn:o-ran:module-cap:1.0"/>"""
        return self._get_and_print_config(cap_filter, "ORAN module capabilities", operational=True)

    def get_yang_library(self):
        """Get ietf-yang-library data from the radio unit (operational <get>)."""
        library_filter = """<yang-library xmlns="urn:ietf:params:xml:ns:yang:ietf-yang-library"/>"""
        return self._get_and_print_config(library_filter, "IETF yang library", operational=True)

    def get_module_capabilities_map(self):
        """Modules the O-RU advertises via ietf-yang-library, with features.

        Falls back to the deprecated RFC 7895 modules-state layout for O-RUs
        that do not expose the RFC 8525 yang-library tree. Returns {} when
        the RU exposes no yang-library data at all.
        """
        capabilities = parse_module_capabilities((self.get_yang_library() or {}).get("yang-library") or {})
        if not capabilities:
            state_filter = """<modules-state xmlns="urn:ietf:params:xml:ns:yang:ietf-yang-library"/>"""
            data = self._get_and_print_config(state_filter, "IETF yang library modules-state", operational=True) or {}
            capabilities = parse_module_capabilities(data.get("modules-state") or {})
        return capabilities

    def get_advertised_features(self, module_name):
        """Feature names the O-RU advertises for module_name via ietf-yang-library.

        Empty set when the module is absent or the RU exposes no yang-library
        data; permissive vs strict treatment is the caller's decision.
        """
        return self.get_module_capabilities_map().get(module_name, {}).get("features", set())

    _PTP_PROFILES = ("G_8275_1", "G_8275_2")

    def set_oran_sync_config(self, accepted_clock_classes=None, domain_number=None, ptp_profile=None, gnss_enable=None):
        """Set the O-RU's writable o-ran-sync configuration.

        accepted_clock_classes: iterable of PTP clock classes to accept. Note
        the keyed list is merge-additive: entries are added, existing entries
        remain (remove stale ones explicitly if replacing the set).
        domain_number: PTP domain (uint8; the RU defaults to 24).
        ptp_profile: G_8275_1 or G_8275_2, validated client-side because the
        server's enum rejection carries no machine-readable error path.
        gnss_enable: True/False, only for RUs advertising the GNSS feature.
        """
        if ptp_profile is not None and ptp_profile not in self._PTP_PROFILES:
            raise ValueError(f"ptp_profile must be one of {self._PTP_PROFILES}, got {ptp_profile!r}")
        sync_config = {
            "accepted_clock_classes": list(accepted_clock_classes or ()),
            "domain_number": domain_number,
            "ptp_profile": ptp_profile,
            "gnss_enable": gnss_enable,
        }
        self._set_config_from_template("oran_sync_config.xml", "ORAN sync configuration", sync_config)

    def get_sync_status(self, strict=False):
        """Get the O-RU's sync-status (config false) with graceful absence.

        None-valued fields / empty reference list when the RU exposes no
        synchronization state (a simulated RU may never populate it); strict=True
        re-raises retrieval failures so a dead session is not mistaken for a
        not-yet-synchronized O-RU.
        """
        status_filter = """<sync xmlns="urn:o-ran:sync:1.0"><sync-status/></sync>"""
        data = self._get_and_print_config(status_filter, "ORAN sync status", operational=True, strict=strict) or {}
        status = (data.get("sync") or {}).get("sync-status") or {}
        return {
            "sync_state": status.get("sync-state"),
            "time_error": status.get("time-error"),
            "frequency_error": status.get("frequency-error"),
            "supported_reference_types": [
                item.get("item") for item in ensure_list(status.get("supported-reference-types")) if item.get("item")
            ],
        }

    def get_array_carriers_state(self, strict=False):
        """Read tx/rx-array-carriers state — the carrier activation receipt.

        Returns {carrier-name: state} from the operational datastore (state
        is DISABLED/BUSY/READY per o-ran-uplane-conf); empty when the O-RU
        exposes no carriers. strict=True re-raises retrieval failures so a
        dead session is not mistaken for a carrier-less O-RU.
        """
        carrier_filter = (
            '<user-plane-configuration xmlns="urn:o-ran:uplane-conf:1.0">'
            "<tx-array-carriers/><rx-array-carriers/></user-plane-configuration>"
        )
        data = (
            self._get_and_print_config(carrier_filter, "ORAN array-carriers state", operational=True, strict=strict)
            or {}
        )
        uplane = data.get("user-plane-configuration") or {}
        states = {}
        for group in ("tx-array-carriers", "rx-array-carriers"):
            for carrier in ensure_list(uplane.get(group)):
                if carrier.get("name"):
                    states[carrier["name"]] = carrier.get("state")
        return states

    def wait_for_carriers_ready(self, timeout_s=120, poll_interval_s=5, on_poll=None, strict=False):
        """Poll array-carriers state until every carrier reports READY.

        The carrier state machine is asynchronous (DISABLED -> BUSY -> READY);
        an accepted activation edit is only an attempt — this readback is the
        confirmation. Returns the final {carrier-name: state} map either way;
        the caller judges completeness (empty means the O-RU exposed none).
        on_poll, when given, runs once per polling round (for example to feed
        a supervision watchdog). strict is forwarded to the state read so a
        dead session raises instead of reading as carrier-less.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            states = self.get_array_carriers_state(strict=strict)
            if states and all(state == "READY" for state in states.values()):
                return states
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return states
            if on_poll is not None:
                on_poll()
            time.sleep(min(poll_interval_s, remaining))

    def wait_for_sync_locked(self, timeout_s=300, poll_interval_s=5):
        """Poll sync-status until the O-RU reports LOCKED.

        Carrier activation requires a synchronized O-RU (an activation
        precondition of the O-RAN WG4 M-plane specification); synchronization
        itself is not configured here — many O-RUs expose o-ran-sync as
        read-only. Returns True once
        sync-state is LOCKED, False on timeout or when the RU exposes no
        synchronization state at all.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            state = self.get_sync_status().get("sync_state")
            if state == "LOCKED":
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logging.warning("O-RU sync-state is %s after %ss; not LOCKED", state, timeout_s)
                return False
            time.sleep(min(poll_interval_s, remaining))

    def get_full_config(self, transport_delays_ns=None, endpoint_config=None):
        """Get the complete configuration from the radio unit.

        transport_delays_ns: optional dict with t12_min_ns/t12_max_ns/
        t34_min_ns/t34_max_ns fronthaul transport delay bounds used when
        deriving the DU timing windows from the O-RU delay profile.
        endpoint_config: optional operator endpoint declarations (explicit
        prach_endpoints/rx_endpoints name lists or naming prefixes) used to
        classify the O-RU's rx endpoints when deriving the DU config —
        required for O-RUs whose names do not reveal PRACH.
        """
        if not self.dry_run:
            uplane_data = self.get_uplane_config() or {}
            processing_data = self.get_processing_elements() or {}
            interfaces_data = self.get_ietf_interfaces() or {}
            delay_data: dict = {}
            if "o-ran-delay-management" in self.get_module_capabilities_map():
                delay_data = self.get_oran_delay_management() or {}
            else:
                logging.info("O-RU does not advertise o-ran-delay-management; DU timing windows are not derived")
            self.get_ietf_hardware()
            self.get_oran_sync()

            try:
                ofh_cell, cell_cfg = build_ofh_config(
                    uplane_data.get("user-plane-configuration", {}),
                    processing_data.get("processing-elements", {}),
                    interfaces_data.get("interfaces", {}),
                    endpoint_config=endpoint_config,
                )
                bandwidth_khz, scs_hz = active_carrier_numerology(uplane_data.get("user-plane-configuration", {}))
                du_timing = build_ofh_timing(
                    delay_data.get("delay-management", {}),
                    transport_delays_ns=transport_delays_ns,
                    bandwidth_khz=bandwidth_khz,
                    scs_hz=scs_hz,
                )
                if ofh_cell:
                    print_ofh_config(ofh_cell, cell_cfg, ru_ofh_extra=du_timing)
            except (KeyError, TypeError, ValueError) as err:  # pragma: no cover - defensive
                logging.error("Failed to render OCUDU OFH config snippet: %s", err)

    def _get_and_print_config(self, filter_xml, description, operational=False, strict=False):
        """Fetch a NETCONF subtree, pretty print it and return the parsed dict.

        operational=True uses <get> (config + state data) instead of
        <get-config>; required for config-false data such as ru-delay-profile.
        Retrieval failures — transport errors, ncclient OperationErrors
        (rpc-error, request validation such as with-defaults) and the reply
        timeout — degrade to {} unless strict=True re-raises them. A dry run
        has no session and returns {}.
        """
        if self.dry_run:
            logging.debug("Dry run: not reading %s", description)
            return {}
        # RFC 6243: with basic-mode=explicit the server omits default-valued
        # leaves (e.g. tx/rx-array-carriers active) unless report-all is
        # requested — asked for only when the server lists that mode.
        with_defaults = with_defaults_mode(getattr(self.netconf_manager, "server_capabilities", None))
        try:
            if operational:
                result = self.netconf_manager.get(filter=("subtree", filter_xml), with_defaults=with_defaults)
            else:
                result = self.netconf_manager.get_config(
                    source=self.datastore, filter=("subtree", filter_xml), with_defaults=with_defaults
                )
        except (transport_errors.TransportError, OperationError, TimeoutExpiredError) as err:  # pragma: no cover
            if strict:
                raise
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
