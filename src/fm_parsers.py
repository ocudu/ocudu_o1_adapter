# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Parsers for o-ran-fm alarm payloads (notifications and the active-alarm-list).

Both parsers return the same plain dict per alarm (fault_id, fault_source,
fault_severity, is_cleared, fault_text, event_time, affected_objects and the
o-ran-fm 2024-08-12 leaves probable_cause, specific_problem,
proposed_repair_actions, alarm_type, additional_information), so a consumer
can treat a live alarm-notif and an active-alarm-list entry alike. Both go
through xmltodict with the o-ran-fm namespace stripped, so one mapping serves
the notification and the list entry.
"""

from xml.parsers.expat import ExpatError

import xmltodict

from xml_utils import ensure_list

_NAMESPACES = {
    "urn:ietf:params:xml:ns:netconf:notification:1.0": None,
    "urn:o-ran:fm:1.0": None,
}


def _text(value):
    """The text of an xmltodict leaf; a leaf with XML attributes deserializes to a mapping."""
    return value.get("#text") if isinstance(value, dict) else value


def _int_or_none(value):
    try:
        return int(_text(value))
    except (TypeError, ValueError):
        return None


def _additional_information(pairs):
    """{identifier: information} from o-ran-fm additional-information entries; blank identifiers dropped."""
    result = {}
    for identifier, information in pairs:
        identifier = (identifier or "").strip()
        if identifier:
            result[identifier] = (information or "").strip()
    return result


def _alarm_from_entry(entry):
    """Map one o-ran-fm alarm mapping (an alarm-notif or an active-alarms entry) to a plain dict.

    Tolerates the shapes xmltodict produces for one, many or no children, for
    leaves carrying XML attributes, and for affected-objects given as mappings
    or bare names.
    """
    affected = []
    for item in ensure_list(entry.get("affected-objects")):
        # an empty element deserializes to None, a mapping carries the name
        # under "name", a bare string is taken as the name itself
        name = _text(item.get("name")) if isinstance(item, dict) else item
        if name:
            affected.append(name)
    extra = _additional_information(
        (_text(item.get("identifier")), _text(item.get("information")))
        for item in ensure_list(entry.get("additional-information"))
        if isinstance(item, dict)
    )
    return {
        "fault_id": _int_or_none(entry.get("fault-id")),
        "fault_source": _text(entry.get("fault-source")),
        "fault_severity": _text(entry.get("fault-severity")),
        "is_cleared": _text(entry.get("is-cleared")) == "true",
        "fault_text": _text(entry.get("fault-text")),
        "event_time": _text(entry.get("event-time")),
        "affected_objects": affected,
        # o-ran-fm 2024-08-12 leaves (optional; absent on older O-RUs)
        "probable_cause": _text(entry.get("probable-cause")),
        "specific_problem": _text(entry.get("specific-problem")),
        "proposed_repair_actions": _text(entry.get("proposed-repair-actions")),
        "alarm_type": _text(entry.get("alarm-type")),
        "additional_information": extra,
    }


def parse_alarm_notification(notification_xml):
    """Parse an o-ran-fm alarm-notif out of a NETCONF notification.

    Returns the alarm dict, or None when the payload is not an alarm-notif
    (another notification, or not XML at all).
    """
    try:
        parsed = xmltodict.parse(notification_xml, process_namespaces=True, namespaces=_NAMESPACES)
    except ExpatError:
        return None
    notification = (parsed or {}).get("notification")
    container = notification if isinstance(notification, dict) else parsed or {}
    alarm = container.get("alarm-notif")
    return _alarm_from_entry(alarm) if isinstance(alarm, dict) else None


def parse_active_alarms(fm_data):
    """Map an o-ran-fm active-alarm-list subtree (xmltodict output) to alarm dicts."""
    container = (fm_data or {}).get("active-alarm-list") or {}
    # an empty <active-alarms/> element deserializes to None, not a mapping
    return [
        _alarm_from_entry(entry) for entry in ensure_list(container.get("active-alarms")) if isinstance(entry, dict)
    ]
