# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Reverse of the O1 adapter: render a srsRAN gNB/CU/DU YAML config into a NETCONF (3GPP NRM) XML config.

WORK IN PROGRESS -- this module is incomplete and its output should not be relied on yet. The coverage
list below is what has been inverted so far; anything outside it is passed through from the scaffold
untouched, and both the covered set and the interfaces here are still expected to change.

The forward path (netconf_to_yaml) projects a full 3GPP NRM NETCONF tree onto the much
smaller srsRAN YAML config, dropping every YANG-mandatory / structural field (resource ids, DNs,
namespaces, priorityLabel, gNBId, nPNIdentityList, ...). That projection is lossy, so YAML -> XML is
not a clean inverse: the YAML alone cannot reconstruct a config that passes YANG validation.

This module therefore works by *scaffold + overlay + prune*: it starts from a base seed XML (which
already carries every mandatory field) and overwrites only the leaves that the YAML actually carries,
applying the inverse of each forward transform. The result is guaranteed structurally valid because the
seed is, and the seeds under tests/configs/ are the natural scaffolds to use.

The overlay records every leaf it writes, so the seed is then pruned back to what the YAML determines
(see _prune) and the output is the *minimal* config that renders to this YAML, not a whole seed with a
few values swapped: spare cells, endpoints and policies go, as does every extension leaf with no YAML
key behind it. What stays is the 3GPP NRM structure of the surviving objects -- their mandatory
attributes, list keys and DNs -- which the YAML never carries but the config cannot validate without.
Pass minimal=False (--full) to get the whole overlaid seed instead.

Every section is optional on both sides: only the network functions the scaffold provides *and* the
YAML carries are overlaid, so a CU-UP split YAML paired with the CU-UP seed needs no GNBDUFunction at
all -- pick the seed that matches the split. Covered so far:

  - GNBDUFunction: NRCellDU standard attributes, all ocudu_nrcelldu_* channel/base extensions, the
    per-cell OFH extension, and RRMPolicyRatio (from cell_cfg)
  - GNBCUCPFunction: the NG-C / E1 / F1-C endpoints, the flat amf.* knobs, and a tracking area's
    tac -> nRTAC, plmn -> pLMNId and tai_slice_support_list -> RRMPolicyRatio members
  - GNBCUUPFunction: the E1 / NG-U / F1-U endpoints (incl. the F1-U GTP-U ports) and test_mode
  - the top-level log and pcap blocks, and the flat sections in _FLAT_SECTIONS (ru_sdr, ru_dummy, hal,
    metrics, remote_control), all routed back to the function extension they came from

