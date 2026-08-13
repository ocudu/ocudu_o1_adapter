# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Projection of a 3GPP NRM NETCONF tree onto the OCUDU gNB YAML config subtrees.

Pure functions over the xmltodict-parsed <get-config> payload: each one takes the raw
config dict and returns the plain dicts/lists that the gNB YAML template renders. They
hold no state and perform no I/O.

The reverse direction (gNB YAML -> NETCONF tree) lives in yaml_to_netconf; the
side-effecting half of this forward path (notification loop, restart handling, template
rendering, RU forwarding) lives in config_manager.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from xml_utils import ensure_list

# pylint: disable=logging-fstring-interpolation
# The extraction functions mirror the shape of the NRM tree they walk, so several of them
# are legitimately branch- and local-heavy.
# pylint: disable=too-many-branches,too-many-locals,too-many-statements


def parse_sd(sd):
    """Parse a YANG-encoded 3GPP slice differentiator into an integer."""
    # 3GPP SD is 3 octets; YANG model encodes it as colon-separated hex bytes
    # (e.g. "ff:ff:ff"). Strip the separators before parsing as a hex integer.
    return int(sd.replace(":", ""), 16)


def _timestamp_to_unix_ms(value):
    """Normalise a YANG timestamp leaf to Unix milliseconds as int.

    Used for every union(date-and-time, uint64) timestamp we forward: mobility t1_thres and the
    NTN epochTime / t_service. The gNB's own ISO 8601 parser accepts no timezone designator and
    assumes UTC, so an RFC 3339 value with a 'Z' or a numeric offset cannot be passed through
    verbatim; Unix ms is the one encoding both sides read unambiguously.
    """
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _coerce_xml_value(value):
    """Convert xmltodict scalars to YAML-friendly Python values."""
    # Recursively walk dicts/lists and turn numeric-looking strings into int/float.
    if isinstance(value, dict):
        return {key: _coerce_xml_value(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_coerce_xml_value(val) for val in value]
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
        try:
            return float(text)
        except ValueError:
            return value
    return value


def extract_top_level_config(raw_config):
    """Extract top-level identifiers from the 3GPP YANG attributes.

    Extracted keys: gnb_id, gnb_id_bit_length, ran_node_name, gnb_cu_up_id, gnb_du_id.

    gNBId/gNBIdLength are duplicated on every function (CU-CP/CU-UP/DU). Their cross-function
    equality is enforced by the gnb-id-consistency YANG deviation, so we just take the first
    function we find them on.
    """
    header = {}
    managed_element = raw_config.get("data", {}).get("ManagedElement", {})

    for func_key in ("GNBCUCPFunction", "GNBCUUPFunction", "GNBDUFunction"):
        try:
            attrs = managed_element[func_key]["attributes"]
        except (KeyError, TypeError):
            continue
        if "gNBId" in attrs:
            header["gnb_id"] = int(attrs["gNBId"])
        if "gNBIdLength" in attrs:
            header["gnb_id_bit_length"] = int(attrs["gNBIdLength"])
        if "gnb_id" in header and "gnb_id_bit_length" in header:
            break

    try:
        name = managed_element["GNBCUCPFunction"]["attributes"].get("gNBCUName")
        if name:
            header["ran_node_name"] = name
    except (KeyError, TypeError):
        pass

    try:
        cu_up_id = managed_element["GNBCUUPFunction"]["attributes"].get("gNBCUUPId")
        if cu_up_id is not None:
            header["gnb_cu_up_id"] = int(cu_up_id)
    except (KeyError, TypeError):
        pass

    try:
        du_attrs = managed_element["GNBDUFunction"]["attributes"]
        du_id = du_attrs.get("gNBDUId")
        if du_id is not None:
            header["gnb_du_id"] = int(du_id)
    except (KeyError, TypeError):
        pass

    return header


# 3GPP EphemerisInfos leaf -> the gNB's ephemeris key, for each arm of the
# positionVelocity-or-orbital choice. Both arms are all-or-nothing: several of the 3GPP leaves carry
# a default of 0, so a partially-configured arm would silently render as a valid-looking but wrong
# state vector. We only emit an arm when every leaf of it came back from the server.
_ECEF_LEAVES = {
    "positionX": "pos_x",
    "positionY": "pos_y",
    "positionZ": "pos_z",
    "velocityVX": "vel_x",
    "velocityVY": "vel_y",
    "velocityVZ": "vel_z",
}

_ORBITAL_LEAVES = {
    "semiMajorAxis": "semi_major_axis",
    "eccentricity": "eccentricity",
    "periapsis": "periapsis",
    "longitude": "longitude",
    "inclination": "inclination",
    "meanAnomaly": "mean_anomaly",
}


def _flow_mapping(fields):
    """Render a dict as a YAML flow mapping so the template's 2-level loop can emit it."""
    return "{" + ", ".join(f"{key}: {value}" for key, value in fields.items()) + "}"


def _ephemeris_arm(entry, leaves):
    """Project one arm of the EphemerisInfos choice, or None when it is absent/incomplete."""
    # Each arm is a max-elements-1 list keyed by idx, so unwrap the single entry and drop the key.
    arms = ensure_list(entry)
    if not arms or not all(leaf in arms[0] for leaf in leaves):
        return None
    return {key: arms[0][leaf] for leaf, key in leaves.items()}


def extract_ntn_satellites(raw_config):
    """Build the top-level ntn.satellites list from ManagedElement/NTNFunction.

    One 3GPP EphemerisInfoSet/EphemerisInfos entry becomes one gNB satellite object, which cells
    reference by satellite_idx. The standard leaves carry the ephemeris; propagator_type,
    gateway_location and ta_info come from the OCUDU augment on the same list entry.
    """
    satellites: List[Dict[str, Any]] = []
    try:
        ntn_functions = ensure_list(raw_config["data"]["ManagedElement"]["NTNFunction"])
    except KeyError:
        return satellites

    for ntn_function in ntn_functions:
        for info_set in ensure_list(ntn_function.get("EphemerisInfoSet")):
            for entry in ensure_list(info_set.get("attributes", {}).get("EphemerisInfos")):
                satellite: Dict[str, Any] = {}

                # satelliteId is a zero-padded 5-digit string; the gNB indexes satellites by integer.
                if "satelliteId" in entry:
                    try:
                        satellite["satellite_idx"] = int(entry["satelliteId"])
                    except ValueError as e:
                        logging.warning(f"Skipping NTN satellite with unparsable satelliteId: {e}")
                        continue
                if "epochTime" in entry:
                    satellite["epoch_timestamp"] = _timestamp_to_unix_ms(entry["epochTime"])

                ecef = _ephemeris_arm(entry.get("positionVelocity"), _ECEF_LEAVES)
                if ecef:
                    satellite["ephemeris_info_ecef"] = _flow_mapping(ecef)
                orbital = _ephemeris_arm(entry.get("orbital"), _ORBITAL_LEAVES)
                if orbital:
                    satellite["ephemeris_orbital"] = _flow_mapping(orbital)
                if not ecef and not orbital:
                    # A satellite the gNB cannot propagate; emitting it would only produce a config
                    # the gNB rejects, so drop it loudly instead of rendering a broken entry.
                    logging.warning(
                        f"Skipping NTN satellite {entry.get('satelliteId')}: "
                        "neither positionVelocity nor orbital is fully configured"
                    )
                    continue

                # The augment's leaves keep their names: propagator_type is a scalar, while
                # gateway_location / ta_info are sub-containers that render as flow mappings.
                for key, value in (entry.get("ocudu_ntn_satellite_extensions") or {}).items():
                    if key.startswith("@"):  # skip xmltodict namespace attrs (@xmlns)
                        continue
                    satellite[key] = _flow_mapping(value) if isinstance(value, dict) else value

                if satellite:
                    satellites.append(satellite)

    return satellites


def get_du_cell_config(raw_config):
    """
    Extracts the full set of configuration parameters for each DU cell from the raw NETCONF configuration.

    Args:
        raw_config (dict): The raw NETCONF configuration data.

    Returns:
        list: A list of configuration parameters for each DU cell.
    """
    nc_du_cell_config = []
    try:
        if isinstance(raw_config["data"]["ManagedElement"]["GNBDUFunction"]["NRCellDU"], list):
            for cell in raw_config["data"]["ManagedElement"]["GNBDUFunction"]["NRCellDU"]:
                nc_du_cell_config.append(cell)
        else:
            nc_du_cell_config.append(raw_config["data"]["ManagedElement"]["GNBDUFunction"]["NRCellDU"])
    except KeyError as e:
        logging.info(f"Couldn't extract GNBDUFunction/NRCellDU config: {e}")

    for cell in nc_du_cell_config:
        logging.debug(cell)

    return nc_du_cell_config


def extract_cells_config(raw_config):
    """Build the per-cell OFH and DU config lists from the NRCellDU subtree.

    Returns:
        tuple: (ofh_cell_config, du_cell_config) lists, one entry per DU cell.
    """
    # Iterate over DU cell and build extract OFH and DU config values
    ofh_cell_config = []
    du_cell_config = []
    # pylint: disable=too-many-nested-blocks
    for cell in get_du_cell_config(raw_config):
        try:
            nc_cell_extension = cell["attributes"]["ocudu_nrcelldu_extensions"]
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU nrcelldu config extensions: {e}")
            nc_cell_extension = {}

        # Extract custom ocudu extensions
        try:
            # build OFH cell struct
            new_ofh_cell = {}
            for key, value in nc_cell_extension["ocudu_nrcelldu_ofh_extensions"].items():
                if "compr_method" in key:
                    value = "bfp" if "BLOCK_FLOATING_POINT" in value else value
                if "static_compr_hdr" in key:
                    value = "true" if "STATIC" in value else "false"
                new_ofh_cell[key] = value
            ofh_cell_config.append(new_ofh_cell)
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU OFH config extensions: {e}")

        # build DU cell struct to overwrite common cell_cfg fields with individual values
        new_du_cell = {}

        try:
            ssb_fields = {}
            for key, value in nc_cell_extension["ocudu_nrcelldu_ssb_extensions"].items():
                ssb_fields[key] = value
            new_du_cell["ssb"] = ssb_fields
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU SSB config extensions: {e}")

        try:
            prach_fields = {}
            for key, value in nc_cell_extension["ocudu_nrcelldu_prach_extensions"].items():
                prach_fields[key] = value
            new_du_cell["prach"] = prach_fields
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU PRACH config extensions: {e}")

        try:
            tdd_fields = {}
            for key, value in nc_cell_extension["ocudu_nrcelldu_tdd_extensions"].items():
                if key == "pattern2":
                    # Optional nested pattern -> emit as a YAML flow mapping for the 2-level template.
                    tdd_fields["pattern2"] = "{" + ", ".join(f"{k}: {v}" for k, v in value.items()) + "}"
                else:
                    tdd_fields[key] = value
            new_du_cell["tdd_ul_dl_cfg"] = tdd_fields
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU TDD config extensions: {e}")

        try:
            pdsch_fields = {}
            for key, value in nc_cell_extension["ocudu_nrcelldu_pdsch_extensions"].items():
                if key == "rv_sequence":
                    # Leaf-list: NETCONF returns a list (or a bare scalar for a single entry).
                    # OCUDU expects a flow sequence of integers, e.g. [0, 2, 3, 1].
                    rv_values = value if isinstance(value, list) else [value]
                    value = [int(rv) for rv in rv_values]
                pdsch_fields[key] = value
            new_du_cell["pdsch"] = pdsch_fields
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU PDSCH config extensions: {e}")

        try:
            pusch_fields = {}
            for key, value in nc_cell_extension["ocudu_nrcelldu_pusch_extensions"].items():
                if key == "rv_sequence":
                    # Leaf-list: NETCONF returns a list (or a bare scalar for a single entry).
                    # OCUDU expects a flow sequence of integers, e.g. [0, 2, 3, 1].
                    rv_values = value if isinstance(value, list) else [value]
                    value = [int(rv) for rv in rv_values]
                pusch_fields[key] = value
            new_du_cell["pusch"] = pusch_fields
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU PUSCH config extensions: {e}")

        try:
            pucch_fields = {}
            for key, value in nc_cell_extension["ocudu_nrcelldu_pucch_extensions"].items():
                pucch_fields[key] = value
            new_du_cell["pucch"] = pucch_fields
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU PUCCH config extensions: {e}")

        try:
            csi_fields = {}
            for key, value in nc_cell_extension["ocudu_nrcelldu_csi_extensions"].items():
                csi_fields[key] = value
            new_du_cell["csi"] = csi_fields
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU CSI config extensions: {e}")

        try:
            srs_fields = {}
            for key, value in nc_cell_extension["ocudu_nrcelldu_srs_extensions"].items():
                srs_fields[key] = value
            new_du_cell["srs"] = srs_fields
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU SRS config extensions: {e}")

        try:
            # Emit each pdcch sub-container (common, dedicated) as a YAML flow mapping so the
            # template's 2-level loop renders it.
            pdcch_fields = {}
            for key, value in nc_cell_extension["ocudu_nrcelldu_pdcch_extensions"].items():
                if value:
                    pdcch_fields[key] = _flow_mapping(value) if isinstance(value, dict) else value
            new_du_cell["pdcch"] = pdcch_fields
        except (KeyError, TypeError) as e:
            logging.warning(f"Couldn't extract OCUDU PDCCH config extensions: {e}")

        try:
            paging_fields = {}
            for key, value in nc_cell_extension["ocudu_nrcelldu_paging_extensions"].items():
                paging_fields[key] = value
            new_du_cell["paging"] = paging_fields
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU paging config extensions: {e}")

        try:
            drx_fields = {}
            for key, value in nc_cell_extension["ocudu_nrcelldu_drx_extensions"].items():
                drx_fields[key] = value
            new_du_cell["drx"] = drx_fields
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU DRX config extensions: {e}")

        try:
            sched_ext = nc_cell_extension["ocudu_nrcelldu_scheduler_extensions"]
            sched_fields = {}
            if "nof_preselected_newtx_ues" in sched_ext:
                sched_fields["nof_preselected_newtx_ues"] = sched_ext["nof_preselected_newtx_ues"]
            # policy is a choice (qos_sched|rr_sched); emit as a nested YAML flow mapping.
            policy = sched_ext.get("policy")
            if policy:
                qos_sched = policy.get("qos_sched")
                if qos_sched:
                    sched_fields["policy"] = (
                        "{qos_sched: {" + ", ".join(f"{k}: {v}" for k, v in qos_sched.items()) + "}}"
                    )
                elif "rr_sched" in policy:
                    sched_fields["policy"] = "{rr_sched: {}}"
            new_du_cell["scheduler"] = sched_fields
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU scheduler config extensions: {e}")

        try:
            ta_fields = {}
            for key, value in nc_cell_extension["ocudu_nrcelldu_ta_extensions"].items():
                ta_fields[key] = value
            new_du_cell["ta"] = ta_fields
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU TA config extensions: {e}")

        try:
            mcg_ext = nc_cell_extension["ocudu_nrcelldu_mac_cell_group_extensions"]
            # Emit each mac_cell_group sub-container as a YAML flow mapping
            # so the template's 2-level loop renders it.
            mcg_fields = {}
            for key, value in mcg_ext.items():
                if value:
                    mcg_fields[key] = _flow_mapping(value) if isinstance(value, dict) else value
            new_du_cell["mac_cell_group"] = mcg_fields
        except (KeyError, TypeError) as e:
            logging.warning(f"Couldn't extract OCUDU MAC cell group config extensions: {e}")

        try:
            sib_ext = nc_cell_extension["ocudu_nrcelldu_sib_extensions"]
            sib_fields = {}
            for key, value in sib_ext.items():
                if key in ("etws", "cmas"):
                    # Nested sub-container -> YAML flow mapping so the template's 2-level loop renders it.
                    sib_fields[key] = _flow_mapping(value)
                elif key == "si_sched_info":
                    # List of SI-message entries -> flow sequence of flow mappings. Drop the ordering key
                    # and render the sib_mapping leaf-list as an inline integer list.
                    entries = value if isinstance(value, list) else [value]
                    rendered = []
                    for entry in entries:
                        fields = {k: v for k, v in entry.items() if k != "id"}
                        mapping = fields.get("sib_mapping")
                        if mapping is not None:
                            mapping = mapping if isinstance(mapping, list) else [mapping]
                            fields["sib_mapping"] = "[" + ", ".join(str(int(m)) for m in mapping) + "]"
                        rendered.append(_flow_mapping(fields))
                    sib_fields[key] = "[" + ", ".join(rendered) + "]"
                else:
                    sib_fields[key] = value
            new_du_cell["sib"] = sib_fields
        except (KeyError, TypeError) as e:
            logging.warning(f"Couldn't extract OCUDU SIB config extensions: {e}")

        try:
            ntn_ext = nc_cell_extension["ocudu_nrcelldu_ntn_extensions"]
            ntn_fields = {}
            for key, value in ntn_ext.items():
                if key.startswith("@"):  # skip xmltodict namespace attrs (@xmlns)
                    continue
                if key == "t_service":
                    ntn_fields[key] = _timestamp_to_unix_ms(value)
                else:
                    # Sub-containers (epoch_time, reference_location, polarization, feeder_link)
                    # -> YAML flow mapping so the template's 2-level loop renders them.
                    ntn_fields[key] = _flow_mapping(value) if isinstance(value, dict) else value
            new_du_cell["ntn"] = ntn_fields
        except KeyError as e:
            logging.debug(f"No OCUDU NTN config extensions for this cell: {e}")

        try:
            for key, value in nc_cell_extension["ocudu_nrcelldu_base_extensions"].items():
                if "scs" in key:
                    value = "".join(filter(str.isdigit, value))
                new_du_cell[key] = value
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU cell base extensions: {e}")

        # Standard attributes. nRTAC/bSChannelBwDL are optional and administrativeState defaults
        # to LOCKED (and may be omitted by get-config), so guard the block and use that default.
        try:
            new_du_cell.update(
                {
                    "pci": cell["attributes"]["nRPCI"],
                    "tac": cell["attributes"]["nRTAC"],
                    "dl_arfcn": cell["attributes"]["arfcnDL"],
                    "channel_bandwidth_MHz": cell["attributes"]["bSChannelBwDL"],
                    "plmn": cell["attributes"]["pLMNInfoList"]["mcc"] + cell["attributes"]["pLMNInfoList"]["mnc"],
                    "enabled": cell["attributes"].get("administrativeState", "LOCKED") != "LOCKED",
                }
            )
        except KeyError as e:
            logging.warning(f"Couldn't extract NRCellDU standard attributes: {e}")
        du_cell_config.append(new_du_cell)
    return ofh_cell_config, du_cell_config


def extract_cell_config(raw_config, du_cells=None):
    """Build the common cell_cfg subtree (tac/plmn/slicing) from the DU RRMPolicyRatio."""
    cell_cfg = {}
    try:
        rrm_policy_config = raw_config["data"]["ManagedElement"]["GNBDUFunction"]["NRCellDU"]["RRMPolicyRatio"][
            "attributes"
        ]

        plmn = rrm_policy_config["rRMPolicyMemberList"]["mcc"] + rrm_policy_config["rRMPolicyMemberList"]["mnc"]

        tac = 7  # Not present in default YANG model it seems
        if du_cells is not None:
            for cell in du_cells:
                if cell["plmn"] == plmn:
                    tac = cell["tac"]
                    break

        # Build cell config subtree
        cell_cfg = {
            "tac": tac,
            "plmn": plmn,
            "slicing": [
                {
                    "sst": rrm_policy_config["rRMPolicyMemberList"]["sst"],
                    "sd": parse_sd(rrm_policy_config["rRMPolicyMemberList"]["sd"]),
                    "sched_cfg": {
                        "min_prb_policy_ratio": rrm_policy_config["rRMPolicyMinRatio"],
                        "max_prb_policy_ratio": rrm_policy_config["rRMPolicyMaxRatio"],
                    },
                },
            ],
        }

    except (KeyError, ValueError) as e:
        logging.warning(f"Couldn't extract OCUDU RRM policy config: {e}")

    return cell_cfg


def extract_ssb_runtime_config(raw_config):
    """Build the per-cell SSB payload sent as a runtime update to the gNB."""
    # Only extract runtime-updateable values
    du_cell_config = []
    for cell in get_du_cell_config(raw_config):
        try:
            nc_cell_extension = cell["attributes"]["ocudu_nrcelldu_extensions"]
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU nrcelldu config extensions: {e}")
            nc_cell_extension = {}

        # build DU cell struct to overwrite common cell_cfg fields with individual values
        new_du_cell = {}
        try:
            for key, value in nc_cell_extension["ocudu_nrcelldu_ssb_extensions"].items():
                value = int(value) if value.isnumeric() else value
                new_du_cell[key] = int(value)
        except KeyError as e:
            logging.warning(f"Couldn't extract OCUDU SSB config extensions: {e}")

        # Extract cell-specific values from standard attributes
        try:
            new_du_cell["plmn"] = cell["attributes"]["pLMNInfoList"]["mcc"] + cell["attributes"]["pLMNInfoList"]["mnc"]
            new_du_cell["nci"] = int(
                raw_config["data"]["ManagedElement"]["GNBDUFunction"]["attributes"]["gNBId"]
                + cell["attributes"]["cellLocalId"]
            )
        except KeyError as e:
            logging.warning(f"Couldn't extract PLMN and NCI from GNBDUFunction attributes: {e}")
        du_cell_config.append(new_du_cell)

    return du_cell_config


def extract_rrm_policy_ratio_config(raw_config):
    """Build the RRM policy ratio payload sent as a runtime update to the gNB."""
    cfg = {}
    try:
        rrm_policy_config = raw_config["data"]["ManagedElement"]["GNBDUFunction"]["NRCellDU"]["RRMPolicyRatio"][
            "attributes"
        ]

        # Build config subtree
        cfg = {
            "resourceType": rrm_policy_config["resourceType"],
            "rRMPolicyMemberList": [
                {
                    "plmn": rrm_policy_config["rRMPolicyMemberList"]["mcc"]
                    + rrm_policy_config["rRMPolicyMemberList"]["mnc"],
                    "sst": int(rrm_policy_config["rRMPolicyMemberList"]["sst"]),
                    "sd": parse_sd(rrm_policy_config["rRMPolicyMemberList"]["sd"]),
                },
            ],
            "min_prb_policy_ratio": int(rrm_policy_config["rRMPolicyMinRatio"]),
            "max_prb_policy_ratio": int(rrm_policy_config["rRMPolicyMaxRatio"]),
            "dedicated_ratio": int(rrm_policy_config["rRMPolicyDedicatedRatio"]),
        }

    except (KeyError, ValueError) as e:
        logging.warning(f"Couldn't extract OCUDU RRM policy config: {e}")

    return cfg


def extract_cucp_config(raw_config, du_cells=None):
    """Build the cu_cp subtree (amf, e1ap/f1ap binds, mobility) from GNBCUCPFunction."""
    cucp_config = {}
    try:

        nc_cucp_config = raw_config["data"]["ManagedElement"]["GNBCUCPFunction"]
        logging.debug(nc_cucp_config)

        plmn = nc_cucp_config["attributes"]["pLMNId"]["mcc"] + nc_cucp_config["attributes"]["pLMNId"]["mnc"]

        try:
            tac = nc_cucp_config["ocudu_gnbcucpfunction_extensions"]["nRTAC"]
        except (KeyError, TypeError):
            tac = 7
            if du_cells is not None:
                for cell in du_cells:
                    if cell["plmn"] == plmn:
                        tac = cell["tac"]
                        break

        tai_slice_support_list = [{"sst": 1}]  # Default if RRMPolicyRatio is absent
        try:
            nc_rrm_members = nc_cucp_config["RRMPolicyRatio"]["attributes"]["rRMPolicyMemberList"]
            if not isinstance(nc_rrm_members, list):
                nc_rrm_members = [nc_rrm_members]
            tai_slice_support_list = []
            for member in nc_rrm_members:
                tai_slice_support_list.append({"sst": int(member["sst"]), "sd": parse_sd(member["sd"])})
        except (KeyError, ValueError) as e:
            logging.warning(f"Couldn't extract tai_slice_support_list from GNBCUCPFunction RRMPolicyRatio: {e}")

        supported_tracking_areas = [
            {
                "tac": tac,
                "plmn_list": [
                    {
                        "plmn": plmn,
                        "tai_slice_support_list": tai_slice_support_list,
                    }
                ],
            }
        ]

        # Build AMF config subtree
        ngc_attrs = nc_cucp_config["EP_NgC"]["attributes"]
        cucp_config = {
            "amf": {
                "addrs": ngc_attrs["remoteAddress"],
                "bind_addrs": ngc_attrs["localAddress"]["ipAddress"],
                "supported_tracking_areas": supported_tracking_areas,
            }
        }
        # The OCUDU EP_NgC extension mirrors the remaining flat cu_cp.amf.* knobs (port,
        # bind_interface, SCTP tuning, no_core, timeouts)
        ngc_ext = ngc_attrs.get("ocudu_ep_ngc_extensions") or {}
        for field, value in ngc_ext.items():
            if not field.startswith("@"):  # skip xmltodict namespace attrs (@xmlns)
                cucp_config["amf"][field] = value

        # Top-level cu_cp scalars. Every scalar leaf of the GNBCUCPFunction extension maps onto a
        # cu_cp key of the same name, so they are forwarded by shape rather than by name: the
        # sub-blocks (log, metrics, pcap, remote_control) are containers and are handled elsewhere,
        # and nRTAC is the one scalar that is consumed above instead of rendered.
        cucp_ext = nc_cucp_config.get("ocudu_gnbcucpfunction_extensions") or {}
        for field, value in cucp_ext.items():
            if field == "nRTAC" or field.startswith("@") or isinstance(value, (dict, list)):
                continue
            cucp_config[field] = value

        # cu_cp.rrc mirrors the OCUDU GNBCUCPFunction rrc container one-for-one.
        rrc = cucp_ext.get("rrc")
        if rrc:
            cucp_config["rrc"] = {k: v for k, v in rrc.items() if not k.startswith("@")}

        for ep, key in (("EP_E1", "e1ap"), ("EP_F1C", "f1ap")):
            try:
                cucp_config[key] = {"bind_addrs": nc_cucp_config[ep]["attributes"]["localAddress"]["ipAddress"]}
            except KeyError:
                pass
    except KeyError as e:
        logging.warning(f"Couldn't extract CU-CP config: {e}")

    try:
        mobility = extract_cucp_mobility_config(raw_config)
        if mobility:
            cucp_config["mobility"] = mobility
    except (KeyError, ValueError, TypeError) as e:
        logging.warning(f"Couldn't extract CU-CP mobility config: {e}")

    return cucp_config


def extract_cucp_mobility_config(raw_config):
    """Extract cu_cp.mobility from the NETCONF tree.

    Returns a dict mirroring the gNB YAML mobility block, or None if no
    mobility extension data is present.
    """
    try:
        nc_cucp = raw_config["data"]["ManagedElement"]["GNBCUCPFunction"]
    except KeyError:
        return None

    mob_ext = nc_cucp.get("ocudu_gnbcucpfunction_mobility_extensions")
    cucp_attrs = nc_cucp["attributes"]

    # NCI = (gNBId << (36 - gNBIdLength)) | cellLocalId per 3GPP TS 38.300.
    gnb_id = int(cucp_attrs["gNBId"])
    gnb_id_length = int(cucp_attrs["gNBIdLength"])
    cell_id_shift = 36 - gnb_id_length

    nrcellcu_list = ensure_list(nc_cucp.get("NRCellCU"))

    nci_by_id = {}
    for nrcellcu in nrcellcu_list:
        local_id = int(nrcellcu["attributes"]["cellLocalId"])
        nci_by_id[str(nrcellcu["id"])] = (gnb_id << cell_id_shift) | local_id

    report_configs = []
    if mob_ext:
        for rc in ensure_list(mob_ext.get("report_configs")):
            entry = {}
            for key in (
                "report_cfg_id",
                "report_type",
                "periodic_ho_rsrp_offset_db",
                "event_triggered_report_type",
                "meas_trigger_quantity",
                "meas_trigger_quantity_threshold_db",
                "meas_trigger_quantity_threshold_2_db",
                "meas_trigger_quantity_offset_db",
                "hysteresis_db",
                "time_to_trigger_ms",
                "t312",
                "distance_thresh_from_ref1_km",
                "distance_thresh_from_ref2_km",
                "hysteresis_location_km",
                "ref_location1",
                "ref_location2",
                "t1_thres",
                "duration_s",
                "report_interval_ms",
            ):
                if key in rc:
                    value = rc[key]
                    if key == "t1_thres":
                        value = _timestamp_to_unix_ms(value)
                    else:
                        value = _coerce_xml_value(value)
                    entry[key] = value
            if entry:
                report_configs.append(entry)

    mobility_cells = []
    for nrcellcu in nrcellcu_list:
        nci = nci_by_id.get(str(nrcellcu.get("id")))
        if nci is None:
            continue

        cellcu_mob = nrcellcu.get("attributes", {}).get("ocudu_nrcellcu_mobility_extensions") or {}
        relations = ensure_list(nrcellcu.get("NRCellRelation"))

        cell_entry: Dict[str, Any] = {"nr_cell_id": f"0x{nci:x}"}
        if "periodic_report_cfg_id" in cellcu_mob:
            cell_entry["periodic_report_cfg_id"] = int(cellcu_mob["periodic_report_cfg_id"])

        ncells = []
        for rel in relations:
            rel_attrs = rel.get("attributes", {})
            # Resolve the adjacentNRCellRef DN (e.g. "...,NRCellCU=nrcellcu2") to the target's NCI.
            dn = rel_attrs.get("adjacentNRCellRef") or ""
            target_nci = None
            for component in str(dn).split(","):
                stripped = component.strip()
                if stripped.startswith("NRCellCU="):
                    target_nci = nci_by_id.get(stripped.split("=", 1)[1].strip())
                    break
            if target_nci is None:
                continue
            ncell: Dict[str, Any] = {"nr_cell_id": f"0x{target_nci:x}"}
            refs = rel_attrs.get("ocudu_nrcellrelation_mobility_extensions", {}).get("report_config_refs")
            if refs is not None:
                ncell["report_configs"] = [int(r) for r in ensure_list(refs)]
            ncells.append(ncell)
        if ncells:
            cell_entry["ncells"] = ncells

        if "periodic_report_cfg_id" in cell_entry or "ncells" in cell_entry:
            mobility_cells.append(cell_entry)

    mobility: Dict[str, Any] = {}
    if mob_ext and "trigger_handover_from_measurements" in mob_ext:
        mobility["trigger_handover_from_measurements"] = str(mob_ext["trigger_handover_from_measurements"]).lower()
    if mob_ext and "trigger_cho_on_ue_setup" in mob_ext:
        mobility["trigger_cho_on_ue_setup"] = str(mob_ext["trigger_cho_on_ue_setup"]).lower()
    if mob_ext and "cho_timeout_ms" in mob_ext:
        mobility["cho_timeout_ms"] = int(mob_ext["cho_timeout_ms"])
    if mobility_cells:
        mobility["cells"] = mobility_cells
    if report_configs:
        mobility["report_configs"] = report_configs

    return mobility or None


def extract_cuup_config(raw_config):
    """Build the cu_up subtree (plmn_list, e1ap, ngu/f1u sockets, test_mode) from GNBCUUPFunction."""
    cuup_config: Dict[str, Any] = {}
    try:
        nc_cuup = raw_config["data"]["ManagedElement"]["GNBCUUPFunction"]
    except KeyError:
        return cuup_config

    try:
        # pLMNInfoList is keyed by mcc/mnc/sd/sst, so a multi-slice PLMN repeats once per
        # slice; the CU-UP wants the distinct PLMN IDs (fromkeys dedupes, keeping order).
        plmn_infos = ensure_list(nc_cuup["attributes"]["pLMNInfoList"])
        cuup_config["plmn_list"] = list(dict.fromkeys(info["mcc"] + info["mnc"] for info in plmn_infos))
    except KeyError as e:
        logging.warning(f"Couldn't extract CU-UP PLMN list: {e}")

    try:
        attrs = nc_cuup["EP_E1"]["attributes"]
        cuup_config["e1ap"] = {
            "gateways": [
                {
                    "addrs": attrs["remoteAddress"],
                    "bind_addrs": attrs["localAddress"]["ipAddress"],
                }
            ]
        }
    except KeyError:
        pass

    for ep, key in (("EP_NgU", "ngu"), ("EP_F1U", "f1u")):
        try:
            attrs = nc_cuup[ep]["attributes"]
            cuup_config[key] = {"socket": [{"bind_addr": attrs["localAddress"]["ipAddress"]}]}
        except KeyError:
            continue
        # F1-U carries optional GTP-U bind/peer UDP ports (OCUDU EP_F1U extension). When
        # absent the gNB applies its own default (2152), so only emit them when set.
        if ep == "EP_F1U":
            ports = attrs.get("ocudu_ep_f1u_extensions") or {}
            if "bind_port" in ports:
                cuup_config[key]["bind_port"] = ports["bind_port"]
            if "peer_port" in ports:
                cuup_config[key]["peer_port"] = ports["peer_port"]

    cuup_ext = nc_cuup.get("ocudu_gnbcuupfunction_extensions") or {}
    test_mode = cuup_ext.get("ocudu_gnbcuupfunction_testmode_extensions")
    if test_mode:
        cuup_config["test_mode"] = test_mode

    return cuup_config


def extract_perf_metric_jobs(raw_config) -> dict:
    """Walk ManagedElement/<NF>/PerfMetricJob and return id -> normalised job dict
    (administrativeState, performanceMetrics list, granularityPeriod int, streamTarget,
    nf_key, nf_instance_id, plmn_id)."""
    jobs: dict = {}
    managed_element = raw_config.get("data", {}).get("ManagedElement", {}) or {}
    nf_instance_id = managed_element.get("@id") or managed_element.get("id") or "unknown"
    for nf_key in ("GNBDUFunction", "GNBCUCPFunction", "GNBCUUPFunction"):
        nf = managed_element.get(nf_key)
        if not nf:
            continue
        plmn_attrs = (nf.get("attributes", {}) or {}).get("pLMNId", {}) or {}
        plmn_id = (plmn_attrs.get("mcc") or "") + (plmn_attrs.get("mnc") or "")
        for entry in ensure_list(nf.get("PerfMetricJob")):
            job_id = entry.get("id")
            if not job_id:
                continue
            attrs = entry.get("attributes", {}) or {}
            try:
                granularity = int(attrs.get("granularityPeriod", 1))
            except (TypeError, ValueError):
                granularity = 1
            jobs[job_id] = {
                "administrativeState": attrs.get("administrativeState", "LOCKED"),
                "performanceMetrics": ensure_list(attrs.get("performanceMetrics")),
                "granularityPeriod": granularity,
                "streamTarget": attrs.get("streamTarget"),
                "nf_key": nf_key,
                "nf_instance_id": nf_instance_id,
                "plmn_id": plmn_id,
            }
    return jobs


def active_stream_ids(jobs: dict) -> set:
    """Return the ids of the PerfMetricJobs that are unlocked and have a stream target."""
    return {
        jid for jid, job in jobs.items() if job.get("administrativeState") == "UNLOCKED" and job.get("streamTarget")
    }
