# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""Performance bridge: O-RU measurement-result-stats into the PM stream.

Parses o-ran-performance-management measurement-result-stats notifications
into flat metric samples and streams them through PmMetrics — the same PM-job
filtering, envelope schema and push queue the gNB metrics use, so O-RU
fronthaul counters land next to the gNB's own KPIs. Samples only go
northbound to PM jobs the SMO configured through the DU-facing NETCONF
config; without a job they are parsed and logged.

Metric naming follows the o-ran-performance-management result tree:
ru.<group>.<measurement-object> for a plain count reported for the whole
O-RU (ru.rx-window-stats.RX_ON_TIME) and ru.<group>.<measurement-object>
followed by one segment per nesting level for everything else — a result
list entry is named by its keys in document order (eaxc-id, object-unit-id,
bin-id, tx-array-carrier and the other list keys of the result submodules),
a result container by its tag, and a leaf other than count by its tag:
ru.rx-window-stats.RX_ON_TIME.3 (per eAxC),
ru.epe-statistics.TEMPERATURE.PA1.average,
ru.transceiver-stats.RX_POWER.1.min.value. Several units of one measurement
object therefore stay distinct samples. Non-numeric leaves (timestamps,
names) are skipped, leaves outside the module's namespace are ignored, and
the multiple-* lists (batches of earlier intervals, reported alongside the
mandated latest result) are skipped.

On each connect cycle the bridge can also configure and activate the O-RU's
measurement objects (two-phase, through the client), gated on the O-RU
implementing o-ran-performance-management per its yang-library — the module
is not universal across O-RUs — and subject to the client's role: per
Table 6.5-1 the hybrid-odu role reads performance management but does not
write it, so the client skips the write with one log line. An O-RU lists the
measurement objects it supports in the module's config-false
measurement-capabilitites container (its own spelling); when it does, only
advertised rx-window objects are requested — all of them when the profile
names none — so the edit is not rejected first and retried; an O-RU exposing
no capabilities gets the configured set and the client's reject-and-retry.
"""

import logging
import math
import re
import xml.etree.ElementTree as ET

from ncclient.operations import rpc as rpc_ops

from xml_utils import ensure_list

_PM_NS = "{urn:o-ran:performance-management:1.0}"
_PM_MODULE = "o-ran-performance-management"
# the measurement-capabilitites lists (the module's spelling) -> group names for the log
_CAPABILITY_LISTS = {
    "rx-window-objects": "rx-window",
    "tx-stats-objects": "tx-stats",
    "transceiver-objects": "transceiver",
    "epe-stats-objects": "epe",
    "symbol-rssi-stats-objects": "symbol-rssi",
    "shared-cell-stats-objects": "shared-cell",
    "tssi-stats-objects": "tssi",
    "rssi-stats-objects": "rssi",
    "tx-antenna-stats-objects": "tx-antenna",
    "tx-output-power-stats-objects": "tx-output-power",
    "ethernet-objects": "ethernet",
}
_RX_WINDOW_UNIT = "RU"  # the object-unit the measurement template requests
_SKIP = object()  # _requested_objects: a capability list that leaves nothing to request

# Keys of the result lists below a measurement-object entry (the o-ran-pm-*
# submodules); their values name the result. "name" is a key only there
# (transport results) — at entry level it is the O-RU's own name and no key.
_RESULT_KEYS = frozenset(
    {
        "object-unit-id",
        "eaxc-id",
        "bin-id",
        "symbol-index",
        "band-number",
        "rx-array",
        "rx-array-element",
        "rx-array-carrier",
        "tx-array",
        "tx-array-element",
        "tx-array-carrier",
        "related-o-ru-connector",
        "processing-elements",
        "name",
    }
)
_NUMBER = re.compile(r"-?\d+(\.\d+)?([eE][+-]?\d+)?")


def _numeric(text):
    """int or finite float value of a leaf's text, None when non-numeric."""
    text = (text or "").strip()
    if not _NUMBER.fullmatch(text):
        return None
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    value = float(text)
    return value if math.isfinite(value) else None