cu_cp.mobility is the one section with a matching function that is still not inverted; it and the
top-level sections in _UNINVERTED_SECTIONS are kept as the scaffold has them and reported.
"""

import argparse
import copy
import logging
import sys
from typing import Any, Dict

import xmltodict
import yaml

_EXT_CONTAINER = "ocudu_nrcelldu_extensions"
_BASE_CONTAINER = "ocudu_nrcelldu_base_extensions"
_OFH_CONTAINER = "ocudu_nrcelldu_ofh_extensions"

# ManagedElement function -> its OCUDU extension container, in the order ConfigManager walks them
# on the forward path: the log block is first-wins in this order, pcap is merged across all of them.
_FUNCTION_EXTENSIONS = (
    ("GNBDUFunction", "ocudu_gnbdufunction_extensions"),
    ("GNBCUCPFunction", "ocudu_gnbcucpfunction_extensions"),
    ("GNBCUUPFunction", "ocudu_gnbcuupfunction_extensions"),
)

# Top-level YAML sections that mirror one OCUDU extension container of a function one-for-one, so the
# inverse is a plain deep-merge onto that container -- no transform, no reshaping. The value is written
# to every function that declares the container: the forward path merges these across functions (or
# takes the last one that carries them), and a single value per YAML key is what reproduces either way.
_FLAT_SECTIONS = {
    "ru_sdr": "ocudu_ru_sdr_extensions",
    "ru_dummy": "ocudu_ru_dummy_extensions",
    "hal": "ocudu_hal_extensions",
    "metrics": "ocudu_metrics_extensions",
    "remote_control": "ocudu_remote_control_extensions",
}

# Top-level YAML sections this module knows how to invert; anything else is reported and left alone.
_HANDLED_SECTIONS = ("cells", "ru_ofh", "cell_cfg", "cu_cp", "cu_up", "log", "pcap") + tuple(_FLAT_SECTIONS)

# cu_cp.amf keys that land on EP_NgC itself; the rest of the block mirrors its OCUDU extension.
_AMF_ENDPOINT_KEYS = ("addrs", "bind_addrs", "supported_tracking_areas")

# The GNBCUCPFunction nodes cu_cp.mobility is rendered from, kept whole when the YAML carries it.
_MOBILITY_NODES = ("ocudu_gnbcucpfunction_mobility_extensions", "NRCellCU")

# The rest of the top-level YAML sections, mapped to the scaffold node each one is rendered from on the
# forward path (ConfigManager.write_full_config). Minimal pruning cannot trim these against the YAML
# because there is no inverse for them, so it works at whole-node granularity instead: a section the YAML
# carries keeps its node exactly as the scaffold has it, and a section the YAML never mentions is dropped
# like any other redundant subtree. Either way the config still renders back to the YAML it came from.
_UNINVERTED_SECTIONS = {
    "f1ap": "EP_F1C",
    "f1u": "EP_F1U",
    "test_mode": "ocudu_gnbdufunction_testmode_extensions",
}

# Everything the OCUDU YANG augmentations add is named with this prefix, which is what lets minimal
# pruning tell adapter payload apart from the 3GPP NRM structure it hangs off (see _prune).
_OCUDU_PREFIX = "ocudu_"

# Leaves that are YANG list keys, from the 'key' statements of _3gpp-common-{managed-element,ep-rp},
# _3gpp-nr-nrm-{ep,rrmpolicy,nrcelldu,nrcellcu,nrcellrelation} and _3gpp-5g-common-yang-types: 'id' for
# the objects themselves, 'mcc mnc sd sst' for pLMNInfoList and rRMPolicyMemberList, 'ipAddress vlanId'
# for localAddress, 'idx' for nPNIdentityList/plmnId. A list entry cannot be addressed without its full
# key, so these ride along with a node that survives pruning -- they never keep one alive by themselves.
# Only load-bearing inside the OCUDU extension containers (a keyed list such as sib's si_sched_info),
# since _prune keeps the 3GPP leaves of a surviving object either way.
_LIST_KEYS = frozenset({"id", "mcc", "mnc", "sd", "sst", "idx", "ipAddress", "vlanId"})

# YAML per-cell key -> NETCONF NRCellDU standard attribute name (plain scalar copy).
_STANDARD_ATTRS = {
    "pci": "nRPCI",
    "tac": "nRTAC",
    "dl_arfcn": "arfcnDL",
    "channel_bandwidth_MHz": "bSChannelBwDL",
}

# YAML per-cell channel key -> ocudu_nrcelldu_extensions sub-container. Note the tdd rename.
_CHANNEL_CONTAINERS = {
    "ssb": "ocudu_nrcelldu_ssb_extensions",
    "prach": "ocudu_nrcelldu_prach_extensions",
    "tdd_ul_dl_cfg": "ocudu_nrcelldu_tdd_extensions",
    "pdsch": "ocudu_nrcelldu_pdsch_extensions",
    "pusch": "ocudu_nrcelldu_pusch_extensions",
    "pucch": "ocudu_nrcelldu_pucch_extensions",
    "csi": "ocudu_nrcelldu_csi_extensions",
    "srs": "ocudu_nrcelldu_srs_extensions",
    "pdcch": "ocudu_nrcelldu_pdcch_extensions",
    "paging": "ocudu_nrcelldu_paging_extensions",
    "drx": "ocudu_nrcelldu_drx_extensions",
    "mac_cell_group": "ocudu_nrcelldu_mac_cell_group_extensions",
    "sib": "ocudu_nrcelldu_sib_extensions",
}


def _scalar_to_text(value):
    """Render a YAML scalar the way xmltodict expects it (all element text is a string)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _sd_to_hex(sd):
    """Inverse of netconf_to_yaml.parse_sd: 3GPP SD -> colon-separated hex octets (e.g. 'ff:ff:ff').

    Takes the plain int the adapter renders as well as the quoted '0x...' form a hand-written YAML uses.
    """
    if isinstance(sd, str) and sd.strip().lower().startswith("0x"):
        value = int(sd.strip(), 16)
    else:
        value = int(sd)
    text = f"{value:06x}"
    return ":".join(text[i : i + 2] for i in range(0, 6, 2))


def _split_plmn(plmn):
    """Split a PLMN string into (mcc, mnc). MCC is always 3 digits per 3GPP.

    Returns None if the value is not an unambiguous string (a bare YAML int such as 00101 has already
    been mangled to octal 65 by the loader, so we cannot recover the original digits -- keep the
    scaffold's mcc/mnc in that case).
    """
    if not isinstance(plmn, str) or len(plmn) < 4:
        return None
    return plmn[:3], plmn[3:]


def _write(parent, key, value, written):
    """Assign parent[key] and record the write, so _prune can tell it apart from scaffold filler.

    The overlay mutates the scaffold nodes in place and every node stays reachable for the whole build,
    so the node object itself is a stable handle -- no need to thread XML paths through the overlay.
    """
    parent[key] = value
    written.add((id(parent), key))


