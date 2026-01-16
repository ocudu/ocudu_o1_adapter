#
# Copyright 2021-2026 Software Radio Systems Limited
#
# By using this file, you agree to the terms and conditions set
# forth in the LICENSE file which can be found at the top level of
# the distribution.
#

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
    _REPORTING_ENTITY = "srsdu"
    _NF_NAMING_CODE = "123"
    _SOURCE_NAME = "srsdu"
    _POST_TIMEOUT = 10

    # pylint: disable=too-many-arguments,too-many-positional-arguments
    def __init__(
        self,
        host="localhost",
        port="8443",
        username="sample1",
        password="sample1",
        oam_ipv4_address="11.22.33.44",
        logging=None,
    ):
        self.url_ves = f"https://{host}:{port}/eventListener/v7"
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

    def send_alarm(
        self,
        alarm_id=1001,
        alarm="internalConnectionLoss",
        alarm_type="EQUIPMENT_ALARM",
        severity="CRITICAL",
        trend="NO_CHANGE",
    ):
        """
        Sends an alarm message to the VES (Virtual Event Streaming) collector.

        References:
        - https://forge.3gpp.org/rep/sa5/MnS/-/blob/Tag_Rel17_SA106/OpenAPI/TS28532_FaultMnS.yaml

        Args:
            alarmId (str): The identifier of the alarm to be sent.
        """
        current_time = datetime.datetime.now(tz=datetime.timezone.utc)
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
            eventType="srsRAN_Alarm",
            priority="High",
            nfNamingCode=self._NF_NAMING_CODE,
            nfVendorName=self._NF_VENDOR,
            reportingEntityName=self._REPORTING_ENTITY,
            softwareVersion=self._NF_VERSION,
            sourceName="srsdu",
            sourceId="noIdea",
            sequence=self.sequence,
            oamV4IpAddress=self.oam_ipv4_address,
            timeStamp=int(current_time.timestamp() * 1000000),
            eventTime=current_time.isoformat() + "Z",
            stateInterface="urn:ietf:params:xml:ns:yang:ietf-interfaces:interfaces/interface/name='O-RAN-SC-OAM'",
            alarmId=alarm_id,
            alarm=alarm,
            alarmType=alarm_type,
            severity=severity,  # WARNING, MAJOR, MINOR
            notificationId=1,
            trendIndication=trend,  # NO_CHANGE, MORE_SEVERE
        )

        self._send_ves_message(msg)

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

    def _send_ves_message(self, msg):
        # Format and send request
        self.logging.debug(f"Request: {msg}")
        formatted = str(json.loads(msg))

        try:
            response = requests.post(
                self.url_ves,
                data=formatted,
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

        if response.status_code >= 200 and response.status_code < 300:
            self.logging.debug("Alarm delivered successfully")
        else:
            self.logging.warning(f"Alarm delivery failed (status code: {response.status_code})")

        # increase sequence number
        self.sequence = self.sequence + 1
        return response
