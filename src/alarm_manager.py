#
# Copyright 2021-2026 Software Radio Systems Limited
#
# By using this file, you agree to the terms and conditions set
# forth in the LICENSE file which can be found at the top level of
# the distribution.
#

"""Manages ORAN/3GPP-style alarms for a DU."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import auto, Enum
from threading import Lock
from typing import Callable, Dict, Iterable, List, Optional


# --- ORAN/3GPP-style enumerations (minimal, extend as needed) ---
class AlarmType(Enum):
    """Alarm type/category as per ORAN/3GPP specs."""

    COMMUNICATIONS = auto()
    PROCESSING = auto()
    ENVIRONMENTAL = auto()
    QUALITY_OF_SERVICE = auto()
    EQUIPMENT = auto()
    SECURITY = auto()
    # Add more mapping to spec categories as needed


class AlarmSeverity(Enum):
    """Alarm severity levels as per ORAN/3GPP specs."""

    CRITICAL = auto()
    MAJOR = auto()
    MINOR = auto()
    WARNING = auto()
    INDETERMINATE = auto()
    CLEARED = auto()  # convenience for normalization


class AlarmTrend(Enum):
    """Alarm trend as per ORAN/3GPP specs."""

    MORE_SEVERE = auto()
    NO_CHANGE = auto()
    LESS_SEVERE = auto()


@dataclass(frozen=True)
class AlarmDefinition:
    """Static definition of an alarm kind/type (extensible registry item)."""

    alarm_id: int
    name: str
    type: AlarmType
    default_severity: AlarmSeverity
    default_trend: AlarmTrend = AlarmTrend.NO_CHANGE


@dataclass
class AlarmState:
    """Runtime state for a specific alarm instance."""

    definition: AlarmDefinition
    active: bool = False
    severity: AlarmSeverity = field(default=AlarmSeverity.CLEARED)
    trend: AlarmTrend = field(default=AlarmTrend.NO_CHANGE)
    last_change_ts: Optional[datetime] = None
    last_message: Optional[str] = None  # free-form cause/extra info


# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True)
class AlarmEvent:
    """Emitted to external notifier on every state change."""

    alarm_id: int
    name: str
    alarm_type: AlarmType
    became_active: bool
    old_severity: AlarmSeverity
    new_severity: AlarmSeverity
    trend: AlarmTrend
    timestamp: datetime
    message: Optional[str] = None


Notifier = Callable[[AlarmEvent], None]
"""
External notifier signature. Example: def notify(event: AlarmEvent) -> None: ...
"""


class AlarmManager:
    """
    Manages ORAN/3GPP-style alarms for a DU.

    - Register alarms once (or pass a list at construction).
    - Use set_alarm(...) to raise/update an alarm.
    - Use clear_alarm(...) to clear it.
    - A notifier callback is invoked on every state transition.
    - Thread-safe.
    """

    def __init__(
        self, alarm_definitions: Iterable[AlarmDefinition] | None = None, notifier: Optional[Notifier] = None
    ) -> None:
        self._defs: Dict[int, AlarmDefinition] = {}
        self._states: Dict[int, AlarmState] = {}
        self._lock = Lock()
        self._notifier = notifier

        if alarm_definitions:
            self.register_many(alarm_definitions)

    # --- Extensibility: register new alarms at runtime ---

    def register(self, definition: AlarmDefinition) -> None:
        """Register a new alarm definition."""
        with self._lock:
            if definition.alarm_id in self._defs:
                raise ValueError(f"Alarm ID {definition.alarm_id} already registered")
            self._defs[definition.alarm_id] = definition
            self._states[definition.alarm_id] = AlarmState(
                definition=definition,
                active=False,
                severity=AlarmSeverity.CLEARED,
                trend=AlarmTrend.NO_CHANGE,
                last_change_ts=None,
                last_message=None,
            )

    def register_many(self, definitions: Iterable[AlarmDefinition]) -> None:
        """Register multiple alarm definitions at once."""
        for d in definitions:
            self.register(d)

    # --- Core operations ---

    def set_alarm(
        self,
        alarm_id: int,
        severity: Optional[AlarmSeverity] = None,
        trend: Optional[AlarmTrend] = None,
        message: Optional[str] = None,
    ) -> None:
        """
        Raise or update an alarm. If severity/trend are omitted, the defaults from
        the alarm definition are used.
        """
        now = datetime.utcnow()
        with self._lock:
            state = self._get_state_or_raise(alarm_id)
            old_sev = state.severity
            new_sev = severity or state.definition.default_severity
            new_trend = trend or state.definition.default_trend

            changed = (
                (not state.active)
                or (old_sev != new_sev)
                or (state.trend != new_trend)
                or (state.last_message != message)
            )
            if not changed:
                return  # no-op

            state.active = True
            state.severity = new_sev
            state.trend = new_trend
            state.last_change_ts = now
            state.last_message = message

        self._emit_event(
            AlarmEvent(
                alarm_id=state.definition.alarm_id,
                name=state.definition.name,
                alarm_type=state.definition.type,
                became_active=True,
                old_severity=old_sev,
                new_severity=new_sev,
                trend=new_trend,
                timestamp=now,
                message=message,
            )
        )

    def clear_alarm(self, alarm_id: int, message: Optional[str] = None) -> None:
        """Clear an alarm. Emits an event only if the alarm was active or changed."""
        now = datetime.utcnow()
        with self._lock:
            state = self._get_state_or_raise(alarm_id)
            if not state.active and state.severity == AlarmSeverity.CLEARED and state.last_message == message:
                return  # no-op

            old_sev = state.severity
            state.active = False
            state.severity = AlarmSeverity.CLEARED
            state.trend = AlarmTrend.LESS_SEVERE if old_sev != AlarmSeverity.CLEARED else AlarmTrend.NO_CHANGE
            state.last_change_ts = now
            state.last_message = message

        self._emit_event(
            AlarmEvent(
                alarm_id=state.definition.alarm_id,
                name=state.definition.name,
                alarm_type=state.definition.type,
                became_active=False,
                old_severity=old_sev,
                new_severity=AlarmSeverity.CLEARED,
                trend=state.trend,
                timestamp=now,
                message=message,
            )
        )

    # --- Queries / utilities ---

    def is_active(self, alarm_id: int) -> bool:
        """Check if an alarm is currently active."""
        with self._lock:
            return self._get_state_or_raise(alarm_id).active

    def get_state(self, alarm_id: int) -> AlarmState:
        """Get a snapshot of the current state of an alarm."""
        with self._lock:
            # Return a shallow copy to avoid external mutation
            s = self._get_state_or_raise(alarm_id)
            return AlarmState(**{**s.__dict__})

    def active_alarms(self) -> List[AlarmState]:
        """Get a list of all currently active alarms."""
        with self._lock:
            return [AlarmState(**{**s.__dict__}) for s in self._states.values() if s.active]

    def set_notifier(self, notifier: Optional[Notifier]) -> None:
        """Set or replace the external notifier callback."""
        with self._lock:
            self._notifier = notifier

    # --- Internal helpers ---

    def _get_state_or_raise(self, alarm_id: int) -> AlarmState:
        if alarm_id not in self._states:
            raise KeyError(f"Alarm ID {alarm_id} not registered")
        return self._states[alarm_id]

    def _emit_event(self, event: AlarmEvent) -> None:
        n = None
        with self._lock:
            n = self._notifier
        if n:
            try:
                n(event)
            except Exception as e:
                # In production you might log this rather than raising.
                raise RuntimeError(f"Notifier failed: {e}") from e
