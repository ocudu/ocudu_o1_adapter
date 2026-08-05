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
from typing import Any, Dict

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


def _t1_thres_to_unix_ms(value):
    """Normalise a t1_thres value to Unix milliseconds as int."""
    # The YANG type accepts either Unix ms or an RFC 3339 timestamp; the gNB only accepts Unix ms.
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
            pdcch_ext = nc_cell_extension["ocudu_nrcelldu_pdcch_extensions"]
            # Emit each pdcch sub-container as a YAML flow mapping so the template's 2-level loop renders it.
            pdcch_fields = {}
            common = pdcch_ext.get("common")
            if common:
                pdcch_fields["common"] = "{" + ", ".join(f"{k}: {v}" for k, v in common.items()) + "}"
            dedicated = pdcch_ext.get("dedicated")
            if dedicated:
                pdcch_fields["dedicated"] = "{" + ", ".join(f"{k}: {v}" for k, v in dedicated.items()) + "}"
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
            mcg_ext = nc_cell_extension["ocudu_nrcelldu_mac_cell_group_extensions"]
            # Emit each mac_cell_group sub-container as a YAML flow mapping
            # so the template's 2-level loop renders it.
            mcg_fields = {}
            bsr_cfg = mcg_ext.get("bsr_cfg")
            if bsr_cfg:
                mcg_fields["bsr_cfg"] = "{" + ", ".join(f"{k}: {v}" for k, v in bsr_cfg.items()) + "}"
            phr_cfg = mcg_ext.get("phr_cfg")
            if phr_cfg:
                mcg_fields["phr_cfg"] = "{" + ", ".join(f"{k}: {v}" for k, v in phr_cfg.items()) + "}"
            sr_cfg = mcg_ext.get("sr_cfg")
            if sr_cfg:
                mcg_fields["sr_cfg"] = "{" + ", ".join(f"{k}: {v}" for k, v in sr_cfg.items()) + "}"
            new_du_cell["mac_cell_group"] = mcg_fields
        except (KeyError, TypeError) as e:
            logging.warning(f"Couldn't extract OCUDU MAC cell group config extensions: {e}")

        try:
            sib_ext = nc_cell_extension["ocudu_nrcelldu_sib_extensions"]
            sib_fields = {}
            for key, value in sib_ext.items():
                if key in ("etws", "cmas"):
                    # Nested sub-container -> YAML flow mapping so the template's 2-level loop renders it.
                    sib_fields[key] = "{" + ", ".join(f"{k}: {v}" for k, v in value.items()) + "}"
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
                        rendered.append("{" + ", ".join(f"{k}: {v}" for k, v in fields.items()) + "}")
                    sib_fields[key] = "[" + ", ".join(rendered) + "]"
                else:
                    sib_fields[key] = value
            new_du_cell["sib"] = sib_fields
        except (KeyError, TypeError) as e:
            logging.warning(f"Couldn't extract OCUDU SIB config extensions: {e}")

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
                        value = _t1_thres_to_unix_ms(value)
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
