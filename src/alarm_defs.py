# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

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
        AlarmDefinition(
            alarm_id=1003,
            name="RU_NETCONF_CONNECTION_LOSS",
            type=AlarmType.COMMUNICATIONS,
            default_severity=AlarmSeverity.CRITICAL,
        ),
        AlarmDefinition(
            alarm_id=1004,
            name="RU_SUPERVISION_FAILURE",
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
        AlarmDefinition(
            alarm_id=2005,
            name="UE_CAPACITY_NEAR_LIMIT",
            type=AlarmType.QUALITY_OF_SERVICE,
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
        AlarmDefinition(
            alarm_id=3003,
            name="CPU_OVERLOAD",
            type=AlarmType.PROCESSING,
            default_severity=AlarmSeverity.MAJOR,
        ),
        AlarmDefinition(
            alarm_id=3004,
            name="MEMORY_OVERLOAD",
            type=AlarmType.PROCESSING,
            default_severity=AlarmSeverity.MAJOR,
        ),
        AlarmDefinition(
            alarm_id=3006,
            name="L2_REALTIME_DEADLINE_MISS",
            type=AlarmType.PROCESSING,
            default_severity=AlarmSeverity.MAJOR,
        ),
        AlarmDefinition(
            alarm_id=3008,
            name="FRONTHAUL_TIMING_VIOLATION",
            type=AlarmType.TIME_DOMAIN_VIOLATION,
            default_severity=AlarmSeverity.MAJOR,
        ),
        AlarmDefinition(
            alarm_id=3009,
            name="PRACH_RECEPTION_FAILURE",
            type=AlarmType.COMMUNICATIONS,
            default_severity=AlarmSeverity.MAJOR,
        ),
        AlarmDefinition(
            alarm_id=3010,
            name="UL_RECEPTION_FAILURE",
            type=AlarmType.COMMUNICATIONS,
            default_severity=AlarmSeverity.MAJOR,
        ),
        # Alarm IDs 4000-69535 reserved for O-RU reported faults, registered
        # dynamically by ru_fm_bridge (4000 + the o-ran-fm fault-id, a uint16)
    ]
