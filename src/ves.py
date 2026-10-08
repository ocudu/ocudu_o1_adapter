# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
This module provides the VesMessages class, which is responsible for sending various types of
messages to the VES (Virtual Event Streaming) collector.
"""

import datetime
import json
import socket

import requests
from jinja2 import Environment, exceptions, FileSystemLoader


class VesMessages:
    """
    VesMessages class is responsible for sending various types of messages
    to the VES (Virtual Event Streaming) collector.

    Attributes:
        host (str): The hostname of the VES collector.
        port (str): The port number of the VES collector.
        username (str): The username for authentication with the VES collector.
        password (str): The password for authentication with the VES collector.
        oam_ipv4_address (str): The OAM IPv4 address.
    """

    _HEADERS = {"Content-Type": "application/json"}
    _VERIFY = False
    _NF_VENDOR = "Software Radio Systems"
    _NF_VERSION = "25.04"
    _REPORTING_ENTITY = "ocududu"
    _NF_NAMING_CODE = "123"
    _SOURCE_NAME = "ocududu"
    _POST_TIMEOUT = 10
    # alarm_manager.AlarmType names -> 3GPP TS 28.532 eventType literals
    _ALARM_TYPES = {
        "COMMUNICATIONS": "COMMUNICATIONS_ALARM",
        "PROCESSING": "PROCESSING_ERROR_ALARM",
        "ENVIRONMENTAL": "ENVIRONMENTAL_ALARM",
        "QUALITY_OF_SERVICE": "QUALITY_OF_SERVICE_ALARM",
        "EQUIPMENT": "EQUIPMENT_ALARM",
        "SECURITY": "SECURITY_SERVICE_OR_MECHANISM_VIOLATION",
        "INTEGRITY_VIOLATION": "INTEGRITY_VIOLATION",
        "OPERATIONAL_VIOLATION": "OPERATIONAL_VIOLATION",
        "PHYSICAL_VIOLATION": "PHYSICAL_VIOLATION",
        "TIME_DOMAIN_VIOLATION": "TIME_DOMAIN_VIOLATION",
    }

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def __init__(
        self,
        scheme,
        host="localhost",
        port="8443",
        username="sample1",
        password="sample1",
        oam_ipv4_address="11.22.33.44",
        logging=None,
    ):
        self.url_ves = f"{scheme}://{host}:{port}/eventListener/v7"
        self.username = username
        self.password = password

        self.oam_ipv4_address = oam_ipv4_address

        self.sequence = 0
        self.logging = logging

    def send_pnf_registration(self):
        """
        Sends a PNF (Physical Network Function) registration message to the VES (Virtual Event Streaming) collector.

        References:
        - https://docs.onap.org/projects/onap-dcaegen2/en/latest/sections/apis/ves.html#sample-request-and-response
        - https://docs.onap.org/projects/onap-integration/en/latest/docs_5g_pnf_pnp.html
        """
        current_time = datetime.datetime.now(tz=datetime.timezone.utc)
        environment = Environment(loader=FileSystemLoader("templates/ves"))
        template = environment.get_template("pnf_registration.json")
        msg = template.render(
            nfVendorName=self._NF_VENDOR,
            reportingEntityName=self._REPORTING_ENTITY,
            softwareVersion=self._NF_VERSION,
            oamV4IpAddress=self.oam_ipv4_address,
            timeStamp=int(current_time.timestamp() * 1000000),
            eventTime=current_time.isoformat() + "Z",
            eventId=socket.getfqdn() + "_" + current_time.isoformat() + "Z",
        )
        self._send_ves_message(msg)

    def send_alarm(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        alarm_id=1001,
        alarm="internalConnectionLoss",
        alarm_type="EQUIPMENT_ALARM",
        severity="CRITICAL",
        trend="NO_CHANGE",
        *,
        message=None,
        details=None,
        event_time=None,
        cleared=False,
        proposed_repair_actions=None,
    ):
        """
        Sends an alarm (3GPP TS 28.532 notifyNewAlarm, or notifyClearedAlarm when
        cleared, as a VES stndDefined event) to the VES collector.

        alarm_type is an alarm_manager.AlarmType or a TS 28.532 eventType literal.
        message becomes additionalText; details — key/value context such as the
        o-ran-fm leaves of an O-RU fault — becomes additionalInformation, and what
        the device itself reported wins where it did: its alarm-type (hyphenated
        o-ran-fm literal) over alarm_type, probable-cause over the alarm name,
        specific-problem over fault-text over the alarm name, and
        proposed-repair-actions over proposed_repair_actions (omitted when neither
        is given). event_time (ISO 8601) is the notification's eventTime, else the
        send time; notificationId is this sender's per-event sequence.

        References:
        - https://forge.3gpp.org/rep/sa5/MnS/-/blob/Tag_Rel17_SA106/OpenAPI/TS28532_FaultMnS.yaml
        """
        current_time = datetime.datetime.now(tz=datetime.timezone.utc)
        type_name = getattr(alarm_type, "name", str(alarm_type))
        details = dict(details or {})
        reported_type = (details.get("alarm-type") or "").replace("-", "_")
        environment = Environment(loader=FileSystemLoader("templates/ves"))
        try:
            template = environment.get_template("alarm.json")
        except exceptions.TemplateNotFound as e:
            self.logging.error(f"Template not found: {e}")
            return

        msg = template.render(
            domain="stndDefined",
            eventId="ManagedElement=ran1,GNBDUFunction=du1,NRCellDU=nrcelldu1",
            nodeId="ran1",
            eventType="ocudu_Alarm",
            priority="High",
            nfNamingCode=self._NF_NAMING_CODE,
            nfVendorName=self._NF_VENDOR,
            reportingEntityName=self._REPORTING_ENTITY,
            softwareVersion=self._NF_VERSION,
            sourceName="ocududu",
            sourceId="noIdea",
            sequence=self.sequence,
            oamV4IpAddress=self.oam_ipv4_address,
            timeStamp=int(current_time.timestamp() * 1000000),
            eventTime=event_time or current_time.isoformat().replace("+00:00", "Z"),
            stateInterface="urn:ietf:params:xml:ns:yang:ietf-interfaces:interfaces/interface/name='O-RAN-SC-OAM'",
            alarmId=alarm_id,
            alarm=alarm,
            alarmType=reported_type or self._ALARM_TYPES.get(type_name, type_name),
            severity=severity,  # CRITICAL, MAJOR, MINOR, WARNING, INDETERMINATE, CLEARED
            notificationId=self.sequence,
            notificationType="notifyClearedAlarm" if cleared else "notifyNewAlarm",
            trendIndication=trend,  # NO_CHANGE, MORE_SEVERE, LESS_SEVERE
            probableCause=details.get("probable-cause") or alarm,
            specificProblem=details.get("specific-problem") or details.get("fault-text") or alarm,
            additionalText=message or "ocudu",
            additionalInformation=details,
            proposedRepairActions=details.get("proposed-repair-actions") or proposed_repair_actions,
        )

        self._send_ves_message(msg)

    def send_alarm_event(self, event):
        """
        Sends an alarm_manager.AlarmEvent: the alarm's registry identity plus what
        its raiser attached (message, details); no canned repair action is invented.
        """
        details = dict(event.details or {})
        self.send_alarm(
            alarm_id=event.alarm_id,
            alarm=event.name,
            alarm_type=event.alarm_type,
            severity=event.new_severity.name,
            trend=event.trend.name,
            message=event.message,
            details=details,
            event_time=details.get("event-time"),
            cleared=not event.became_active,
        )

    def send_state_change(self, old_state="maintenance", new_state="inService"):
        """
        Sends a state change message to the VES (Virtual Event Streaming) collector.

        Args:
            old_state (str): The previous state of the component. Defaults to "maintenance".
            new_state (str): The new state of the component. Defaults to "inService".
        """
        current_time = datetime.datetime.now(tz=datetime.timezone.utc)
        environment = Environment(loader=FileSystemLoader("templates/ves"))
        template = environment.get_template("state_change.json")
        msg = template.render(
            domain="stateChange",
            newState=new_state,
            oldState=old_state,
            eventType="O_RAN_COMPONENT",
            nfNamingCode=self._NF_NAMING_CODE,
            nfVendorName=self._NF_VENDOR,
            reportingEntityName=self._REPORTING_ENTITY,
            softwareVersion=self._NF_VERSION,
            sourceName=self._SOURCE_NAME,
            sequence=self.sequence,
            oamV4IpAddress=self.oam_ipv4_address,
            timeStamp=int(current_time.timestamp() * 1000000),
            eventTime=current_time.isoformat() + "Z",
            eventId=socket.getfqdn() + "_" + current_time.isoformat() + "Z",
            stateInterface="urn:ietf:params:xml:ns:yang:ietf-interfaces:interfaces/interface/name='O-RAN-SC-OAM'",
        )
        self._send_ves_message(msg)

    def send_file_ready(self, me_id, file_location, file_size, ready_time, expiration_time):
        """Sends a stndDefined notifyFileReady (TS 28.532 FileDataReportingMnS) for one PM file."""
        current_time = datetime.datetime.now(tz=datetime.timezone.utc)
        environment = Environment(loader=FileSystemLoader("templates/ves"))
        template = environment.get_template("file_ready.json")
        msg = template.render(
            eventId=f"{me_id}_notifyFileReady_{self.sequence}",
            sequence=self.sequence,
            reportingEntityName=f"ManagedElement={me_id}",
            sourceName=me_id,
            timeStamp=int(current_time.timestamp() * 1000000),
            nfNamingCode=self._NF_NAMING_CODE,
            nfVendorName=self._NF_VENDOR,
            href=f"http://{self.oam_ipv4_address}/ManagedElement={me_id}",
            notificationId=self.sequence,
            eventTime=current_time.isoformat(timespec="seconds"),
            systemDN=f"ManagedElement={me_id}",
            fileLocation=file_location,
            fileSize=file_size,
            fileReadyTime=ready_time,
            fileExpirationTime=expiration_time,
        )
        self._send_ves_message(msg)

    def _send_ves_message(self, msg):
        # one sequence number per event, delivered or not: the collector must
        # be able to tell events apart even across an outage on its side
        self.sequence = self.sequence + 1
        # Format and send request
        self.logging.debug(f"Request: {msg}")
        formatted = json.loads(msg)

        try:
            response = requests.post(
                self.url_ves,
                json=formatted,
                headers=self._HEADERS,
                auth=(self.username, self.password),
                verify=self._VERIFY,
                timeout=self._POST_TIMEOUT,
            )
        except (
            requests.exceptions.Timeout,
            requests.exceptions.TooManyRedirects,
            requests.exceptions.ConnectionError,
            requests.exceptions.RequestException,
        ) as e:
            self.logging.error(f"VES HTTP request failed: {e}")
            return None

        if 200 <= response.status_code < 300:
            self.logging.debug("Alarm delivered successfully")
        else:
            self.logging.warning(f"Alarm delivery failed (status code: {response.status_code})")

        return response