def _merge(parent, key, yaml_val, written):
    """Deep-merge a YAML value onto parent[key], mirroring the existing scaffold node's XML shape.

    - dict -> recurse, preserving any scaffold-only leaves (and the @xmlns attr) already under the node
    - list of dicts -> repeated complex elements (rebuilt from scratch)
    - list of scalars -> a bracketed string leaf ('[a, b]') or a leaf-list (repeated elements),
      matching whatever the scaffold already has; a bracketed string when the node is new
    - scalar -> element text
    """
    existing = parent.get(key)

    if isinstance(yaml_val, dict):
        if not isinstance(existing, dict):
            existing = {}
            parent[key] = existing
        for sub_key, sub_val in yaml_val.items():
            _merge(existing, sub_key, sub_val, written)
        return

    if isinstance(yaml_val, list):
        if yaml_val and all(isinstance(item, dict) for item in yaml_val):
            rebuilt = []
            for item in yaml_val:
                node: Dict[str, Any] = {}
                for sub_key, sub_val in item.items():
                    _merge(node, sub_key, sub_val, written)
                rebuilt.append(node)
            _write(parent, key, rebuilt, written)
        elif isinstance(existing, list):  # existing YANG leaf-list -> repeated elements
            _write(parent, key, [_scalar_to_text(item) for item in yaml_val], written)
        else:  # OFH port ids and the bracketed-string arrays are single leaves holding "[a, b]"
            _write(parent, key, "[" + ", ".join(_scalar_to_text(item) for item in yaml_val) + "]", written)
        return

    _write(parent, key, _scalar_to_text(yaml_val), written)


def _overlay_ofh(ext, ofh_cell, written):
    """Overlay a ru_ofh cell onto the cell's OFH extension, inverting the compression transforms."""
    container = ext.setdefault(_OFH_CONTAINER, {})
    for key, value in ofh_cell.items():
        if "compr_method" in key and value == "bfp":
            value = "BLOCK_FLOATING_POINT"
        elif "static_compr_hdr" in key:
            enabled = value is True or str(value).lower() == "true"
            value = "STATIC" if enabled else "DYNAMIC"
        _merge(container, key, value, written)


def _overlay_cell(cell_xml, yaml_cell, ofh_cell, written):
    """Overlay one YAML cell (+ its ru_ofh counterpart) onto a scaffold NRCellDU element in place."""
    attrs = cell_xml.setdefault("attributes", {})
    ext = attrs.setdefault(_EXT_CONTAINER, {})

    for yaml_key, value in yaml_cell.items():
        if yaml_key in _STANDARD_ATTRS:
            _write(attrs, _STANDARD_ATTRS[yaml_key], _scalar_to_text(value), written)
        elif yaml_key == "enabled":
            _write(attrs, "administrativeState", "UNLOCKED" if value else "LOCKED", written)
        elif yaml_key == "plmn":
            split = _split_plmn(value)
            if split is None:
                logging.warning(
                    "Cell plmn %r is not an unambiguous string; keeping the scaffold's mcc/mnc. "
                    'Quote the value (e.g. plmn: "00101") to override it.',
                    value,
                )
            elif isinstance(attrs.get("pLMNInfoList"), dict):
                _write(attrs["pLMNInfoList"], "mcc", split[0], written)
                _write(attrs["pLMNInfoList"], "mnc", split[1], written)
        elif yaml_key in _CHANNEL_CONTAINERS:
            _merge(ext, _CHANNEL_CONTAINERS[yaml_key], value, written)
        elif not isinstance(value, (dict, list)):  # leftover scalar -> base extension
            # Only leaves the scaffold already carries, never a new one: the base extension declares just
            # a handful (nof_antennas_dl/ul, common_scs) while a srsRAN cell_cfg holds far more top-level
            # scalars, and libyang rejects the whole config over a leaf the model does not have -- 'band'
            # is the one that bites, since OCUDU derives it from dl_arfcn anyway.
            base = ext.setdefault(_BASE_CONTAINER, {})
            if yaml_key not in base:
                logging.warning("Ignoring per-cell key %r: %s has no such leaf", yaml_key, _BASE_CONTAINER)
                continue
            _write(base, yaml_key, f"KHZ_{value}" if "scs" in yaml_key else _scalar_to_text(value), written)
        else:
            logging.warning("Ignoring unrecognised per-cell key %r", yaml_key)

    if ofh_cell is not None:
        _overlay_ofh(ext, ofh_cell, written)


def _overlay_rrm_policy(rrm_attrs, cell_cfg, written):
    """Overlay cell_cfg onto a single RRMPolicyRatio 'attributes' dict."""
    slicing = (cell_cfg.get("slicing") or [{}])[0]
    sched = slicing.get("sched_cfg", {})
    member = rrm_attrs.setdefault("rRMPolicyMemberList", {})

    split = _split_plmn(cell_cfg.get("plmn"))
    if split is not None and isinstance(member, dict):
        _write(member, "mcc", split[0], written)
        _write(member, "mnc", split[1], written)
    if "sst" in slicing and isinstance(member, dict):
        _write(member, "sst", _scalar_to_text(slicing["sst"]), written)
    if "sd" in slicing and isinstance(member, dict):
        _write(member, "sd", _sd_to_hex(slicing["sd"]), written)
    if "min_prb_policy_ratio" in sched:
        _write(rrm_attrs, "rRMPolicyMinRatio", _scalar_to_text(sched["min_prb_policy_ratio"]), written)
    if "max_prb_policy_ratio" in sched:
        _write(rrm_attrs, "rRMPolicyMaxRatio", _scalar_to_text(sched["max_prb_policy_ratio"]), written)


