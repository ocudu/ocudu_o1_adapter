# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Fault-management bridge: O-RU o-ran-fm faults into the adapter alarm manager.

Each O-RU fault becomes an adapter alarm (id 4000 + fault-id, registered on
first sight, severity and type from the O-RU's leaves) that the existing
notifier sends northbound with the o-ran-fm leaves attached. Live alarm-notifs
drive raises and clears; every connect cycle reconciles against the O-RU's
active-alarm-list. Identity, aggregation over fault sources, the strict-read
rules and the WARNING retirement are described in docs/mplane-service.md,
section "Fault bridge".
"""

import logging
from typing import Dict, NamedTuple, Optional, Set, Tuple

from ncclient.operations import rpc as rpc_ops

from alarm_manager import AlarmDefinition, AlarmSeverity, AlarmType
from fm_parsers import parse_alarm_notification

# Adapter alarm id = RU_FAULT_ALARM_BASE + fault-id; o-ran-fm's fault-id is a
# uint16, so 4000-69535 is reserved for O-RU reported faults
RU_FAULT_ALARM_BASE = 4000
RU_FAULT_ID_LIMIT = 65536
RU_FAULT_ALARM_END = RU_FAULT_ALARM_BASE + RU_FAULT_ID_LIMIT

# o-ran-fm fault-severity literals, most severe first; anything else is INDETERMINATE
_SEVERITIES = {
    "CRITICAL": AlarmSeverity.CRITICAL,
    "MAJOR": AlarmSeverity.MAJOR,
    "MINOR": AlarmSeverity.MINOR,
    "WARNING": AlarmSeverity.WARNING,
}
_RANK = {severity: rank for rank, severity in enumerate((*_SEVERITIES.values(), AlarmSeverity.INDETERMINATE))}

_STALE_MESSAGE = "not in the O-RU active-alarm-list at reconcile"


class _Report(NamedTuple):
    """What one fault-source reported for a fault-id; event_time last (see _merge)."""

    severity: AlarmSeverity
    text: Optional[str]
    affected: Tuple[str, ...]
    alarm_type: Optional[str]
    probable_cause: Optional[str]
    specific_problem: Optional[str]
    repair: Optional[str]
    extra: Tuple[Tuple[str, str], ...]
    event_time: Optional[str]


# o-ran-fm alarm-type (2024-08-12) -> the registry's type; EQUIPMENT when an O-RU reports none
_ALARM_TYPES = {
    "COMMUNICATIONS-ALARM": AlarmType.COMMUNICATIONS,
    "PROCESSING-ERROR-ALARM": AlarmType.PROCESSING,
    "ENVIRONMENTAL-ALARM": AlarmType.ENVIRONMENTAL,
    "QUALITY-OF-SERVICE-ALARM": AlarmType.QUALITY_OF_SERVICE,
    "EQUIPMENT-ALARM": AlarmType.EQUIPMENT,
    "INTEGRITY-VIOLATION": AlarmType.INTEGRITY_VIOLATION,
    "OPERATIONAL-VIOLATION": AlarmType.OPERATIONAL_VIOLATION,
    "PHYSICAL-VIOLATION": AlarmType.PHYSICAL_VIOLATION,
    "SECURITY-SERVICE-OR-MECHANISM-VIOLATION": AlarmType.SECURITY,
    "TIME-DOMAIN-VIOLATION": AlarmType.TIME_DOMAIN_VIOLATION,
}


# fault-source -> its report, for every source reporting one fault-id
_Sources = Dict[Optional[str], _Report]


class RuFaultBridge:
    """Mirror O-RU o-ran-fm faults into an AlarmManager.

    Not internally locked: the M-plane session awaits each handler call —
    cycle handlers before the listen loop, notification handlers one at a
    time — and the AlarmManager serializes its own state. A bridge shared by
    more than one session would need a lock here.
    """

    def __init__(self, alarm_mgr):
        self.alarm_mgr = alarm_mgr
        self._known_faults: Set[int] = set()  # fault ids with a registered definition
        self._reports: Dict[int, _Sources] = {}  # fault id -> sources reporting it

    def handle_notification(self, notification_xml):
        """Apply an alarm-notif from the notification stream; ignore other events.

        Suitable for MplaneSession.register_notification_handler.
        """
        alarm = parse_alarm_notification(notification_xml)
        if alarm is not None:
            self._apply(alarm)

    def reconcile(self, ru_config):
        """Synchronise the mirror with the O-RU's active-alarm-list.

        Suitable for MplaneSession.register_cycle_handler. A transport failure
        or reply timeout on the strict read propagates (the session is
        recycled); an rpc-error is logged and the mirror kept until the next
        cycle. Each listed fault-id is applied once, aggregated over its
        sources, so an unchanged list emits nothing northbound; mirrored
        faults the O-RU no longer lists are retired, judged against the
        AlarmManager's active state. A notifier failure on one update does
        not stop the rest; the session keeps the watchdog fed meanwhile.
        """
        try:
            listed = ru_config.get_active_alarms(strict=True)
        except rpc_ops.RPCError as err:
            logging.error("O-RU refused the active-alarm-list read; mirror kept until the next cycle: %s", err)
            return
        reports: Dict[int, _Sources] = {}
        for alarm in listed:
            fault_id = self._fault_id(alarm)
            if fault_id is None or alarm.get("is_cleared"):
                continue  # a residual cleared entry carries no active state
            source = alarm.get("fault_source")
            reports.setdefault(fault_id, {})[source] = self._merge(
                self._reports.get(fault_id, {}).get(source), self._report(alarm)
            )
        self._reports = reports
        mirrored = {
            state.definition.alarm_id - RU_FAULT_ALARM_BASE
            for state in self.alarm_mgr.active_alarms()
            if RU_FAULT_ALARM_BASE <= state.definition.alarm_id < RU_FAULT_ALARM_END
        }
        stale = sorted(mirrored - set(reports))
        logging.info("O-RU active-alarm-list: faults %s; retiring %s", sorted(reports) or "none", stale or "none")
        for fault_id in sorted(reports):
            self._guarded(self._raise, fault_id, reports[fault_id])
        for fault_id in stale:
            self._guarded(
                self.alarm_mgr.clear_alarm,
                RU_FAULT_ALARM_BASE + fault_id,
                message=_STALE_MESSAGE,
                details={"fault-id": str(fault_id)},
            )

    def _apply(self, alarm):
        """Raise or clear the mirrored adapter alarm for one live O-RU fault.

        Bookkeeping mutates before the manager call: a notifier exception
        surfacing from set/clear must not desynchronise the mirror.
        """
        fault_id = self._fault_id(alarm)
        if fault_id is None:
            return
        source = alarm.get("fault_source")
        logging.info(
            "O-RU fault %s %s from %s: %s (affected: %s)",
            fault_id,
            "cleared" if alarm.get("is_cleared") else alarm.get("fault_severity"),
            source,
            alarm.get("fault_text"),
            ",".join(alarm.get("affected_objects") or []) or "-",
        )
        sources = self._reports.get(fault_id)
        if alarm.get("is_cleared"):
            if sources is None or source not in sources:
                logging.debug("Ignoring clear for unmirrored O-RU fault %s from %s", fault_id, source)
                return
            del sources[source]
            if sources:
                self._raise(fault_id, sources)  # others still report it: re-aggregate
                return
            del self._reports[fault_id]
            self.alarm_mgr.clear_alarm(
                RU_FAULT_ALARM_BASE + fault_id,
                message=alarm.get("fault_text"),
                details=self._details(fault_id, {source: self._report(alarm)}),
            )
        else:
            if sources is None:
                sources = self._reports[fault_id] = {}
            sources[source] = self._merge(sources.get(source), self._report(alarm))
            self._raise(fault_id, sources)

    def _raise(self, fault_id, sources):
        """Set the mirrored alarm to the aggregate of the sources reporting the fault."""
        ranked = sorted(sources.items(), key=lambda item: (_RANK[item[1].severity], str(item[0])))
        severity, text = ranked[0][1].severity, ranked[0][1].text
        self._ensure_registered(fault_id, severity, ranked[0][1].alarm_type)
        named = ",".join(sorted(str(name) for name in sources if name))
        message = "; ".join(part for part in (text, f"sources: {named}" if named else None) if part) or None
        self.alarm_mgr.set_alarm(
            RU_FAULT_ALARM_BASE + fault_id, severity=severity, message=message, details=self._details(fault_id, sources)
        )

    def _ensure_registered(self, fault_id, severity, alarm_type=None):
        """Register the fault's alarm definition on first sight, typed as the O-RU reports it."""
        if fault_id in self._known_faults:
            return
        try:
            self.alarm_mgr.register(
                AlarmDefinition(
                    alarm_id=RU_FAULT_ALARM_BASE + fault_id,
                    name=f"RU_FAULT_{fault_id}",
                    type=_ALARM_TYPES.get(alarm_type or "", AlarmType.EQUIPMENT),
                    default_severity=severity,
                )
            )
        except ValueError:
            # Already registered — e.g. a bridge recreated over a live manager
            logging.debug("RU fault %s already registered", fault_id)
        self._known_faults.add(fault_id)

    @staticmethod
    def _guarded(update, *args, **kwargs):
        """Run one registry update; a notifier failure is logged, not fatal to the sweep."""
        try:
            update(*args, **kwargs)
        except RuntimeError:
            logging.exception("Alarm notifier failed during reconcile; registry and mirror agree")

    def _report(self, alarm):
        """One source's report of a fault, as parsed from o-ran-fm."""
        return _Report(
            self._severity(alarm.get("fault_severity")),
            alarm.get("fault_text"),
            tuple(alarm.get("affected_objects") or ()),
            alarm.get("alarm_type"),
            alarm.get("probable_cause"),
            alarm.get("specific_problem"),
            alarm.get("proposed_repair_actions"),
            tuple(sorted((alarm.get("additional_information") or {}).items())),
            alarm.get("event_time"),
        )

    @staticmethod
    def _merge(previous, report):
        """The report to keep: a re-report of an unchanged state keeps the first.

        Only the event-time differs between a fault re-announced with the
        same leaves and the report already held, and a fresh timestamp alone
        is not a state change worth a northbound notification — the details
        would otherwise churn.
        """
        if previous is not None and previous[:-1] == report[:-1]:
            return previous
        return report

    @staticmethod
    def _details(fault_id, sources):
        """The fault's o-ran-fm leaves, aggregated over its sources, for the notifier.

        Keys are the o-ran-fm leaf names; several sources or affected objects
        are comma-joined, the text, alarm-type, probable-cause,
        specific-problem and proposed-repair-actions are the most severe
        source's that reported them, the event-time the latest reported, and
        the additional-information pairs of every source follow under their
        own identifiers (a leaf-name key is never overwritten by one).
        """
        ranked = sorted(sources.values(), key=lambda report: _RANK[report.severity])
        details = {"fault-id": str(fault_id)}
        named = sorted(str(name) for name in sources if name)
        if named:
            details["fault-source"] = ",".join(named)
        affected = sorted({obj for report in sources.values() for obj in report.affected})
        if affected:
            details["affected-objects"] = ",".join(affected)
        if ranked and ranked[0].text:
            details["fault-text"] = ranked[0].text
        for leaf, field in (
            ("alarm-type", "alarm_type"),
            ("probable-cause", "probable_cause"),
            ("specific-problem", "specific_problem"),
            ("proposed-repair-actions", "repair"),
        ):
            value = next((getattr(report, field) for report in ranked if getattr(report, field)), None)
            if value:
                details[leaf] = value
        times = [report.event_time for report in sources.values() if report.event_time]
        if times:
            details["event-time"] = max(times)
        for report in sources.values():
            for identifier, information in report.extra:
                details.setdefault(identifier, information)
        return details

    @staticmethod
    def _fault_id(alarm):
        """The alarm's fault-id when it is a uint16, else None (logged)."""
        fault_id = alarm.get("fault_id")
        if fault_id is None or not 0 <= fault_id < RU_FAULT_ID_LIMIT:
            logging.warning("Ignoring O-RU alarm without a uint16 fault-id: %s", alarm)
            return None
        return fault_id

    @staticmethod
    def _severity(literal):
        """Map an o-ran-fm fault-severity literal onto AlarmSeverity."""
        severity = _SEVERITIES.get(literal)
        if severity is None:
            logging.warning("Unknown O-RU fault severity %r", literal)
            return AlarmSeverity.INDETERMINATE
        return severity
