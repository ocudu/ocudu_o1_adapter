#!/usr/bin/python3

# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
This module provides RU NETCONF startup sync and forwarder helpers for the O1 adapter.
"""

import asyncio
import copy
import io
import logging
import xml.etree.ElementTree as ET
from contextlib import suppress

from ncclient import manager
from ncclient.transport.errors import AuthenticationError, SessionCloseError, SSHError

from ru_controller import RuConfig
from state import AppState

SYNC_ALLOWED_NAMESPACES = {
    "urn:ietf:params:xml:ns:netconf:base:1.0",
    "urn:ietf:params:xml:ns:yang:iana-if-type",
    "urn:ietf:params:xml:ns:yang:ietf-interfaces",
    "urn:o-ran:interfaces:1.0",
    "urn:o-ran:performance-management:1.0",
    "urn:o-ran:processing-element:1.0",
    "urn:o-ran:uplane-conf:1.0",
}


class RuForwarder:
    """Handle RU NETCONF startup sync and forwarding of source NETCONF updates."""

    def __init__(self, state: AppState, args, alarm_mgr, retry_interval=5):
        self.state = state
        self.args = args
        self.alarm_mgr = alarm_mgr
        self.retry_interval = retry_interval

    def _data_xml_to_edit_config_xml(self, data_xml, include_namespaces=None):
        """Convert NETCONF <data> payload to NETCONF edit-config <config> payload."""
        data_root, prefixed_ns = self._parse_data_xml(data_xml)
        children = list(data_root)
        if include_namespaces is not None:
            allowed_namespaces = set(include_namespaces)
            children = [child for child in children if self._extract_xml_namespace(child.tag) in allowed_namespaces]

        if not children:
            raise ValueError("No top-level config nodes left after filters")

        return self._build_edit_config_xml(children, prefixed_ns)

    @staticmethod
    def _parse_data_xml(data_xml):
        """Parse NETCONF payload and return resolved <data> root with prefixed namespaces."""
        prefixed_ns = {}
        for _, (prefix, uri) in ET.iterparse(io.StringIO(data_xml), events=("start-ns",)):
            if prefix and prefix not in prefixed_ns:
                prefixed_ns[prefix] = uri

        source_root = ET.fromstring(data_xml)
        source_local_name = source_root.tag.split("}", maxsplit=1)[-1]

        if source_local_name == "data":
            return source_root, prefixed_ns

        if source_local_name == "rpc-reply":
            for child in list(source_root):
                if child.tag.split("}", maxsplit=1)[-1] == "data":
                    return child, prefixed_ns
            raise ValueError("Expected NETCONF <data> inside <rpc-reply>")

        raise ValueError(f"Expected NETCONF <data> root, got <{source_local_name}>")

    @staticmethod
    def _extract_xml_namespace(tag):
        """Extract namespace URI from ElementTree tag."""
        if isinstance(tag, str) and tag.startswith("{") and "}" in tag:
            return tag[1:].split("}", maxsplit=1)[0]
        return ""

    @staticmethod
    def _apply_prefixed_namespaces(root, prefixed_ns):
        """Attach prefixed namespace declarations to XML root element."""
        for prefix, uri in prefixed_ns.items():
            root.set(f"xmlns:{prefix}", uri)

    def _build_edit_config_xml(self, children, prefixed_ns):
        """Build NETCONF <config> payload from top-level child elements."""
        netconf_ns = "urn:ietf:params:xml:ns:netconf:base:1.0"
        config_root = ET.Element(f"{{{netconf_ns}}}config")
        self._apply_prefixed_namespaces(config_root, prefixed_ns)
        for child in children:
            config_root.append(copy.deepcopy(child))
        return ET.tostring(config_root, encoding="unicode")

    async def sync_source_netconf_from_ru(self, source_session):
        """
        One-time bootstrap: apply RU NETCONF running config to source NETCONF server.
        """
        logging.info("Starting one-time RU -> source NETCONF startup sync")
        ru_session = await self.try_connect()
        if ru_session is None:
            logging.warning("Startup sync skipped: unable to connect RU NETCONF server")
            return False

        try:
            ru_data_xml = await asyncio.to_thread(lambda: ru_session.get_config(source=self.args.ru_datastore).data_xml)
            ru_data_root, prefixed_ns = self._parse_data_xml(ru_data_xml)
            attempted = 0
            applied = 0
            skipped = 0
            for child in list(ru_data_root):
                ns = self._extract_xml_namespace(child.tag)
                local = child.tag.split("}", maxsplit=1)[-1]
                if ns not in SYNC_ALLOWED_NAMESPACES:
                    skipped += 1
                    logging.debug(f"Startup selective sync skipped '{local}' due to namespace filter '{ns}'")
                    continue

                attempted += 1
                try:
                    await asyncio.to_thread(
                        source_session.edit_config,
                        config=self._build_edit_config_xml([child], prefixed_ns),
                        format="xml",
                        target=self.args.datastore,
                        default_operation="merge",
                    )
                    applied += 1
                except Exception as child_err:  # pylint: disable=broad-exception-caught
                    logging.warning(f"Startup selective sync failed for '{local}': {child_err}")

            if applied:
                logging.info(
                    f"Startup selective sync completed: applied {applied}/{attempted} top-level config element(s), "
                    f"skipped {skipped} filtered namespace element(s)"
                )
                return True
            logging.warning(
                f"Startup selective sync did not apply any config (attempted={attempted}, skipped={skipped})"
            )
            return False
        except (ValueError, ET.ParseError) as e:
            logging.warning(f"Startup sync failed while converting RU NETCONF payload: {e}")
            return False
        except Exception as e:  # pylint: disable=broad-exception-caught
            logging.warning(f"Startup sync failed while applying RU NETCONF payload: {e}")
            return False
        finally:
            with suppress(Exception):
                ru_session.close_session()

    async def try_connect(self):
        """
        Try to connect to the RU NETCONF server once.
        Returns manager instance if successful, None otherwise
        """
        try:
            # run blocking ncclient call in a background thread
            def connect():
                # pylint: disable=duplicate-code
                netconf_manager = manager.connect(
                    host=self.args.ru_netconf_host,
                    port=self.args.ru_netconf_port,
                    username=self.args.ru_netconf_username,
                    password=self.args.ru_netconf_password,
                    hostkey_verify=False,
                    allow_agent=False,
                    look_for_keys=False,
                    # timeout=self.retry_interval,
                )
                # pylint: enable=duplicate-code
                logging.info("Connected to RU NETCONF server")
                self.alarm_mgr.clear_alarm(
                    1003,
                    message="RU NETCONF connection restored",
                )
                return netconf_manager

            return await asyncio.to_thread(connect)

        except (
            SSHError,
            AuthenticationError,
            SessionCloseError,
        ) as e:
            logging.warning(f"RU NETCONF connection failed: {e}")
            self.alarm_mgr.set_alarm(
                1003,
                message="RU NETCONF connection lost",
            )
            return None

    async def run(self):
        """Main loop for forwarding source NETCONF updates to the RU NETCONF server."""
        while True:
            ru_session = await self.try_connect()
            if ru_session:
                self.state.session_state["ru_nc_connected"] = True
                ru_config = RuConfig(ru_session, self.args.ru_datastore)

                while ru_session.connected:
                    try:
                        data_xml = await asyncio.wait_for(self.state.ru_update_queue.get(), timeout=1)
                    except asyncio.TimeoutError:
                        continue

                    try:
                        edit_payload = self._data_xml_to_edit_config_xml(
                            data_xml,
                            include_namespaces=SYNC_ALLOWED_NAMESPACES,
                        )
                        await asyncio.to_thread(ru_config.edit_config, edit_payload, "Forwarded NETCONF config")
                        logging.info("Forwarded NETCONF update to RU")
                    except (ValueError, ET.ParseError) as e:
                        logging.warning(f"Skipping RU forward update due to payload conversion error: {e}")
                    except Exception as e:  # pylint: disable=broad-exception-caught
                        logging.warning(f"Failed forwarding NETCONF update to RU: {e}")
                        if not ru_session.connected:
                            break
                    finally:
                        self.state.ru_update_queue.task_done()

                with suppress(Exception):
                    ru_session.close_session()

            if self.state.session_state.get("ru_nc_connected"):
                self.state.session_state["ru_nc_connected"] = False

            self.alarm_mgr.set_alarm(
                1003,
                message="RU NETCONF connection lost",
            )
            logging.debug(f"Retrying RU NETCONF forwarder in {self.retry_interval} seconds...")
            await asyncio.sleep(self.retry_interval)