def _ensure_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def overlay_du(yaml_cfg, gnbdu, written):
    """Overlay the DU portion of a srsRAN YAML config onto a scaffold GNBDUFunction element, in place."""
    yaml_cells = yaml_cfg.get("cells") or []
    ofh_cells = (yaml_cfg.get("ru_ofh") or {}).get("cells") or []
    scaffold_cells = _ensure_list(gnbdu.get("NRCellDU"))
    if yaml_cells and not scaffold_cells:
        raise ValueError("scaffold GNBDUFunction has no NRCellDU to overlay onto")

    overlaid = []
    for index, yaml_cell in enumerate(yaml_cells):
        if index < len(scaffold_cells):
            cell_xml = scaffold_cells[index]
        else:  # more cells than the scaffold provides: clone the first as a template
            cell_xml = copy.deepcopy(scaffold_cells[0])
            cell_xml["id"] = f"nrcelldu{index + 1}"
            _write(cell_xml.setdefault("attributes", {}), "cellLocalId", str(index), written)
        ofh_cell = ofh_cells[index] if index < len(ofh_cells) else None
        _overlay_cell(cell_xml, yaml_cell, ofh_cell, written)
        overlaid.append(cell_xml)

    # Keep any scaffold cells beyond the YAML's cell count untouched. A cell_cfg-only YAML overlays no
    # cell at all, in which case the scaffold's NRCellDU (if any) is left exactly as it is.
    if overlaid:
        gnbdu["NRCellDU"] = overlaid + scaffold_cells[len(overlaid) :]
    elif scaffold_cells:
        gnbdu["NRCellDU"] = scaffold_cells

    # Only a cell_cfg that carries slicing keeps the RRM policies alive. The forward path renders the
    # whole cell_cfg block -- plmn, tac and the slicing list with its sched_cfg -- out of
    # NRCellDU/RRMPolicyRatio, so overlaying a slice-less cell_cfg would keep a policy the YAML never
    # asked for and render back a default slicing block that was not in it. plmn and tac round-trip
    # through the cell itself either way (pLMNInfoList, nRTAC).
    cell_cfg = yaml_cfg.get("cell_cfg") or {}
    if cell_cfg.get("slicing"):
        if isinstance(gnbdu.get("RRMPolicyRatio"), dict):
            _overlay_rrm_policy(gnbdu["RRMPolicyRatio"].setdefault("attributes", {}), cell_cfg, written)
        for cell_xml in _ensure_list(gnbdu.get("NRCellDU")):
            rrm = cell_xml.get("RRMPolicyRatio")
            if isinstance(rrm, dict):
                _overlay_rrm_policy(rrm.setdefault("attributes", {}), cell_cfg, written)


def _overlay_endpoint(function, ep_key, written, bind_addr=None, peer_addr=None):
    """Overlay bind/peer addresses onto an EP_* element; returns its attributes, or None if absent."""
    endpoint = function.get(ep_key)
    if not isinstance(endpoint, dict):
        logging.warning("Scaffold has no %s to overlay onto; keeping the scaffold's addresses", ep_key)
        return None

    attrs = endpoint.setdefault("attributes", {})
    if bind_addr is not None:
        _write(attrs.setdefault("localAddress", {}), "ipAddress", _scalar_to_text(bind_addr), written)
    if peer_addr is not None:
        _write(attrs, "remoteAddress", _scalar_to_text(peer_addr), written)
    return attrs


def _bind_addr(block, name):
    """Return the bind address of an ngu/f1u socket list; an EP_* endpoint carries exactly one."""
    sockets = block.get("socket") or []
    if len(sockets) > 1:
        logging.warning("cu_up.%s has %d sockets; only the first one maps onto the endpoint", name, len(sockets))
    return sockets[0].get("bind_addr") if sockets else None