def _local(tag):
    return tag.split("}", 1)[-1]


def _pm_children(element):
    """The element's children in the module's namespace; anything else is not a result."""
    return [child for child in element if child.tag.startswith(_PM_NS)]


def _collect(element, scope, samples, nested):
    """Samples under one element: numeric leaves here, result lists and containers recursed."""
    children = _pm_children(element)
    if nested:
        keys = [(child.text or "").strip() for child in children if _local(child.tag) in _RESULT_KEYS]
        scope = ".".join([scope, *[key for key in keys if key]])
    for child in children:
        tag = _local(child.tag)
        if tag == "measurement-object" or (nested and tag in _RESULT_KEYS) or tag.startswith("multiple-"):
            continue
        if len(child):
            keyed = any(_local(grandchild.tag) in _RESULT_KEYS for grandchild in _pm_children(child))
            _collect(child, scope if keyed else f"{scope}.{tag}", samples, nested=True)
            continue
        value = _numeric(child.text)
        if value is None:
            continue
        samples.append({"name": scope if tag == "count" else f"{scope}.{tag}", "value": value})


def _leaf_text(value):
    """The text of an xmltodict leaf; a leaf with XML attributes deserializes to a mapping."""
    text = value.get("#text") if isinstance(value, dict) else value
    return str(text).strip() if text is not None else None


def parse_measurement_capabilities(pm_data):
    """{group: {measurement-object: [object-unit, ...]}} from a <get> of the PM tree.

    Reads the config-false measurement-capabilitites container (the module's
    own spelling), where an O-RU lists the measurement objects it supports
    per group; {} when it exposes none.
    """
    objects_tree = (pm_data or {}).get("performance-measurement-objects") or {}
    container = objects_tree.get("measurement-capabilitites") or {}
    groups = {}
    for list_name, group in _CAPABILITY_LISTS.items():
        objects = {}
        for entry in ensure_list(container.get(list_name)):
            name = _leaf_text(entry.get("measurement-object")) if isinstance(entry, dict) else None
            if name:
                units = (_leaf_text(value) for value in ensure_list(entry.get("object-unit")))
                objects[name] = [unit for unit in units if unit]
        if objects:
            groups[group] = objects
    return groups


def parse_measurement_stats(notification_xml):
    """Flatten a measurement-result-stats notification into metric samples.

    Returns [] for non-PM notifications or unparseable payloads. Each sample
    is {"name": ..., "value": ...}. An entry without a measurement-object
    cannot be named and is skipped (logged at DEBUG).
    """
    try:
        root = ET.fromstring(notification_xml)
    except ET.ParseError:
        return []
    samples: list = []
    for stats in root.iter(f"{_PM_NS}measurement-result-stats"):
        for entry in _pm_children(stats):
            group = _local(entry.tag)
            measurement_object = (entry.findtext(f"{_PM_NS}measurement-object") or "").strip()
            if not measurement_object:
                logging.debug("Skipping %s entry without a measurement-object", group)
                continue
            try:
                _collect(entry, f"ru.{group}.{measurement_object}", samples, nested=False)
            except RecursionError:
                logging.warning("Ignoring a %s/%s result nested beyond reason", group, measurement_object)
    return samples


