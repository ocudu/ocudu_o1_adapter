# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Helpers to derive OCUDU Open Fronthaul configuration from NETCONF payloads."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from xml_utils import ensure_list as _ensure_list


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
    """Map NETCONF compression type to OCUDU format and static header flag."""
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


def endpoint_naming(endpoint_config: Any) -> Dict[str, Any]:
    """Endpoint names are device data: many O-RUs ship fixed static
    endpoints that may only be modified by their exact names. Defaults
    preserve the legacy generated names."""
    cfg = endpoint_config or {}
    return {
        "tx": cfg.get("tx_endpoint_prefix", "sep_txch"),
        "rx": cfg.get("rx_endpoint_prefix", "sep_rxch"),
        "prach": cfg.get("prach_endpoint_prefix", "sep_prach"),
        "base": int(cfg.get("endpoint_index_base", 1)),
    }


def normalize_endpoint_entries(entries: Any, key: str) -> List[Dict[str, Any]]:
    """Normalize an explicit endpoint list ([{name, eaxc_id}]) from config.

    Prefix+index generation cannot express every O-RU's fixed endpoint names
    (zero-padded, non-consecutive, type-blind), so config may declare the
    names outright; this validates one such declaration. Raises ValueError on
    a malformed list so callers can fail fast at load time.
    """
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{key} must be a non-empty list of {{name, eaxc_id}} entries")
    normalized: List[Dict[str, Any]] = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        eaxc_id = _to_int(entry.get("eaxc_id")) if isinstance(entry, dict) else None
        if not name or eaxc_id is None:
            raise ValueError(f"{key} entries need a name and an integer eaxc_id: {entry!r}")
        normalized.append({"name": str(name), "eaxc_id": eaxc_id})
    return normalized


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


def _prach_classifier(endpoint_config: Optional[Dict[str, Any]]):
    """Return a predicate deciding whether an rx endpoint name is PRACH.

    Which endpoints carry PRACH is an operator assignment, not something an
    O-RU's endpoint names reliably reveal (some vendors use type-blind names
    like PORT-05). Declared endpoint.prach_endpoints/rx_endpoints names are
    authoritative when config carries them; then the configured PRACH name
    prefix; the \"rach\" substring heuristic remains the last resort — it
    covers the legacy generated names (sep_prach*) and fixed vendor names
    like *RxPrachEndpoint*, but silently misclassifies type-blind names.
    """
    cfg = endpoint_config or {}
    prach_names = {str(entry.get("name")) for entry in _ensure_list(cfg.get("prach_endpoints")) if entry.get("name")}
    rx_names = {str(entry.get("name")) for entry in _ensure_list(cfg.get("rx_endpoints")) if entry.get("name")}
    prach_prefix = cfg.get("prach_endpoint_prefix")

    def _is_prach(name: str) -> bool:
        if name in prach_names:
            return True
        if name in rx_names:
            return False
        if prach_prefix and name.startswith(prach_prefix):
            return True
        return "rach" in name.lower()

    return _is_prach


def _linked_endpoint_names(uplane_cfg: Dict[str, Any], links_key: str, endpoint_leaf: str) -> Set[str]:
    """Endpoint names referenced by the low-level links of a user-plane-configuration."""
    return {
        str(link.get(endpoint_leaf))
        for link in _ensure_list(uplane_cfg.get(links_key))
        if isinstance(link, dict) and link.get(endpoint_leaf)
    }


def _declared_endpoint_names(endpoint_config: Optional[Dict[str, Any]], *keys: str) -> Set[str]:
    """Names from the explicit endpoint declarations under the given keys."""
    cfg = endpoint_config or {}
    return {
        str(entry.get("name"))
        for key in keys
        for entry in _ensure_list(cfg.get(key))
        if isinstance(entry, dict) and entry.get("name")
    }


def _select_endpoints(
    entries: List[Dict[str, Any]], linked_names: Set[str], declared_names: Set[str]
) -> List[Dict[str, Any]]:
    """The endpoints that count towards the DU configuration.

    An O-RU's static endpoint list can hold more than the carriers use
    (monitoring or spare endpoints); the low-level links say which ones a
    carrier is actually wired to, so once links exist only linked endpoints
    count, plus any name the operator declared. Without links (an
    unprovisioned O-RU) every endpoint counts.
    """
    if not linked_names:
        return entries
    return [entry for entry in entries if entry["name"] in linked_names or entry["name"] in declared_names]