# pylint: disable=too-many-locals,too-many-branches
def overlay_cuup(cu_up, gnbcuup, written):
    """Overlay the cu_up block of a srsRAN YAML config onto a scaffold GNBCUUPFunction element, in place."""
    plmn_list = cu_up.get("plmn_list") or []
    if plmn_list:
        # pLMNInfoList is keyed by mcc/mnc/sd/sst: only the PLMN halves are overlaid, the scaffold
        # keeps its own slice keys. One entry per scaffold entry, in order.
        plmn_infos = _ensure_list((gnbcuup.get("attributes") or {}).get("pLMNInfoList"))
        if not plmn_infos:
            logging.warning("Scaffold GNBCUUPFunction has no pLMNInfoList; dropping cu_up.plmn_list")
        elif len(plmn_list) > len(plmn_infos):
            logging.warning(
                "cu_up.plmn_list has %d entries but the scaffold declares %d pLMNInfoList entries; "
                "only the first %d map onto it",
                len(plmn_list),
                len(plmn_infos),
                len(plmn_infos),
            )
        for plmn_info, plmn in zip(plmn_infos, plmn_list):
            split = _split_plmn(plmn)
            if split is None:
                logging.warning(
                    "cu_up.plmn_list entry %r is not an unambiguous string; keeping the scaffold's mcc/mnc. "
                    'Quote the value (e.g. - "00101") to override it.',
                    plmn,
                )
            else:
                _write(plmn_info, "mcc", split[0], written)
                _write(plmn_info, "mnc", split[1], written)

    e1ap = cu_up.get("e1ap") or {}
    if e1ap:
        # The adapter renders e1ap as a 'gateways' list; a hand-written CU-UP YAML uses the flat form.
        gateway = (e1ap.get("gateways") or [e1ap])[0]
        _overlay_endpoint(gnbcuup, "EP_E1", written, gateway.get("bind_addrs"), gateway.get("addrs"))

    ngu = cu_up.get("ngu") or {}
    if ngu:  # no remoteAddress: the N3/NG-U peer (UPF) is signalled by the core
        _overlay_endpoint(gnbcuup, "EP_NgU", written, _bind_addr(ngu, "ngu"))

    f1u = cu_up.get("f1u") or {}
    if f1u:  # no remoteAddress either: the F1-U peer is learned via F1AP signaling
        attrs = _overlay_endpoint(gnbcuup, "EP_F1U", written, _bind_addr(f1u, "f1u"))
        ports = {key: f1u[key] for key in ("bind_port", "peer_port") if key in f1u}
        if ports and attrs is not None:
            # The ports live in an OCUDU extension container, which carries its own namespace: only
            # overlay them when the scaffold already declares it, never invent an unnamespaced one.
            if isinstance(attrs.get("ocudu_ep_f1u_extensions"), dict):
                _merge(attrs, "ocudu_ep_f1u_extensions", ports, written)
            else:
                logging.warning("Scaffold EP_F1U has no ocudu_ep_f1u_extensions; dropping %s", ", ".join(ports))

    test_mode = cu_up.get("test_mode")
    if test_mode:
        ext = gnbcuup.get("ocudu_gnbcuupfunction_extensions")
        if isinstance(ext, dict):
            _merge(ext, "ocudu_gnbcuupfunction_testmode_extensions", test_mode, written)
        else:
            logging.warning("Scaffold GNBCUUPFunction has no extension container; dropping cu_up.test_mode")

    for key in cu_up:
        if key not in ("plmn_list", "e1ap", "ngu", "f1u", "test_mode"):
            logging.warning("Ignoring unrecognised cu_up key %r", key)


# pylint: disable=too-many-branches
def overlay_cucp(cu_cp, gnbcucp, written):
    """Overlay the cu_cp block of a srsRAN YAML config onto a scaffold GNBCUCPFunction element, in place."""
    ext = gnbcucp.get("ocudu_gnbcucpfunction_extensions")
    amf = cu_cp.get("amf") or {}
    if amf:
        ngc_attrs = _overlay_endpoint(gnbcucp, "EP_NgC", written, amf.get("bind_addrs"), amf.get("addrs"))
        # Everything else flat under amf (port, bind_interface, no_core, SCTP tuning, timeouts) mirrors
        # the OCUDU EP_NgC extension one-for-one.
        knobs = {key: value for key, value in amf.items() if key not in _AMF_ENDPOINT_KEYS}
        if knobs and ngc_attrs is not None:
            if isinstance(ngc_attrs.get("ocudu_ep_ngc_extensions"), dict):
                _merge(ngc_attrs, "ocudu_ep_ngc_extensions", knobs, written)
            else:
                logging.warning("Scaffold EP_NgC has no ocudu_ep_ngc_extensions; dropping %s", ", ".join(knobs))

        areas = amf.get("supported_tracking_areas") or []
        if len(areas) > 1:
            # The NRM carries a single TAC per CU-CP (the OCUDU nRTAC extension), so extra areas have
            # nowhere to go.
            logging.warning("cu_cp.amf has %d supported_tracking_areas; only the first one maps on", len(areas))
        if areas:
            _overlay_tracking_area(areas[0], gnbcucp, ext, written)

    for section, ep_key in (("e1ap", "EP_E1"), ("f1ap", "EP_F1C")):
        block = cu_cp.get(section) or {}
        if block:  # no remoteAddress: the CU-CP terminates E1 and F1-C as a server (bind only)
            _overlay_endpoint(gnbcucp, ep_key, written, block.get("bind_addrs"))
        for key in block:
            if key != "bind_addrs":
                logging.warning("Ignoring cu_cp.%s.%s: the forward path only renders bind_addrs", section, key)

    if cu_cp.get("mobility"):
        # Not inverted: mobility spreads over report_configs plus per-NRCellCU relations and their DNs.
        # Keep the nodes it is rendered from exactly as the scaffold has them.
        logging.warning("No overlay for cu_cp.mobility; left as the scaffold has it")
        _keep_uninverted(gnbcucp, _MOBILITY_NODES, written)

    for key in cu_cp:
        if key not in ("amf", "e1ap", "f1ap", "mobility"):
            logging.warning("Ignoring unrecognised cu_cp key %r", key)


