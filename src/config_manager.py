#!/usr/bin/python3
# SPDX-License-Identifier: BSD 3-Clause Open MPI variant

"""This module provides a ConfigManager class to manage and update the configuration of a gNB / CU / DU."""

import asyncio
import logging

import ncclient
import xmltodict
from deepdiff import DeepDiff
from jinja2 import Environment, exceptions as jinja2_exceptions, FileSystemLoader

from remote_commands import WsRemoteCommands
from state import AppState

RETRY_INTERVAL = 5  # seconds
WORKER_SLEEP_INTERVAL = 5  # seconds


# pylint: disable=too-many-instance-attributes,logging-fstring-interpolation,too-many-arguments,too-many-positional-arguments
class ConfigManager:
    """
    ConfigManager is responsible for managing and updating the configuration of a gNB / CU / DU.
    It retrieves the configuration, detects changes, and updates the runtime configuration
    or triggers a full restart if necessary. The updated configuration is written to a file.

    Attributes:
        netconf_manager (NetconfManager): The NETCONF manager instance for retrieving configurations.
        datastore (str): The datastore source for the NETCONF configuration.
        output_filename (str): The filename where the full configuration will be written.
        template_filename (str): The filename of the template used for rendering the configuration file.
    """

    _RUNTIME_UPDATABLE_PARAMS = ["ssb_block_power_dbm", "RRMPolicyRatio"]
    _FULL_RESTART_TIMEOUT = 30

    def __init__(self, state: AppState, netconf_manager, datastore, output_filename, template_filename):
        # execute the base constructor
        self.netconf_manager = netconf_manager
        self.datastore = datastore
        self.output_filename = output_filename
        self.template_filename = template_filename
        self.last_config = None
        self.state = state
        self._ws = WsRemoteCommands(state.ws_send_queue)  # Inject shared WS send queue

    async def process_config_update(self):
        """
        Retrieve full config
        """
        raw_config = xmltodict.parse(self.netconf_manager.get_config(source=self.datastore).data_xml)

        # Create diff between old and new config
        diff = DeepDiff(self.last_config, raw_config, ignore_order=True)
        if not diff:
            logging.debug("No config change detected")
            return

        # Check if only runtime updatable parameters changed
        runtime_updatable = True
        logging.debug(f"Config change detected: {diff}")
        try:
            for _, change in diff.items():
                for key, _ in change.items():
                    param_found = False
                    for item in self._RUNTIME_UPDATABLE_PARAMS:
                        if item in key:
                            param_found = True
                            break
                    if not param_found:
                        runtime_updatable = False
                        break

        except (AttributeError, TypeError) as e:
            logging.debug(f"Couldn't determine if parameters can be updated: {e}")
            runtime_updatable = False

        if runtime_updatable:
            logging.debug("Only runtime updatable parameters changed, no need to restart application")
            self.update_runtime_config(raw_config, diff)
        else:
            logging.debug("Full restart needed, sending quit cmd")
            await self._full_restart()

        # Write full config to file
        self.write_full_config(raw_config)

    async def _full_restart(self):
        if not self._ws.send_quit_command():
            logging.warning("Failed to send quit command")
            self.state.restart_req = True
        else:
            logging.info("Waiting for the reboot to complete ..")
            await asyncio.sleep(1)

    def update_runtime_config(self, raw_config, diff):
        """
        Updates the runtime configuration of the DU cell.

        Args:
            raw_config (dict): The raw configuration data.
            diff (DeepDiff): The differences between the old and new configuration.
        """
        if "ssb_block_power_dbm" in str(diff):
            logging.info("Updating SSB power")

            nc_du_cell_config = self.get_du_cell_config(raw_config)

            # Only extract runtime-updateable values
            du_cell_config = []
            for cell in nc_du_cell_config:
                try:
                    nc_cell_extension = cell["attributes"]["srs_nrcelldu_extensions"]
                except KeyError as e:
                    logging.warning(f"Couldn't extract srsRAN nrcelldu config extensions: {e}")

                # build DU cell struct to overwrite common cell_cfg fields with individual values
                new_du_cell = {}
                try:
                    for key, value in nc_cell_extension["srs_nrcelldu_ssb_extensions"].items():
                        value = int(value) if value.isnumeric() else value
                        new_du_cell[key] = int(value)
                except KeyError as e:
                    logging.warning(f"Couldn't extract srsRAN SSB config extensions: {e}")

                # Extract cell-specific values from standard attributes
                try:
                    new_du_cell["plmn"] = (
                        cell["attributes"]["pLMNInfoList"]["mcc"] + cell["attributes"]["pLMNInfoList"]["mnc"]
                    )
                    new_du_cell["nci"] = int(
                        raw_config["data"]["ManagedElement"]["GNBDUFunction"]["attributes"]["gNBId"]
                        + cell["attributes"]["cellLocalId"]
                    )
                except KeyError as e:
                    logging.warning(f"Couldn't extract PLMN and NCI from GNBDUFunction attributes: {e}")
                du_cell_config.append(new_du_cell)

            # Send SSB update command
            self._ws.send_ssb_command(du_cell_config)

        if "RRMPolicyRatio" in str(diff):
            logging.info("Updating RRM policy")
            rrm_policy_config = self._extract_rrm_policy_ratio_config(raw_config)

            # Send update command
            self._ws.send_rrm_policy_ratio_command(rrm_policy_config)

    def get_du_cell_config(self, raw_config):
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

    # pylint: disable=too-many-locals
    def write_full_config(self, raw_config):
        """
        Writes the full configuration to a file based on the provided raw configuration.

        Args:
            raw_config (dict): The raw configuration data.
        """
        if raw_config is None:
            raw_config = xmltodict.parse(self.netconf_manager.get_config(source=self.datastore).data_xml)

        logging.debug(f"RAW xml:\n{raw_config}")

        # Store config
        self.last_config = raw_config

        ofh_cell_config, du_cell_config = self._extract_cells_config(raw_config)
        cell_config = self._extract_cell_config(raw_config, du_cell_config)
        cucp_config = self._extract_cucp_config(raw_config, du_cell_config, cell_config)

        # GNBDUFunction extensions
        testmode_config = {"enabled": False}
        log_config = {}
        hal_config = {}
        metrics_config = {}
        remote_control_config = {}
        try:
            srs_gnbdufunction_extensions = raw_config["data"]["ManagedElement"]["GNBDUFunction"][
                "srs_gnbdufunction_extensions"
            ]
            testmode_config = srs_gnbdufunction_extensions["srs_gnbdufunction_testmode_extensions"]
            log_config = srs_gnbdufunction_extensions["srs_gnbdufunction_log_extensions"]
            hal_config = srs_gnbdufunction_extensions["srs_hal_extensions"]
            metrics_config = srs_gnbdufunction_extensions["srs_metrics_extensions"]
            remote_control_config = srs_gnbdufunction_extensions["srs_remote_control_extensions"]
        except KeyError as e:
            logging.warning(f"Couldn't extract srsRAN GNBDUFunction extensions: {e}")

        # Render config file
        try:
            environment = Environment(loader=FileSystemLoader("templates/"))
            template = environment.get_template(self.template_filename)
            content = template.render(
                ofh_cells=ofh_cell_config,
                du_cells=du_cell_config,
                cucp_config=cucp_config,
                testmode_config=testmode_config,
                log_config=log_config,
                hal_config=hal_config,
                metrics_config=metrics_config,
                remote_control_config=remote_control_config,
                cell_config=cell_config,
            )
        except jinja2_exceptions.UndefinedError as e:
            logging.error(f"Template rendering error: {e}")
            return False

        with open(self.output_filename, mode="w", encoding="utf-8") as message:
            message.write(content)
            logging.info(f"Generating {self.output_filename}")
            logging.debug(f"Generated config:\n{content}")

        return True

    def _extract_cucp_config(self, raw_config, du_cells=None, cell_cfg=None):
        cucp_config = {}
        try:

            nc_cucp_config = raw_config["data"]["ManagedElement"]["GNBCUCPFunction"]
            logging.debug(nc_cucp_config)

            plmn = nc_cucp_config["attributes"]["pLMNId"]["mcc"] + nc_cucp_config["attributes"]["pLMNId"]["mnc"]

            tac = 7  # Not present in default YANG model it seems
            if du_cells is not None:
                for cell in du_cells:
                    if cell["plmn"] == plmn:
                        tac = cell["tac"]
                        break

            tai_slice_support_list = [
                {
                    "sst": 1,  # Default value, can be overwritten by cell config
                }
            ]

            if cell_cfg is not None:
                tai_slice_support_list = []
                if cell_cfg["plmn"] == plmn:
                    for s in cell_cfg["slicing"]:
                        tai_slice_support_list.append({"sst": s["sst"], "sd": s["sd"]})

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
            cucp_config = {
                "amf": {
                    "addr": nc_cucp_config["EP_NgC"]["attributes"]["remoteAddress"],
                    "bind_addr": nc_cucp_config["EP_NgC"]["attributes"]["localAddress"]["ipAddress"],
                    "supported_tracking_areas": supported_tracking_areas,
                }
            }
        except KeyError as e:
            logging.warning(f"Couldn't extract CU-CP config: {e}")

        return cucp_config

    def _extract_rrm_policy_ratio_config(self, raw_config):
        cfg = {}
        try:
            rrm_policy_config = raw_config["data"]["RRMPolicyRatio"]["attributes"]

            # Build config subtree
            cfg = {
                "resourceType": rrm_policy_config["resourceType"],
                "rRMPolicyMemberList": [
                    {
                        "plmn": rrm_policy_config["rRMPolicyMemberList"]["mcc"]
                        + rrm_policy_config["rRMPolicyMemberList"]["mnc"],
                        "sst": int(rrm_policy_config["rRMPolicyMemberList"]["sst"]),
                        "sd": int(rrm_policy_config["rRMPolicyMemberList"]["sd"], 16),
                    },
                ],
                "min_prb_policy_ratio": int(rrm_policy_config["rRMPolicyMinRatio"]),
                "max_prb_policy_ratio": int(rrm_policy_config["rRMPolicyMaxRatio"]),
                "dedicated_ratio": int(rrm_policy_config["rRMPolicyDedicatedRatio"]),
            }

        except (KeyError, ValueError) as e:
            logging.warning(f"Couldn't extract srsRAN RRM policy config: {e}")

        return cfg

    def _extract_cell_config(self, raw_config, du_cells=None):
        cell_cfg = {}
        try:
            rrm_policy_config = raw_config["data"]["RRMPolicyRatio"]["attributes"]

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
                        "sd": rrm_policy_config["rRMPolicyMemberList"]["sd"],
                        "sched_cfg": {
                            "min_prb_policy_ratio": rrm_policy_config["rRMPolicyMinRatio"],
                            "max_prb_policy_ratio": rrm_policy_config["rRMPolicyMaxRatio"],
                        },
                    },
                ],
            }

        except KeyError as e:
            logging.warning(f"Couldn't extract srsRAN RRM policy config: {e}")

        return cell_cfg

    def _extract_cells_config(self, raw_config):  # pylint: disable=too-many-branches
        # Iterate over DU cell and build extract OFH and DU config values
        ofh_cell_config = []
        du_cell_config = []
        for cell in self.get_du_cell_config(raw_config):
            try:
                nc_cell_extension = cell["attributes"]["srs_nrcelldu_extensions"]
            except KeyError as e:
                logging.warning(f"Couldn't extract srsRAN nrcelldu config extensions: {e}")

            # Extract custom srsRAN extensions
            try:
                # build OFH cell struct
                new_ofh_cell = {}
                for key, value in nc_cell_extension["srs_nrcelldu_ofh_extensions"].items():
                    if "compr_method" in key:
                        value = "bfp" if "BLOCK_FLOATING_POINT" in value else value
                    if "static_compr_hdr" in key:
                        value = "true" if "STATIC" in value else "false"
                    new_ofh_cell[key] = value
                ofh_cell_config.append(new_ofh_cell)
            except KeyError as e:
                logging.warning(f"Couldn't extract srsRAN OFH config extensions: {e}")

            # build DU cell struct to overwrite common cell_cfg fields with individual values
            new_du_cell = {}

            try:
                ssb_fields = {}
                for key, value in nc_cell_extension["srs_nrcelldu_ssb_extensions"].items():
                    ssb_fields[key] = value
                new_du_cell["ssb"] = ssb_fields
            except KeyError as e:
                logging.warning(f"Couldn't extract srsRAN SSB config extensions: {e}")

            try:
                prach_fields = {}
                for key, value in nc_cell_extension["srs_nrcelldu_prach_extensions"].items():
                    prach_fields[key] = value
                new_du_cell["prach"] = prach_fields
            except KeyError as e:
                logging.warning(f"Couldn't extract srsRAN PRACH config extensions: {e}")

            try:
                tdd_fields = {}
                for key, value in nc_cell_extension["srs_nrcelldu_tdd_extensions"].items():
                    tdd_fields[key] = value
                new_du_cell["tdd_ul_dl_cfg"] = tdd_fields
            except KeyError as e:
                logging.warning(f"Couldn't extract srsRAN TDD config extensions: {e}")

            try:
                for key, value in nc_cell_extension["srs_nrcelldu_base_extensions"].items():
                    if "scs" in key:
                        value = "".join(filter(str.isdigit, value))
                    new_du_cell[key] = value
            except KeyError as e:
                logging.warning(f"Couldn't extract srsRAN cell base extensions: {e}")

            # Extract values from standard attributes
            new_du_cell.update(
                {
                    "pci": cell["attributes"]["nRPCI"],
                    "tac": cell["attributes"]["nRTAC"],
                    "dl_arfcn": cell["attributes"]["arfcnDL"],
                    "channel_bandwidth_MHz": cell["attributes"]["bSChannelBwDL"],
                    "plmn": cell["attributes"]["pLMNInfoList"]["mcc"] + cell["attributes"]["pLMNInfoList"]["mnc"],
                    "enabled": cell["attributes"]["administrativeState"] != "LOCKED",
                }
            )
            du_cell_config.append(new_du_cell)
        return ofh_cell_config, du_cell_config

    async def run(self, stop_event: asyncio.Event):
        """Main run loop of the config manager worker."""
        logging.debug("Worker started")
        try:
            # Subscribe to notifications (default NETCONF stream)
            try:
                self.netconf_manager.create_subscription(stream_name="NETCONF", filter=None)
                logging.info("Subscribed to NETCONF notifications")
            except (ncclient.operations.OperationError, ncclient.operations.TimeoutExpiredError) as e:
                logging.warning(f"Failed to subscribe: {e}")

            while not stop_event.is_set():
                if not self.netconf_manager.connected:
                    logging.info("NETCONF session no longer connected (worker)")
                    break

                # Listen for notifications (non-blocking with timeout)
                try:
                    notif = self.netconf_manager.take_notification(timeout=1)
                    if notif is not None:
                        if "netconf-config-change" in notif.notification_xml:
                            logging.debug("netconf-config-change notification received")
                            # Queue event to update config
                            await self.state.app_command_queue.put(notif)

                except ncclient.operations.TimeoutExpiredError:
                    # timeout or transport closed
                    pass

                # Handle commands from main thread
                try:
                    cmd = self.state.app_command_queue.get_nowait()
                except asyncio.QueueEmpty:
                    cmd = None

                if cmd:
                    logging.debug("Processing config update")
                    await self.process_config_update()

                # Periodic heartbeat
                logging.debug("Worker heartbeat")
                await asyncio.sleep(WORKER_SLEEP_INTERVAL)

        finally:
            logging.debug("Worker stopped")
