# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Self-defined alarm definitions for a gNB."""

from enum import IntEnum
from typing import List

from alarm_manager import AlarmDefinition, AlarmSeverity, AlarmType


class AlarmId(IntEnum):
    """
    The self-defined alarms as (ID, type, default severity, X.733 / TS 28.532 probable cause);
    the member name is the alarm's name.
    """

    alarm_type: AlarmType
    default_severity: AlarmSeverity
    probable_cause: str

    def __new__(cls, alarm_id: int, alarm_type: AlarmType, default_severity: AlarmSeverity, probable_cause: str):
        member = int.__new__(cls, alarm_id)
        member._value_ = alarm_id
        member.alarm_type = alarm_type
        member.default_severity = default_severity
        member.probable_cause = probable_cause
        return member

    # Alarm ID range 1000-1999 reserved for life-cycle management alarms
    NETCONF_CONNECTION_LOSS = (
        1001,
        AlarmType.COMMUNICATIONS,
        AlarmSeverity.CRITICAL,
        "connectionEstablishmentError",
    )

    REMOTE_CONTROL_CONNECTION_LOSS = (
        1002,
        AlarmType.COMMUNICATIONS,
        AlarmSeverity.CRITICAL,
        "connectionEstablishmentError",
    )

    RU_NETCONF_CONNECTION_LOSS = (
        1003,
        AlarmType.COMMUNICATIONS,
        AlarmSeverity.CRITICAL,
        "connectionEstablishmentError",
    )

    RU_SUPERVISION_FAILURE = (
        1004,
        AlarmType.COMMUNICATIONS,
        AlarmSeverity.CRITICAL,
        "communicationsSubsystemFailure",
    )

    # Alarm ID range 2000-2999 reserved for 3GPP alarms
    AMF_CONNECTION_LOSS = (
        2001,
        AlarmType.COMMUNICATIONS,
        AlarmSeverity.MAJOR,
        "connectionEstablishmentError",
    )

    UE_CAPACITY_NEAR_LIMIT = (
        2005,
        AlarmType.QUALITY_OF_SERVICE,
        AlarmSeverity.MAJOR,
        "resourceAtOrNearingCapacity",
    )

    RRC_ESTABLISHMENT_FAILURES = (
        2006,
        AlarmType.COMMUNICATIONS,
        AlarmSeverity.MAJOR,
        "callEstablishmentError",
    )

    PDU_SESSION_SETUP_FAILURES = (
        2008,
        AlarmType.COMMUNICATIONS,
        AlarmSeverity.MAJOR,
        "callEstablishmentError",
    )

    # Alarm ID range 3000-3999 reserved for platform-specific alarms
    PTP_GM_LOSS = (
        3001,
        AlarmType.EQUIPMENT,
        AlarmSeverity.WARNING,
        "timingProblem",
    )

    PTP_LATENCY_TOO_HIGH = (
        3002,
        AlarmType.EQUIPMENT,
        AlarmSeverity.WARNING,
        "timingProblem",
    )

    CPU_OVERLOAD = (
        3003,
        AlarmType.PROCESSING,
        AlarmSeverity.MAJOR,
        "cpuCyclesLimitExceeded",
    )

    MEMORY_OVERLOAD = (
        3004,
        AlarmType.PROCESSING,
        AlarmSeverity.MAJOR,
        "outOfMemory",
    )

    L2_REALTIME_DEADLINE_MISS = (
        3006,
        AlarmType.PROCESSING,
        AlarmSeverity.MAJOR,
        "performanceDegraded",
    )

    FRONTHAUL_TIMING_VIOLATION = (
        3008,
        AlarmType.TIME_DOMAIN_VIOLATION,
        AlarmSeverity.MAJOR,
        "timingProblem",
    )

    PRACH_RECEPTION_FAILURE = (
        3009,
        AlarmType.COMMUNICATIONS,
        AlarmSeverity.MAJOR,
        "receiveFailure",
    )

    UL_RECEPTION_FAILURE = (
        3010,
        AlarmType.COMMUNICATIONS,
        AlarmSeverity.MAJOR,
        "receiveFailure",
    )

    # Alarm IDs 4000-69535 reserved for O-RU reported faults, registered
    # dynamically by ru_fm_bridge (4000 + the o-ran-fm fault-id, a uint16)


ALARM_DEFINITIONS: List[AlarmDefinition] = [
    AlarmDefinition(
        alarm_id=int(a),
        name=a.name,
        type=a.alarm_type,
        default_severity=a.default_severity,
        probable_cause=a.probable_cause,
    )
    for a in AlarmId
]