def _overlay_tracking_area(area, gnbcucp, ext, written):
    """Overlay one cu_cp.amf.supported_tracking_areas entry: tac -> nRTAC, plmn + slices -> RRMPolicyRatio."""
    if "tac" in area:
        if isinstance(ext, dict):
            _write(ext, "nRTAC", _scalar_to_text(area["tac"]), written)
        else:
            logging.warning("Scaffold GNBCUCPFunction has no extension container; dropping the tracking area tac")

    plmns = area.get("plmn_list") or []
    if len(plmns) > 1:
        logging.warning("A tracking area carries %d plmn_list entries; only the first one maps on", len(plmns))
    if not plmns:
        return

    split = _split_plmn(plmns[0].get("plmn"))
    if split is None:
        logging.warning(
            "cu_cp.amf plmn %r is not an unambiguous string; keeping the scaffold's mcc/mnc. "
            'Quote the value (e.g. plmn: "00101") to override it.',
            plmns[0].get("plmn"),
        )
    elif isinstance(gnbcucp.get("attributes", {}).get("pLMNId"), dict):
        _write(gnbcucp["attributes"]["pLMNId"], "mcc", split[0], written)
        _write(gnbcucp["attributes"]["pLMNId"], "mnc", split[1], written)

    # tai_slice_support_list -> one rRMPolicyMemberList entry per slice, which is where the forward path
    # reads sst/sd back from. The members are keyed by mcc/mnc/sd/sst, so each one carries the PLMN too.
    slices = plmns[0].get("tai_slice_support_list") or []
    rrm = gnbcucp.get("RRMPolicyRatio")
    if not slices or not isinstance(rrm, dict):
        return
    rrm_attrs = rrm.setdefault("attributes", {})
    template = _ensure_list(rrm_attrs.get("rRMPolicyMemberList"))
    members = []
    for entry in slices:
        member = copy.deepcopy(template[0]) if template else {}
        if split is not None:
            _write(member, "mcc", split[0], written)
            _write(member, "mnc", split[1], written)
        if "sst" in entry:
            _write(member, "sst", _scalar_to_text(entry["sst"]), written)
        if "sd" in entry:
            _write(member, "sd", _sd_to_hex(entry["sd"]), written)
        members.append(member)
    _write(rrm_attrs, "rRMPolicyMemberList", members, written)


def _function_extensions(managed_element):
    """Yield each present function's OCUDU extension container, in forward-path (DU, CU-CP, CU-UP) order."""
    for func_key, ext_key in _FUNCTION_EXTENSIONS:
        function = managed_element.get(func_key)
        if isinstance(function, dict) and isinstance(function.get(ext_key), dict):
            yield function[ext_key]


def _overlay_log(managed_element, log_cfg, written):
    """Overlay the top-level log block onto the function extension that carries ocudu_log_extensions.

    The forward path takes the first log extension it finds walking DU -> CU-CP -> CU-UP, so the
    inverse writes back to that same one; if no function declares it, the first extension container
    present gets it.
    """
    target = None
    for ext in _function_extensions(managed_element):
        if "ocudu_log_extensions" in ext:
            target = ext
            break
        if target is None:
            target = ext

    if target is None:
        logging.warning("Scaffold has no function extension container; dropping the log block")
        return
    _merge(target, "ocudu_log_extensions", log_cfg, written)


def _overlay_pcap(managed_element, pcap_cfg, written):
    """Overlay the flat top-level pcap block onto the per-function ocudu_pcap_extensions.

    The forward path flattens <channel><leaf> into '<channel>_<leaf>' and merges the channels of every
    function into one YAML block, so each key is routed back to whichever function declares that
    channel. The longest matching channel name wins, so 'e2ap_cu_up_filename' beats 'e2ap'.
    """
    channels: Dict[str, list] = {}
    for ext in _function_extensions(managed_element):
        for name, channel in (ext.get("ocudu_pcap_extensions") or {}).items():
            if isinstance(channel, dict):
                channels.setdefault(name, []).append(channel)

    for key, value in pcap_cfg.items():
        match = None
        for name in channels:
            if key.startswith(name + "_") and (match is None or len(name) > len(match)):
                match = name
        if match is None:
            logging.warning("Ignoring pcap key %r: the scaffold declares no matching channel", key)
            continue

        leaf = key[len(match) + 1 :]
        text = _scalar_to_text(value)
        if leaf == "enable" and text in ("enable", "disable"):
            # OCUDU takes 'enable'/'disable' for the pcap switches as well as a plain bool, but the YANG
            # leaf is a boolean and libyang rejects anything else.
            text = "true" if text == "enable" else "false"
        for channel in channels[match]:
            _write(channel, leaf, text, written)