class RuPmBridge:
    """Stream O-RU performance measurements through the adapter's PM path."""

    def __init__(self, pm_metrics, pm_config=None, configure=False):
        self.pm_metrics = pm_metrics
        self.pm_config = pm_config
        self.configure_enabled = configure
        self._streaming = False

    def handle_notification(self, notification_xml):
        """Stream a measurement-result-stats notification; ignore other events.

        Suitable for MplaneSession.register_notification_handler.
        """
        samples = parse_measurement_stats(notification_xml)
        if not samples:
            return
        if not self._streaming:
            self._streaming = True
            logging.info("O-RU performance stream started: %d samples, e.g. %s", len(samples), samples[0]["name"])
        else:
            logging.debug("O-RU performance samples: %d", len(samples))
        self.pm_metrics.handle_ru_metrics(samples)

    def on_cycle(self, ru_config):
        """Start of a connect cycle: log the stream afresh; configure the O-RU when enabled.

        Suitable for MplaneSession.register_cycle_handler.
        """
        self._streaming = False
        if self.configure_enabled:
            self.configure(ru_config)

    def configure(self, ru_config):
        """Configure and activate the O-RU's measurement objects once per cycle.

        Skips O-RUs whose yang-library (or legacy modules-state) does not
        advertise o-ran-performance-management; that read is strict, so a
        transport failure or reply timeout recycles the session while an
        rpc-error is logged. An rpc-error on the measurement edit means the
        O-RU is alive but rejected the write: performance measurement is
        optional surface, so it is logged and the session lives — only
        transport-level failures propagate. The client drops the objects the
        O-RU rejects and retries with the rest; the set it converged on is
        requested as such from the next cycle on. The edits run without a
        watchdog feed: at most two per retry round, each bounded by the RPC
        timeout.
        What is requested comes from _requested_objects.
        """
        try:
            advertised = ru_config.get_module_capabilities_map(strict=True)
        except rpc_ops.RPCError as err:
            logging.warning("O-RU rejected the yang-library read; measurement configuration skipped: %s", err)
            return
        if _PM_MODULE not in advertised:
            logging.info(
                "O-RU does not advertise %s in its yang-library; measurement configuration skipped", _PM_MODULE
            )
            return
        pm_config = self._requested_objects(ru_config)
        if pm_config is _SKIP:
            return
        try:
            active = ru_config.configure_perf_measurement(pm_config)
        except rpc_ops.RPCError as err:
            logging.warning("O-RU rejected the measurement configuration: %s", err)
            return
        requested = (pm_config or {}).get("rx_window_objects")
        if active and (requested is None or set(requested) != set(active)):
            self.pm_config = {**(self.pm_config or {}), "rx_window_objects": list(active)}
            logging.info("Measurement objects converged on %s; later cycles request exactly these", list(active))

    def _requested_objects(self, ru_config):
        """The measurement config to push: the profile's, narrowed to what the O-RU advertises.

        An O-RU lists the measurement objects it supports in the config-false
        measurement-capabilitites container; when it exposes rx-window ones,
        only those are requested — all of them when the profile names none,
        else the profile's set minus the unadvertised (logged) — so the edit
        is not rejected first and then retried. An O-RU exposing no
        capabilities gets the configured set as is — None for the client's
        defaults — and the client's reject-and-retry (read non-strictly: a
        failed read counts as "none exposed"). _SKIP when a capability list
        leaves nothing to request.
        """
        capabilities = parse_measurement_capabilities(ru_config.get_perf_measurement_config(operational=True))
        if not capabilities:
            logging.info(
                "O-RU exposes no measurement-capabilitites; requesting the configured set, learning from rejections"
            )
            return self.pm_config
        pm_config = dict(self.pm_config or {})
        logging.info(
            "O-RU advertises measurement objects: %s",
            ", ".join(f"{group} ({len(objects)})" for group, objects in capabilities.items()),
        )
        logging.debug("O-RU measurement capabilities: %s", capabilities)
        supported = [
            name
            for name, units in (capabilities.get("rx-window") or {}).items()
            if not units or _RX_WINDOW_UNIT in units
        ]
        requested = pm_config.get("rx_window_objects")
        objects = supported if requested is None else [name for name in requested if name in supported]
        if requested is not None and set(requested) - set(objects):
            logging.info(
                "Measurement objects not advertised by the O-RU (unit %s), not requested: %s",
                _RX_WINDOW_UNIT,
                sorted(set(requested) - set(objects)),
            )
        if not objects:
            logging.warning(
                "O-RU advertises none of the rx-window measurement objects; measurement configuration skipped"
            )
            return _SKIP
        return {**pm_config, "rx_window_objects": objects}
