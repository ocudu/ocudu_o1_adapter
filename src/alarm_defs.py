#
# Copyright 2021-2026 Software Radio Systems Limited
#
# By using this file, you agree to the terms and conditions set
# forth in the LICENSE file which can be found at the top level of
# the distribution.
#

"""Self-defined alarm definitions for a gNB."""

from alarm_manager import AlarmDefinition, AlarmSeverity, AlarmType


# pylint: disable=too-few-public-methods
class AlarmDefinitions:
    """Container for predefined alarm definitions."""

    defs = [
        # Alarm ID range 1000-1999 reserved for life-cycle management alarms
        AlarmDefinition(
            alarm_id=1001,
            name="NETCONF_CONNECTION_LOSS",
            type=AlarmType.COMMUNICATIONS,
            default_severity=AlarmSeverity.CRITICAL,
        ),
        AlarmDefinition(
            alarm_id=1002,
            name="REMOTE_CONTROL_CONNECTION_LOSS",
            type=AlarmType.COMMUNICATIONS,
            default_severity=AlarmSeverity.CRITICAL,
        ),
        # Alarm ID range 2000-2999 reserved for 3GPP alarms
        AlarmDefinition(
            alarm_id=2001,
            name="AMF_CONNECTION_LOSS",
            type=AlarmType.COMMUNICATIONS,
            default_severity=AlarmSeverity.MAJOR,
        ),
        # Alarm ID range 3000-3999 reserved for platform-specific alarms
        AlarmDefinition(
            alarm_id=3001,
            name="PTP_GM_LOSS",
            type=AlarmType.EQUIPMENT,
            default_severity=AlarmSeverity.WARNING,
        ),
        AlarmDefinition(
            alarm_id=3002,
            name="PTP_LATENCY_TOO_HIGH",
            type=AlarmType.EQUIPMENT,
            default_severity=AlarmSeverity.WARNING,
        ),
    ]