def _overlay_flat_sections(managed_element, yaml_cfg, written):
    """Overlay every _FLAT_SECTIONS block the YAML carries onto the container it mirrors.

    Returns the sections that were overlaid, for the caller's report. A section whose container no
    function declares is dropped rather than invented: the container carries its own namespace, and the
    scaffold is the only thing that knows which function the block belongs to.
    """
    overlaid = []
    for section, container in _FLAT_SECTIONS.items():
        block = yaml_cfg.get(section)
        if not block:
            continue
        targets = [ext for ext in _function_extensions(managed_element) if isinstance(ext.get(container), dict)]
        if not targets:
            logging.warning("Scaffold declares no %s; dropping the %s block", container, section)
            continue
        for ext in targets:
            _merge(ext, container, block, written)
        overlaid.append(section)
    return overlaid


def _holds_write(node, written):
    """True if the overlay wrote anything anywhere under this node."""
    for key, value in node.items():
        if (id(node), key) in written:
            return True
        for entry in value if isinstance(value, list) else [value]:
            if isinstance(entry, dict) and _holds_write(entry, written):
                return True
    return False


def _is_object(value):
    """True for a 3GPP NRM object (an IOC instance), which the models always key by 'id'."""
    for entry in value if isinstance(value, list) else [value]:
        if isinstance(entry, dict) and "id" in entry:
            return True
    return False


def _keep_uninverted(node, keep_nodes, written):
    """Record the nodes feeding YAML sections this module cannot invert, so pruning leaves them intact.

    Marking them as written is exactly the claim being made: the YAML does determine this subtree, just
    not leaf by leaf, so the pruner must treat it as the YAML's own content.
    """
    for key, value in node.items():
        if key in keep_nodes:
            written.add((id(node), key))
        for entry in value if isinstance(value, list) else [value]:
            if isinstance(entry, dict):
                _keep_uninverted(entry, keep_nodes, written)


def _prune(node, written, extension=False):
    """Drop everything the YAML does not determine from a scaffold node, in place.

    The scaffold mixes two kinds of content, so pruning applies two different rules:

    - Inside an OCUDU extension container (and anywhere below it) only the written leaves survive. Those
      containers are pure adapter payload -- every leaf is there to become a YAML key -- so a leaf the
      YAML does not carry is redundant by definition.
    - Everywhere else the 3GPP NRM structure of a surviving object is left intact: its attributes are the
      mandatory leaves, list keys and DNs that the YAML never carries but that the result would not
      validate without. Pruning is per object there -- an object the overlay never wrote into is dropped
      whole, as is any list entry that holds nothing, which is what takes the scaffold's spare cells,
      endpoints and policies out.
    """
    for key in list(node):
        value = node[key]
        if (id(node), key) in written or key.startswith("@") or key in _LIST_KEYS:
            # The YAML's own value (kept whole, rebuilt lists included), a namespace, or a list key.
            # Checked first because a key leaf can be the very thing the YAML determines: EP_*
            # localAddress is keyed by ipAddress, which is exactly where a bind address lands.
            continue

        if not isinstance(value, (dict, list)):
            if extension:
                del node[key]  # extension leaf with no YAML value behind it
            continue

        child_extension = extension or key.startswith(_OCUDU_PREFIX)
        entries = [entry for entry in (value if isinstance(value, list) else [value]) if isinstance(entry, dict)]
        if child_extension or _is_object(value):
            entries = [entry for entry in entries if _holds_write(entry, written)]
            if not entries:
                del node[key]
                continue
            node[key] = entries if isinstance(value, list) else entries[0]
        for entry in entries:
            _prune(entry, written, child_extension)


