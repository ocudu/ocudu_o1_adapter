# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""This module provides a ConfigManager class to manage and update the configuration of a gNB / CU / DU.

The NETCONF-tree -> gNB YAML projection itself lives in netconf_to_yaml; this module owns the
side-effecting half: the notification loop, restart-vs-runtime classification, template
rendering and RU forwarding.
"""

import asyncio
import logging
from typing import Any, Dict

import ncclient
import xmltodict
from deepdiff import DeepDiff
from jinja2 import Environment, exceptions as jinja2_exceptions, FileSystemLoader

from netconf_to_yaml import (
    active_stream_ids,
    extract_cell_config,
    extract_cells_config,
    extract_cucp_config,
    extract_cuup_config,
    extract_perf_metric_jobs,
    extract_rrm_policy_ratio_config,
    extract_ssb_runtime_config,
)
from remote_commands import WsRemoteCommands
from state import AppState

# take_notification() blocks off the event loop and returns the instant ncclient
# queues a server-pushed <notification>, so config changes are handled event-driven
# with ~0 latency. This timeout is only a wake-up tick that lets the worker re-check
# the stop event / connection state between notifications; it is not added latency.
NOTIFICATION_TICK = 1  # seconds


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

    _RUNTIME_UPDATABLE_PARAMS = ["ssb_block_power_dbm", "RRMPolicyRatio", "PerfMetricJob"]
    _FULL_RESTART_TIMEOUT = 30

    def __init__(
        self,
        state: AppState,
        netconf_manager,
        datastore,
        output_filename,
        template_filename,
        ru_forward_enabled,
        profile="gnb",
    ):
        # execute the base constructor
        self.netconf_manager = netconf_manager
        self.datastore = datastore
        self.output_filename = output_filename
        self.template_filename = template_filename
        self.last_config = None
        self.state = state
        self._ws = WsRemoteCommands(state.ws_send_queue)  # Inject shared WS send queue
        self._ru_forward_enabled = ru_forward_enabled
        self._render_enabled = profile != "ru"
        self._profile = profile

    async def process_config_update(self):
        """
        Retrieve full config
        """
        raw_xml = self.netconf_manager.get_config(source=self.datastore).data_xml
        raw_config = xmltodict.parse(raw_xml)

        # Create diff between old and new config
        diff = DeepDiff(self.last_config, raw_config, ignore_order=True)
        if not diff:
            logging.debug("No config change detected")
            return

        # RU profile: forward raw config only, skip gNB-shaped diff classification.
        if not self._render_enabled:
            self.write_full_config(raw_config, raw_xml)
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
            self.write_full_config(raw_config, raw_xml)
        else:
            logging.debug("Full restart needed, sending quit cmd")
            await self._full_restart()
            # _full_restart blocks until WS reconnects; netconf may have changed meanwhile, so refetch.
            self.write_full_config(None)

    async def _full_restart(self):
        if not self._ws.send_quit_command():
            logging.warning("Failed to send quit command")
            self.state.restart_req = True
        else:
            # Mark restart requested immediately for health endpoint consumers.
            self.state.restart_req = True
            logging.info("Waiting for the reboot to complete ..")
            start_time = asyncio.get_running_loop().time()
            while not self.state.session_state.get("ws_connected"):
                if asyncio.get_running_loop().time() - start_time > self._FULL_RESTART_TIMEOUT:
                    logging.warning("Timed out waiting for WebSocket reconnect")
                    self.state.restart_req = True
                    return
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

            # Send SSB update command
            self._ws.send_ssb_command(extract_ssb_runtime_config(raw_config))

        if "RRMPolicyRatio" in str(diff):
            logging.info("Updating RRM policy")
            rrm_policy_config = extract_rrm_policy_ratio_config(raw_config)

            # Send update command
            self._ws.send_rrm_policy_ratio_command(rrm_policy_config)

    # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    def write_full_config(self, raw_config, raw_xml=None):
        """
        Writes the full configuration to a file based on the provided raw configuration.

        Args:
            raw_config (dict): The raw configuration data.
            raw_xml (str, optional): The raw NETCONF XML payload.
        """
        if raw_config is None:
            if raw_xml is None:
                raw_xml = self.netconf_manager.get_config(source=self.datastore).data_xml
            raw_config = xmltodict.parse(raw_xml)

        logging.debug(f"RAW xml:\n{raw_config}")

        # Store config
        self.last_config = raw_config

        prev_active = active_stream_ids(self.state.pm_jobs)
        self.state.pm_jobs = extract_perf_metric_jobs(raw_config)
        # Newly-active jobs need a fresh snapshot from the gNB (one-shot on subscribe).
        if active_stream_ids(self.state.pm_jobs) - prev_active:
            self._ws.send_metrics_subscribe()

        if not self._render_enabled:
            self._enqueue_ru_forward_update(raw_xml)
            return True

        ofh_cell_config, du_cell_config = extract_cells_config(raw_config)
        cell_config = extract_cell_config(raw_config, du_cell_config)
        cucp_config = extract_cucp_config(raw_config, du_cell_config)
        cuup_config = extract_cuup_config(raw_config)

        # DU F1 addresses (standalone 'du' app only; gnb/cu wire F1 in-process). The DU is the
        # F1-C client: EP_F1C remoteAddress -> f1ap.addrs, localAddress -> f1ap.bind_addrs. The DU's
        # own F1-U bind address comes from EP_F1U localAddress -> f1u.socket[].bind_addr.
        f1ap_config = {}
        f1u_config = {}
        if self._profile == "du":
            try:
                ep_f1c = raw_config["data"]["ManagedElement"]["GNBDUFunction"]["EP_F1C"]["attributes"]
                f1ap_config = {
                    "addrs": ep_f1c["remoteAddress"],
                    "bind_addrs": ep_f1c["localAddress"]["ipAddress"],
                }
            except KeyError as e:
                logging.warning(f"Couldn't extract DU F1-C config: {e}")
            try:
                ep_f1u = raw_config["data"]["ManagedElement"]["GNBDUFunction"]["EP_F1U"]["attributes"]
                f1u_config = {"socket": [{"bind_addr": ep_f1u["localAddress"]["ipAddress"]}]}
                # Optional GTP-U bind/peer UDP ports (OCUDU EP_F1U extension). When absent the
                # gNB applies its own default (2152), so only emit them when explicitly set.
                ports = ep_f1u.get("ocudu_ep_f1u_extensions") or {}
                if "bind_port" in ports:
                    f1u_config["bind_port"] = ports["bind_port"]
                if "peer_port" in ports:
                    f1u_config["peer_port"] = ports["peer_port"]
            except KeyError as e:
                logging.warning(f"Couldn't extract DU F1-U config: {e}")

        # Function extensions (DU/CU-CP/CU-UP). testmode/hal/remote_control live only on
        # the DU; log lives on any function (first-wins); pcap entries are merged across
        # all functions; metrics_extensions is now per-function and gets merged
        testmode_config = {"enabled": False}
        log_config: Dict[str, Any] = {}
        hal_config = {}
        metrics_config: Dict[str, Any] = {}
        remote_control_config = {}
        pcap_config: Dict[str, Any] = {}
        ru_dummy_config = {}
        ru_sdr_config = {}

        managed_element = raw_config.get("data", {}).get("ManagedElement", {})
        for func_key, ext_key in (
            ("GNBDUFunction", "ocudu_gnbdufunction_extensions"),
            ("GNBCUCPFunction", "ocudu_gnbcucpfunction_extensions"),
            ("GNBCUUPFunction", "ocudu_gnbcuupfunction_extensions"),
        ):
            try:
                ext = managed_element[func_key][ext_key]
            except (KeyError, TypeError):
                continue

            testmode = ext.get("ocudu_gnbdufunction_testmode_extensions")
            if testmode is not None:
                testmode_config = testmode
            hal = ext.get("ocudu_hal_extensions")
            if hal is not None:
                hal_config = hal
            remote = ext.get("ocudu_remote_control_extensions")
            if remote is not None:
                remote_control_config = remote
            ru_dummy = ext.get("ocudu_ru_dummy_extensions")
            if ru_dummy is not None:
                ru_dummy_config = ru_dummy
            ru_sdr = ext.get("ocudu_ru_sdr_extensions")
            if ru_sdr is not None:
                ru_sdr_config = ru_sdr

            if not log_config:
                log = ext.get("ocudu_log_extensions")
                if log is not None:
                    log_config = log

            metrics = ext.get("ocudu_metrics_extensions")
            if metrics is not None:
                for key, value in metrics.items():
                    if key in ("periodicity", "layers") and isinstance(value, dict):
                        metrics_config.setdefault(key, {}).update(value)
                    else:
                        metrics_config[key] = value

            pcap = ext.get("ocudu_pcap_extensions") or {}
            pcap_config.update(pcap)

        # Render config file
        try:
            environment = Environment(loader=FileSystemLoader("templates/"))
            template = environment.get_template(self.template_filename)
            content = template.render(
                ofh_cells=ofh_cell_config,
                du_cells=du_cell_config,
                cucp_config=cucp_config,
                cuup_config=cuup_config,
                testmode_config=testmode_config,
                log_config=log_config,
                hal_config=hal_config,
                metrics_config=metrics_config,
                remote_control_config=remote_control_config,
                pcap_config=pcap_config,
                cell_config=cell_config,
                ru_dummy_config=ru_dummy_config,
                ru_sdr_config=ru_sdr_config,
                f1ap_config=f1ap_config,
                f1u_config=f1u_config,
            )
        except jinja2_exceptions.UndefinedError as e:
            logging.error(f"Template rendering error: {e}")
            return False

        with open(self.output_filename, mode="w", encoding="utf-8") as message:
            message.write(content)
            logging.info(f"Generating {self.output_filename}")
            logging.debug(f"Generated config:\n{content}")

        self._enqueue_ru_forward_update(raw_xml)

        return True

    def _enqueue_ru_forward_update(self, raw_xml):
        """Queue latest NETCONF config for RU forwarding."""
        if not self._ru_forward_enabled or raw_xml is None:
            return

        dropped = 0
        while not self.state.ru_update_queue.empty():
            try:
                self.state.ru_update_queue.get_nowait()
                self.state.ru_update_queue.task_done()
                dropped += 1
            except asyncio.QueueEmpty:
                break

        self.state.ru_update_queue.put_nowait(raw_xml)
        if dropped:
            logging.debug(f"Dropped {dropped} stale RU forwarding update(s)")
        logging.debug("Queued RU forwarding update")

    async def run(self, stop_event: asyncio.Event):
        """Main run loop of the config manager worker.

        Config changes are consumed event-driven: take_notification() waits on a
        worker thread (so the event loop isn't blocked) and returns the instant
        ncclient queues a server-pushed <notification>, so the change is processed
        with essentially no latency and without polling the running config.
        """
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

                # Wait for a notification on a worker thread so the event loop isn't
                # blocked. A queued notification returns immediately; the tick timeout
                # only bounds how fast we notice stop_event / a dropped connection
                # between changes.
                notif = await asyncio.to_thread(self.netconf_manager.take_notification, True, NOTIFICATION_TICK)
                if notif is None:
                    continue

                logging.info(f"NETCONF notification received:\n{notif.notification_xml}")
                if "netconf-config-change" not in notif.notification_xml:
                    continue

                await self.process_config_update()

        finally:
            logging.debug("Worker stopped")
