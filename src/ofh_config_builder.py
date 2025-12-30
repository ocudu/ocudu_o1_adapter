#!/usr/bin/python3
# SPDX-License-Identifier: BSD 3-Clause Open MPI variant

"""Helpers to derive srsRAN Open Fronthaul configuration from NETCONF payloads."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import yaml


def _ensure_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(str(value))
    except (ValueError, TypeError):
        return None


def _first_value(entries: List[Dict[str, Any]], key: str) -> Any:
    for entry in entries:
        value = entry.get(key)
        if value is not None:
            return value
    return None


def _normalize_compression(compression_value: Optional[str]) -> Tuple[Optional[str], Optional[bool]]:
    """Map NETCONF compression type to srsRAN format and static header flag."""
    if not compression_value:
        return None, None
    comp_upper = compression_value.replace("-", " ").replace("_", " ").upper()
    static_hdr = False
    if "BLOCK" in comp_upper or "BFP" in comp_upper or comp_upper == "STATIC":
        static_hdr = True
        return "bfp", static_hdr
    if "MU" in comp_upper:
        return "mu law", static_hdr
    if "MODULATION" in comp_upper:
        return "modulation", static_hdr
    if "NONE" in comp_upper:
        return "none", static_hdr
    return compression_value.lower(), static_hdr


def _extract_endpoints(endpoints: Any, prefix: str) -> List[Dict[str, Any]]:
    """Extract endpoint information filtered by name prefix."""
    collected: List[Dict[str, Any]] = []
    for endpoint in _ensure_list(endpoints):
        name = endpoint.get("name")
        if not name or not name.startswith(prefix):
            continue
        compression = endpoint.get("compression", {})
        e_axcid = endpoint.get("e-axcid", {})
        collected.append(
            {
                "name": name,
                "port_id": _to_int(e_axcid.get("eaxc-id")),
                "compression_type": compression.get("compression-type"),
                "iq_bitwidth": _to_int(compression.get("iq-bitwidth")),
            }
        )
    return collected


# pylint: disable=too-many-locals,too-many-branches,too-many-statements
def build_ofh_config(
    uplane_cfg: Dict[str, Any], processing_cfg: Dict[str, Any], interfaces_cfg: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Construct the srsRAN OFH and cell_cfg configuration from NETCONF responses."""
    if not uplane_cfg and not processing_cfg:
        return None, None

    cell: Dict[str, Any] = {}
    cell_cfg: Dict[str, Any] = {}

    # Processing elements for MAC/VLAN information.
    ru_elements = _ensure_list(processing_cfg.get("ru-elements"))
    if ru_elements:
        transport_flow = ru_elements[0].get("transport-flow", {})
        interface_name = transport_flow.get("interface-name")
        eth_flow = transport_flow.get("eth-flow", {})
        vlan = eth_flow.get("vlan-id") or transport_flow.get("vlan-id")
        cell["network_interface"] = interface_name
        cell["ru_mac_addr"] = eth_flow.get("ru-mac-address")
        cell["du_mac_addr"] = eth_flow.get("o-du-mac-address")
        if vlan is not None:
            vlan_int = _to_int(vlan)
            cell["vlan_tag_cp"] = vlan_int
            cell["vlan_tag_up"] = vlan_int

    # Fall back to VLAN interface name if not present.
    if not cell.get("network_interface"):
        interfaces = _ensure_list(interfaces_cfg.get("interface"))
        for iface in interfaces:
            base = iface.get("base-interface")
            if base:
                cell["network_interface"] = base
                break
        if not cell.get("network_interface") and interfaces:
            cell["network_interface"] = interfaces[0].get("name")

    low_level_tx_eps = uplane_cfg.get("low-level-tx-endpoints")
    low_level_rx_eps = uplane_cfg.get("low-level-rx-endpoints")

    tx_entries = _extract_endpoints(low_level_tx_eps, "sep_txch")
    rx_entries = _extract_endpoints(low_level_rx_eps, "sep_rxch")
    prach_entries = _extract_endpoints(low_level_rx_eps, "sep_prach")

    dl_ports = sorted({entry["port_id"] for entry in tx_entries if entry["port_id"] is not None})
    ul_ports = sorted({entry["port_id"] for entry in rx_entries if entry["port_id"] is not None})
    prach_ports = sorted({entry["port_id"] for entry in prach_entries if entry["port_id"] is not None})

    if dl_ports:
        cell["dl_port_id"] = dl_ports
        cell_cfg["nof_antennas_dl"] = len(dl_ports)
    if ul_ports:
        cell["ul_port_id"] = ul_ports
        cell_cfg["nof_antennas_ul"] = len(ul_ports)
    if prach_ports:
        cell["prach_port_id"] = prach_ports

    raw_dl_comp = _first_value(tx_entries, "compression_type")
    raw_ul_comp = _first_value(rx_entries, "compression_type")
    raw_prach_comp = _first_value(prach_entries, "compression_type")

    dl_comp, dl_static = _normalize_compression(raw_dl_comp)
    ul_comp, ul_static = _normalize_compression(raw_ul_comp)
    prach_comp, prach_static = _normalize_compression(raw_prach_comp)

    if dl_comp:
        cell["compr_method_dl"] = dl_comp
        cell["enable_dl_static_compr_hdr"] = bool(dl_static)
    if ul_comp:
        cell["compr_method_ul"] = ul_comp
        cell["enable_ul_static_compr_hdr"] = bool(ul_static)
    if prach_comp:
        cell["compr_method_prach"] = prach_comp
        cell.setdefault("enable_ul_static_compr_hdr", bool(ul_static or prach_static))

    dl_bitwidth = _first_value(tx_entries, "iq_bitwidth")
    ul_bitwidth = _first_value(rx_entries, "iq_bitwidth")
    prach_bitwidth = _first_value(prach_entries, "iq_bitwidth")

    if dl_bitwidth is not None:
        cell["compr_bitwidth_dl"] = dl_bitwidth
    if ul_bitwidth is not None:
        cell["compr_bitwidth_ul"] = ul_bitwidth
    if prach_bitwidth is not None:
        cell["compr_bitwidth_prach"] = prach_bitwidth

    # Default operational flags – these are commonly required by the DU.
    cell.setdefault("is_prach_cp_enabled", True)

    # Provide a sensible default RU reference level if not derivable from NETCONF.
    cell.setdefault("ru_reference_level_dBFS", -15.0)
    cell.setdefault("subcarrier_rms_backoff_dB", 3.0)

    # Derive additional DU configuration properties.
    tx_array_carriers = _ensure_list(uplane_cfg.get("tx-array-carriers"))
    channel_bw_hz = None
    dl_arfcn = None
    if tx_array_carriers:
        channel_bw_hz = _to_int(_first_value(tx_array_carriers, "channel-bandwidth"))
        dl_arfcn = _to_int(_first_value(tx_array_carriers, "absolute-frequency-center"))
    if channel_bw_hz:
        cell_cfg["channel_bandwidth_MHz"] = int(round(channel_bw_hz / 1e6))
    if dl_arfcn is not None:
        cell_cfg["dl_arfcn"] = dl_arfcn

    return cell, cell_cfg


# pylint: enable=too-many-locals,too-many-branches,too-many-statements


def print_ofh_config(cell_config: Dict[str, Any], cell_cfg: Optional[Dict[str, Any]] = None) -> None:
    """Pretty print the OFH configuration snippet."""
    snippet: Dict[str, Any] = {"ru_ofh": {"cells": [cell_config]}}
    if cell_cfg:
        snippet["cell_cfg"] = cell_cfg

    print("\n# Auto-generated OFH configuration")
    if yaml is not None:
        print(yaml.safe_dump(snippet, default_flow_style=False, sort_keys=False))
    else:
        print(json.dumps(snippet, indent=2))