def overlay(yaml_cfg, scaffold, minimal=True):
    """Overlay a srsRAN YAML config onto a parsed scaffold (xmltodict dict), in place.

    Only the functions the scaffold provides and the YAML carries are touched. A YAML section whose
    function is missing from the scaffold is an error rather than a silent drop -- that means the seed
    does not match the split the YAML was written for.

    With `minimal` (the default) the scaffold is then pruned back to what the YAML actually determines,
    so the result is the smallest config that reproduces this YAML rather than a full seed with a few
    values swapped; `minimal=False` keeps the whole scaffold.
    """
    managed_element = scaffold.get("ManagedElement")
    if not isinstance(managed_element, dict):
        raise ValueError("scaffold has no ManagedElement to overlay onto")

    du_cfg = any(key in yaml_cfg for key in ("cells", "ru_ofh", "cell_cfg"))
    cu_cp = yaml_cfg.get("cu_cp") or {}
    cu_up = yaml_cfg.get("cu_up") or {}
    log_cfg = yaml_cfg.get("log")
    pcap_cfg = yaml_cfg.get("pcap")
    written: set = set()

    overlaid = []
    if du_cfg:
        gnbdu = managed_element.get("GNBDUFunction")
        if not isinstance(gnbdu, dict):
            raise ValueError("YAML carries a DU config but the scaffold has no ManagedElement/GNBDUFunction")
        overlay_du(yaml_cfg, gnbdu, written)
        overlaid.append("GNBDUFunction")
    if cu_cp:
        gnbcucp = managed_element.get("GNBCUCPFunction")
        if not isinstance(gnbcucp, dict):
            raise ValueError("YAML carries a cu_cp config but the scaffold has no ManagedElement/GNBCUCPFunction")
        overlay_cucp(cu_cp, gnbcucp, written)
        overlaid.append("GNBCUCPFunction")
    if cu_up:
        gnbcuup = managed_element.get("GNBCUUPFunction")
        if not isinstance(gnbcuup, dict):
            raise ValueError("YAML carries a cu_up config but the scaffold has no ManagedElement/GNBCUUPFunction")
        overlay_cuup(cu_up, gnbcuup, written)
        overlaid.append("GNBCUUPFunction")
    if log_cfg:
        _overlay_log(managed_element, log_cfg, written)
        overlaid.append("log")
    if pcap_cfg:
        _overlay_pcap(managed_element, pcap_cfg, written)
        overlaid.append("pcap")
    overlaid.extend(_overlay_flat_sections(managed_element, yaml_cfg, written))

    if not overlaid:
        raise ValueError(f"YAML carries none of the overlayable sections ({', '.join(_HANDLED_SECTIONS)})")
    logging.debug("Overlaid %s", ", ".join(overlaid))

    unhandled = [key for key in yaml_cfg if key not in _HANDLED_SECTIONS]
    if unhandled:
        logging.warning("No overlay for top-level section(s) %s; left as the scaffold has them", ", ".join(unhandled))

    if minimal:
        keep_nodes = {_UNINVERTED_SECTIONS[key] for key in unhandled if key in _UNINVERTED_SECTIONS}
        _keep_uninverted(managed_element, keep_nodes, written)
        _prune(managed_element, written)

    return scaffold


# REUSE-IgnoreStart
_LICENSE_HEADER = (
    "<!--\n"
    "SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited\n"
    "SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors\n"
    "SPDX-License-Identifier: BSD-3-Clause-Open-MPI\n"
    "-->\n"
)
# REUSE-IgnoreEnd
_EDIT_CONFIG_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"


def build_netconf(yaml_cfg, scaffold_xml, edit_config=False, minimal=True):
    """Return a NETCONF XML string built from a YAML config dict and a scaffold XML string."""
    scaffold = xmltodict.parse(scaffold_xml)
    overlay(yaml_cfg, scaffold, minimal=minimal)

    if edit_config:  # wrap in <config> for a netconf edit-config payload
        managed_element = scaffold["ManagedElement"]
        managed_element["@xmlns:nc"] = _EDIT_CONFIG_NS
        scaffold = {"config": {"@xmlns": _EDIT_CONFIG_NS, "ManagedElement": managed_element}}

    body = xmltodict.unparse(scaffold, pretty=True, indent="  ", full_document=False)
    return _LICENSE_HEADER + body + "\n"


def main(argv=None):
    """CLI entry point: overlay a YAML config onto a seed XML and write the result out."""
    parser = argparse.ArgumentParser(description="Render a srsRAN gNB/CU/DU YAML config into a NETCONF XML config.")
    parser.add_argument("-i", "--input", required=True, help="Input srsRAN YAML config file")
    parser.add_argument(
        "-s",
        "--scaffold",
        required=True,
        help="Base seed NETCONF XML (source of the mandatory 3GPP NRM structure) matching the split the YAML "
        "was written for, e.g. tests/configs/du/du_config_1.xml or tests/configs/cuup/cuup_config_1.xml",
    )
    parser.add_argument("-o", "--output", help="Output XML file (default: stdout)")
    parser.add_argument(
        "--edit-config",
        action="store_true",
        help="Wrap the result in <config> for use as a NETCONF edit-config payload",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Emit the whole overlaid scaffold instead of pruning it back to the leaves the YAML determines",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Log overlay warnings")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s: %(message)s")

    with open(args.input, encoding="utf-8") as handle:
        yaml_cfg = yaml.safe_load(handle)
    with open(args.scaffold, encoding="utf-8") as handle:
        scaffold_xml = handle.read()

    try:
        xml = build_netconf(yaml_cfg, scaffold_xml, edit_config=args.edit_config, minimal=not args.full)
    except ValueError as e:  # scaffold/YAML mismatch: a user error, not a crash
        logging.error("%s", e)
        return 1

    if args.output:
        with open(args.output, mode="w", encoding="utf-8") as handle:
            handle.write(xml)
    else:
        sys.stdout.write(xml)
    return 0


if __name__ == "__main__":
    sys.exit(main())