# pylint: disable=too-many-locals,too-many-branches,too-many-statements
def build_ofh_config(
    uplane_cfg: Dict[str, Any],
    processing_cfg: Dict[str, Any],
    interfaces_cfg: Dict[str, Any],
    endpoint_config: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Construct the OCUDU OFH and cell_cfg configuration from NETCONF responses.

    endpoint_config, when given, carries the operator's endpoint declarations
    (explicit prach_endpoints/rx_endpoints name lists or the PRACH name
    prefix) used to classify the O-RU's rx endpoints — see _prach_classifier
    for why the name heuristic alone is not enough. Which endpoints count at
    all follows the O-RU's low-level links (see _select_endpoints): linked
    endpoints and declared names, or every endpoint when no links exist.
    """
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

    # tx/rx is structural (separate containers); which endpoints count follows
    # the low-level links (see _select_endpoints), and PRACH endpoints are told
    # apart by the operator's declarations when config carries them, falling
    # back to the name heuristic (see _prach_classifier).
    is_prach = _prach_classifier(endpoint_config)
    tx_entries = _select_endpoints(
        _extract_endpoints(low_level_tx_eps, ""),
        _linked_endpoint_names(uplane_cfg, "low-level-tx-links", "low-level-tx-endpoint"),
        _declared_endpoint_names(endpoint_config, "tx_endpoints"),
    )
    all_rx = _select_endpoints(
        _extract_endpoints(low_level_rx_eps, ""),
        _linked_endpoint_names(uplane_cfg, "low-level-rx-links", "low-level-rx-endpoint"),
        _declared_endpoint_names(endpoint_config, "rx_endpoints", "prach_endpoints"),
    )
    prach_entries = [e for e in all_rx if is_prach(e.get("name") or "")]
    rx_entries = [e for e in all_rx if not is_prach(e.get("name") or "")]

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


# o-ran-delay-management reports the O-RU delay profile in nanoseconds; the
# OCUDU gnb config expects microseconds.
_NS_PER_US = 1000

_RU_DELAY_PROFILE_LEAVES = (
    "t2a-min-up",
    "t2a-max-up",
    "t2a-min-cp-dl",
    "t2a-max-cp-dl",
    "t2a-min-cp-ul",
    "t2a-max-cp-ul",
    "ta3-min",
    "ta3-max",
    "tcp-adv-dl",
)


def _ceil_us(value_ns: int) -> int:
    return (value_ns + _NS_PER_US - 1) // _NS_PER_US


def _floor_us(value_ns: int) -> int:
    return value_ns // _NS_PER_US


def _profile_of_entry(entry: Dict[str, Any]) -> Dict[str, int]:
    ru_profile = entry.get("ru-delay-profile") or {}
    return {leaf: value for leaf in _RU_DELAY_PROFILE_LEAVES if (value := _to_int(ru_profile.get(leaf))) is not None}


def parse_delay_profile(
    delay_cfg: Optional[Dict[str, Any]], bandwidth_khz: Optional[int] = None, scs_hz: Optional[int] = None
) -> Dict[str, int]:
    """Extract the O-RU delay profile from an o-ran-delay-management subtree.

    Values are nanoseconds, as advertised by the O-RU (config false). The
    bandwidth-scs-delay-state list is keyed by bandwidth (kHz) and subcarrier
    spacing (Hz) precisely because processing delays differ per numerology:
    when bandwidth_khz/scs_hz are given, the matching entry is used. Without
    them — or when nothing matches — the first entry carrying a
    ru-delay-profile is used, which is only reliable for single-entry RUs.
    Returns {} when the RU exposes no delay data.
    """
    entries = _ensure_list((delay_cfg or {}).get("bandwidth-scs-delay-state"))
    if bandwidth_khz is not None and scs_hz is not None:
        for entry in entries:
            if _to_int(entry.get("bandwidth")) == bandwidth_khz and _to_int(entry.get("subcarrier-spacing")) == scs_hz:
                if profile := _profile_of_entry(entry):
                    return profile
    for entry in entries:
        if profile := _profile_of_entry(entry):
            return profile
    return {}


_SCS_ENUM_HZ = {"KHZ_15": 15000, "KHZ_30": 30000, "KHZ_60": 60000, "KHZ_120": 120000, "KHZ_240": 240000}


def active_carrier_numerology(uplane_cfg: Optional[Dict[str, Any]]) -> Tuple[Optional[int], Optional[int]]:
    """Best-effort (bandwidth_khz, scs_hz) of the configured carrier.

    Read from a user-plane-configuration subtree (tx-array-carriers
    channel-bandwidth + the endpoints' number-of-prb-per-scs key), for
    selecting the matching delay profile entry. (None, None) when unknown.
    """
    cfg = uplane_cfg or {}
    bandwidth_hz = _to_int(_first_value(_ensure_list(cfg.get("tx-array-carriers")), "channel-bandwidth"))
    scs_hz = None
    for endpoint in _ensure_list(cfg.get("low-level-tx-endpoints")):
        for prb_entry in _ensure_list(endpoint.get("number-of-prb-per-scs")):
            scs_name = (prb_entry or {}).get("scs")
            if scs_name and (scs_hz := _SCS_ENUM_HZ.get(scs_name)):
                break
        if scs_hz:
            break
    return (bandwidth_hz // 1000 if bandwidth_hz else None, scs_hz)


def derive_du_timing(
    profile: Dict[str, int],
    t12_min_ns: int = 0,
    t12_max_ns: int = 0,
    t34_min_ns: int = 0,
    t34_max_ns: int = 0,
) -> Dict[str, int]:
    """Derive the DU-side ru_ofh timing windows from an O-RU delay profile.

    Inputs are nanoseconds (o-ran-delay-management units); outputs are the
    OCUDU gnb yaml t1a_*/ta4_* keys in microseconds. O-RAN WG4 delay
    management arithmetic:

        T1a_min_x = T2a_min_x + T12_max    (DL transmit window; x = up/cp-dl/cp-ul)
        T1a_max_x = T2a_max_x + T12_min
        Ta4_min   = Ta3_min   + T34_min    (UL reception window)
        Ta4_max   = Ta3_max   + T34_max

    Rounding is conservative: the DL transmit window only tightens (min
    rounded up, max rounded down) and the UL reception window only widens
    (min rounded down, max rounded up). Keys are emitted only when the O-RU
    advertised the corresponding profile value.
    """
    derivations = (
        # (output key, profile leaf, transport delay, ns->us rounding)
        ("t1a_max_cp_dl", "t2a-max-cp-dl", t12_min_ns, _floor_us),
        ("t1a_min_cp_dl", "t2a-min-cp-dl", t12_max_ns, _ceil_us),
        ("t1a_max_cp_ul", "t2a-max-cp-ul", t12_min_ns, _floor_us),
        ("t1a_min_cp_ul", "t2a-min-cp-ul", t12_max_ns, _ceil_us),
        ("t1a_max_up", "t2a-max-up", t12_min_ns, _floor_us),
        ("t1a_min_up", "t2a-min-up", t12_max_ns, _ceil_us),
        ("ta4_max", "ta3-max", t34_max_ns, _ceil_us),
        ("ta4_min", "ta3-min", t34_min_ns, _floor_us),
    )
    timing: Dict[str, int] = {}
    for out_key, leaf, transport_ns, rounder in derivations:
        value_ns = profile.get(leaf)
        if value_ns is not None:
            timing[out_key] = rounder(value_ns + transport_ns)
    return timing


def build_ofh_timing(
    delay_cfg: Optional[Dict[str, Any]],
    transport_delays_ns: Optional[Dict[str, int]] = None,
    bandwidth_khz: Optional[int] = None,
    scs_hz: Optional[int] = None,
) -> Dict[str, int]:
    """Parse an o-ran-delay-management subtree and derive the DU timing keys.

    transport_delays_ns optionally carries t12_min_ns/t12_max_ns/t34_min_ns/
    t34_max_ns fronthaul transport bounds; bandwidth_khz/scs_hz select the
    delay profile entry matching the active carrier's numerology (see
    parse_delay_profile). Returns {} when the RU exposes no delay data, so
    callers can pass the result straight to print_ofh_config's ru_ofh_extra
    without special-casing.
    """
    return derive_du_timing(
        parse_delay_profile(delay_cfg, bandwidth_khz=bandwidth_khz, scs_hz=scs_hz),
        **(transport_delays_ns or {}),
    )


# 10 ms radio frame in o-ran-uplane-conf frame-offset units (1/1.2288 GHz ticks)
_FRAME_TICKS = 12_288_000

# 1 ms subframe in the same units
_SUBFRAME_TICKS = 1_228_800

# TS 38.211 5.3.1: the first OFDM symbol of each half-subframe (l = 0 and
# l = 7*2^mu, l counted per subframe) stretches its cyclic prefix by
# 16*kappa*Tc — a numerology-independent 520.83 ns, exactly 640 ticks.
_LONG_CP_EXTRA_TICKS = 640

# CUS-plane frameStructure lower nibble (subcarrier spacing index, mu)
_SCS_INDEX = {15: 0, 30: 1, 60: 2, 120: 3, 240: 4}

# 3GPP TS 38.104 table 5.3.2-1 (FR1): max transmission bandwidth N_RB per
# SCS/MHz — identical values to TS 38.101-1; the DU's own get_max_Nprb cites
# 38.104 (ocudu include/ocudu/ran/resource_block.h).
_NUM_PRB = {
    15: {5: 25, 10: 52, 15: 79, 20: 106, 25: 133, 30: 160, 35: 188, 40: 216, 45: 242, 50: 270},
    30: {
        5: 11,
        10: 24,
        15: 38,
        20: 51,
        25: 65,
        30: 78,
        35: 92,
        40: 106,
        45: 119,
        50: 133,
        60: 162,
        70: 189,
        80: 217,
        90: 245,
        100: 273,
    },
    60: {
        10: 11,
        15: 18,
        20: 24,
        25: 31,
        30: 38,
        35: 44,
        40: 51,
        45: 58,
        50: 65,
        60: 79,
        70: 93,
        80: 107,
        90: 121,
        100: 135,
    },
}


def compute_num_prb(bandwidth_mhz: int, scs_khz: int = 30) -> Optional[int]:
    """Max transmission bandwidth in PRBs (3GPP TS 38.104 table 5.3.2-1, FR1)."""
    return _NUM_PRB.get(scs_khz, {}).get(bandwidth_mhz)


def compute_frame_structure(num_prb: Optional[int], scs_khz: int = 30) -> Optional[int]:
    """CUS-plane frameStructure byte for a carrier.

    Upper nibble = FFT size exponent (smallest power of two covering the
    occupied subcarriers), lower nibble = subcarrier spacing index.
    """
    scs_index = _SCS_INDEX.get(scs_khz)
    if num_prb is None or scs_index is None:
        return None
    fft_exponent = max(1, (num_prb * 12 - 1).bit_length())
    return (fft_exponent << 4) | scs_index


def _symbol_boundary_ticks(scs_khz: int, slot_index: int, nof_symbols: int) -> int:
    """Absolute frame offset of the boundary after nof_symbols CP-inclusive
    OFDM symbols into slot slot_index (TS 38.211 5.3.1 timing).

    Symbols are not uniform: each is useful part + normal CP, and the first
    symbol of each half-subframe carries the extended CP. Dividing a slot
    into 14 equal parts therefore lands mid-symbol — an O-RU that validates
    switching points against its real symbol boundaries rejects such offsets.
    All quantities are exact integers in 1/1.2288 GHz ticks.
    """
    slots_per_subframe = 1 << _SCS_INDEX[scs_khz]
    useful_ticks = 1_228_800_000 // (scs_khz * 1000)
    normal_cp_ticks = useful_ticks * 9 // 128  # 144/2048 of the useful part
    subframe, slot_in_subframe = divmod(slot_index, slots_per_subframe)
    end_symbol = slot_in_subframe * 14 + nof_symbols
    long_cps = sum(1 for symbol in (0, 7 * slots_per_subframe) if symbol < end_symbol)
    return subframe * _SUBFRAME_TICKS + end_symbol * (useful_ticks + normal_cp_ticks) + long_cps * _LONG_CP_EXTRA_TICKS


def compute_tdd_switching_points(  # pylint: disable=too-many-arguments
    scs_khz: int,
    dl_ul_tx_period_slots: int,
    nof_dl_slots: int,
    *,
    nof_dl_symbols: int = 0,
    nof_ul_symbols: int = 0,
    nof_ul_slots: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Compute o-ran-uplane-conf configurable-tdd-pattern switching points.

    Parameters mirror the OCUDU tdd_ul_dl_cfg shape. A symbol split
    (nof_dl_symbols/nof_ul_symbols) implies exactly one special slot; without
    one the UL portion starts at the DL/UL slot boundary. When nof_ul_slots
    is given the slot budget is validated against the period. frame-offset is
    in 1/1.2288 GHz ticks (10 ms frame = 12288000); the pattern repeats over
    the frame, so the period must divide it.

    Offsets land on exact CP-inclusive symbol boundaries (TS 38.211 5.3.1),
    computed per repetition from the frame start: with more than one slot per
    subframe the extended-CP symbols make slots unequal, so a repetition is
    not a pure translation of the first period.

    Notes per o-ran-uplane-conf: the last DL point lands on the frame
    boundary (12288000), matching established vendor practice for "DL from
    frame start"; the O-RU rejects patterns exceeding its (unadvertised)
    switching-point limit; configurable-tdd-pattern must not be combined with
    the deprecated LTE TDD config (this client never writes it).
    """
    scs_index = _SCS_INDEX.get(scs_khz)
    if scs_index is None:
        raise ValueError(f"unsupported subcarrier spacing: {scs_khz} kHz")
    slots_per_frame = 10 * 2**scs_index
    if dl_ul_tx_period_slots <= 0 or slots_per_frame % dl_ul_tx_period_slots != 0:
        raise ValueError("dl_ul_tx_period must divide the 10 ms frame at this SCS")
    guard_symbols = 14 - nof_dl_symbols - nof_ul_symbols
    if guard_symbols < 0:
        raise ValueError("nof_dl_symbols + nof_ul_symbols exceeds one slot")
    special_slots = 1 if (nof_dl_symbols or nof_ul_symbols) else 0
    if nof_dl_slots + special_slots > dl_ul_tx_period_slots:
        raise ValueError("nof_dl_slots and the special slot exceed dl_ul_tx_period")
    if nof_ul_slots is not None and nof_dl_slots + special_slots + nof_ul_slots != dl_ul_tx_period_slots:
        raise ValueError("nof_dl_slots + special slot + nof_ul_slots must equal dl_ul_tx_period")

    points: List[Dict[str, Any]] = []
    for repetition in range(slots_per_frame // dl_ul_tx_period_slots):
        base_slot = repetition * dl_ul_tx_period_slots
        downlink_end = _symbol_boundary_ticks(scs_khz, base_slot + nof_dl_slots, nof_dl_symbols)
        if special_slots:
            uplink_start = _symbol_boundary_ticks(scs_khz, base_slot + nof_dl_slots, 14 - nof_ul_symbols)
        else:
            uplink_start = downlink_end  # no special slot: UL begins at the slot boundary
        if guard_symbols > 0 and special_slots:
            points.append({"direction": "GP", "frame_offset": downlink_end})
        points.append({"direction": "UL", "frame_offset": uplink_start})
        points.append(
            {
                "direction": "DL",
                "frame_offset": _symbol_boundary_ticks(scs_khz, base_slot + dl_ul_tx_period_slots, 0),
            }
        )
    return points


_SWITCHING_POINT_DIRECTIONS = ("DL", "UL", "GP")

# o-ran-uplane-conf types the switching-point-id list key as uint16
_SWITCHING_POINT_ID_MAX = 65535


def _validate_switching_point_id(entry: Dict[str, Any], seen_ids: Set[int]) -> int:
    """Coerce one config-supplied switching_point_id, enforcing the uint16
    range of the o-ran-uplane-conf leaf and uniqueness within the pattern."""
    point_id = _to_int(entry.get("switching_point_id"))
    if point_id is None or not 0 <= point_id <= _SWITCHING_POINT_ID_MAX:
        raise ValueError(
            f"switching point switching_point_id must be an integer within uint16 (0..{_SWITCHING_POINT_ID_MAX}): "
            f"{entry!r}"
        )
    if point_id in seen_ids:
        raise ValueError(f"switching point switching_point_id must be unique within the pattern: {entry!r}")
    seen_ids.add(point_id)
    return point_id


def validate_switching_points(points: Any) -> List[Dict[str, Any]]:
    """Validate a config-supplied switching-point list (the vendor escape
    hatch: O-RUs disagree about where a valid boundary lies, so raw offsets
    from config bypass compute_tdd_switching_points entirely).

    Each entry needs a direction (DL/UL/GP) and an integer frame_offset in
    1/1.2288 GHz ticks within the 10 ms frame; offsets must be
    non-decreasing. switching_point_id is optional and all-or-none: with
    none given the entries come back as {direction, frame_offset} and the
    template numbers them by list position (1..N); when every entry carries
    one, the ids are coerced to int, must be unique and must fit the leaf's
    uint16 type (0..65535), and the entries come back with
    "switching_point_id" set. A list mixing entries with and without an id
    raises. Returns the normalized list; raises ValueError on any malformed
    entry.
    """
    if not isinstance(points, list) or not points:
        raise ValueError("switching_points must be a non-empty list")
    validated: List[Dict[str, Any]] = []
    previous_offset = 0
    ids_given: Optional[bool] = None
    seen_ids: Set[int] = set()
    for entry in points:
        if not isinstance(entry, dict):
            raise ValueError(f"switching point entries must be mappings, got {entry!r}")
        direction = str(entry.get("direction", "")).upper()
        if direction not in _SWITCHING_POINT_DIRECTIONS:
            raise ValueError(f"switching point direction must be one of {_SWITCHING_POINT_DIRECTIONS}: {entry!r}")
        offset = _to_int(entry.get("frame_offset"))
        if offset is None or not 0 <= offset <= _FRAME_TICKS:
            raise ValueError(f"switching point frame_offset must be an integer within the 10 ms frame: {entry!r}")
        if offset < previous_offset:
            raise ValueError(f"switching point offsets must be non-decreasing: {entry!r}")
        previous_offset = offset
        normalized: Dict[str, Any] = {"direction": direction, "frame_offset": offset}
        has_id = entry.get("switching_point_id") is not None
        if ids_given is None:
            ids_given = has_id
        elif has_id != ids_given:
            raise ValueError(f"switching_point_id must be given on every switching point or on none: {entry!r}")
        if has_id:
            normalized["switching_point_id"] = _validate_switching_point_id(entry, seen_ids)
        validated.append(normalized)
    return validated


def parse_module_capabilities(yang_library_cfg: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Extract per-module capabilities from an ietf-yang-library subtree.

    Accepts the RFC 8525 yang-library layout (module-set/module) as well as
    the deprecated RFC 7895 modules-state layout (a module list at the top
    level). Returns {module_name: {"revision", "namespace", "features": set}}
    and {} when the RU exposes no yang-library data.
    """
    cfg = yang_library_cfg or {}
    module_lists = [module_set.get("module") for module_set in _ensure_list(cfg.get("module-set"))]
    module_lists.append(cfg.get("module"))  # RFC 7895 modules-state layout
    modules: Dict[str, Dict[str, Any]] = {}
    for module_list in module_lists:
        for module in _ensure_list(module_list):
            name = module.get("name")
            if not name:
                continue
            modules[name] = {
                "revision": module.get("revision"),
                "namespace": module.get("namespace"),
                "features": set(_ensure_list(module.get("feature"))),
            }
    return modules


def print_ofh_config(
    cell_config: Dict[str, Any],
    cell_cfg: Optional[Dict[str, Any]] = None,
    ru_ofh_extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Pretty print the OFH configuration snippet.

    ru_ofh_extra keys (e.g. the derived t1a_*/ta4_* timing windows) are laid
    out at ru_ofh top level, before cells — matching the OCUDU gnb yaml layout.
    """
    ru_ofh: Dict[str, Any] = dict(ru_ofh_extra or {})
    ru_ofh["cells"] = [cell_config]
    snippet: Dict[str, Any] = {"ru_ofh": ru_ofh}
    if cell_cfg:
        snippet["cell_cfg"] = cell_cfg

    print("\n# Auto-generated OFH configuration")
    if yaml is not None:
        print(yaml.safe_dump(snippet, default_flow_style=False, sort_keys=False))
    else:
        print(json.dumps(snippet, indent=2))
