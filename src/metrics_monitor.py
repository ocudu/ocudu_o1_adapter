# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
Alarms derived from the gNB WS metric payloads: overload, capacity and malfunction (e.g.
fronthaul traffic missing for a misconfigured VLAN) conditions.

Each gNB component reports on its own period and in its own WS message (cells, du, ru,
cu-cp, app_resource_usage), with counters holding the delta of one report period. Every
message is checked against a set of conditions; one alarm may be fed by several of them
(e.g. one per cell). A condition turns active after `raise_consecutive` breaching reports
and inactive after `clear_consecutive` clean ones; the alarm is raised while any of its
conditions is active, at the highest severity among them. A report lacking a field (its
metric layer is disabled in the gNB) leaves the condition untouched.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from alarm_defs import AlarmId
from alarm_manager import AlarmManager, AlarmSeverity

_SEVERITY_RANK = {
    AlarmSeverity.WARNING: 1,
    AlarmSeverity.MINOR: 2,
    AlarmSeverity.MAJOR: 3,
    AlarmSeverity.CRITICAL: 4,
}

# (severity, message) of a breaching report; None for a clean one
Breach = Optional[Tuple[AlarmSeverity, str]]


# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True)
class MetricsThresholds:
    """Thresholds and debouncing of the metrics alarms."""

    raise_consecutive: int = 3
    clear_consecutive: int = 3
    cpu_major_percent: float = 85.0
    cpu_critical_percent: float = 95.0
    mem_major_percent: float = 85.0
    mem_critical_percent: float = 95.0
    max_nof_ues: int = 8192  # the CU-CP's cu_cp.max_nof_ues
    ue_major_percent: float = 90.0
    rrc_fail_percent: float = 10.0  # failed RRC connection establishments, in % of the attempted ones
    rrc_min_attempts: int = 10  # attempts the failure ratio is judged over, gathered across reports
    pdu_fail_percent: float = 10.0  # failed PDU session setups, in % of the requested ones
    pdu_min_attempts: int = 10
    l2_max_slot_percent: float = 100.0  # MAC DL slot processing max latency, in % of the slot duration
    fh_late_percent: float = 1.0  # late UL fronthaul packets, in % of the received ones


def _num(value) -> Optional[float]:
    """value as a float when it is numeric (bool excluded), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _dict(value) -> dict:
    """value when it is a dict, else an empty one."""
    return value if isinstance(value, dict) else {}


def _counts(value) -> Optional[Dict[str, float]]:
    """A counter with cause ({cause: count}) as such, a plain counter as {"total": count}; else None."""
    if isinstance(value, dict):
        return {str(k): n for k, n in ((k, _num(v)) for k, v in value.items()) if n is not None}
    count = _num(value)
    return None if count is None else {"total": count}


def _dicts(value) -> list:
    """The dict entries of value when it is a list, else nothing."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


@dataclass
class _Condition:
    """Debounce state of one condition feeding an alarm."""

    breaches: int = 0
    cleans: int = 0
    active: bool = False
    severity: AlarmSeverity = AlarmSeverity.CLEARED
    message: str = ""


@dataclass
class _CumulativeFailures:
    """Attempts and failures (per cause) of one source as the gNB counts them, cumulative since it
    started: the counts last reported, and the increase gathered since the ratio was last judged."""

    attempted: float
    failed: Dict[str, float]
    window_attempted: float = 0.0
    window_failed: Dict[str, float] = field(default_factory=dict)

    def add(self, attempted: float, failed: Dict[str, float], min_attempts: int):
        """Gather the increase up to these counts; once min_attempts are gathered, hand over the
        window as (attempted, {cause: failed}) and start a new one, else None."""
        self.window_attempted += attempted - self.attempted
        for cause, count in failed.items():
            self.window_failed[cause] = self.window_failed.get(cause, 0.0) + max(count - self.failed.get(cause, 0.0), 0)
        self.attempted, self.failed = attempted, failed
        if self.window_attempted < max(min_attempts, 1):
            return None
        window = (self.window_attempted, self.window_failed)
        self.window_attempted, self.window_failed = 0.0, {}
        return window


class MetricsMonitor:  # pylint: disable=too-few-public-methods
    """Evaluates gNB metric payloads and raises/clears the metrics alarms."""

    def __init__(self, alarm_mgr: AlarmManager, thresholds: MetricsThresholds = MetricsThresholds()):
        self._alarm_mgr = alarm_mgr
        self._thr = thresholds
        self._conditions: Dict[AlarmId, Dict[str, _Condition]] = defaultdict(dict)
        self._failures: Dict[Tuple[AlarmId, str], _CumulativeFailures] = {}

    def handle_metrics(self, data: dict) -> None:
        """Check one decoded WS metric message."""
        self._check_resource_usage(data.get("app_resource_usage"))
        self._check_scheduler(data.get("cells"))
        self._check_mac(_dict(_dict(data.get("du")).get("du_high")).get("mac"))
        self._check_ofh(_dict(data.get("ru")).get("ofh"))
        cu_cp = data.get("cu-cp")
        if isinstance(cu_cp, dict):
            self._check_amf(cu_cp.get("ngaps"))
            self._check_pdu_sessions(cu_cp.get("ngaps"))
            self._check_ue_capacity(cu_cp.get("rrcs"))
            self._check_rrc_establishments(cu_cp.get("rrcs"))

    # --- checks per WS message type ---

    def _check_resource_usage(self, usage) -> None:
        if not isinstance(usage, dict):
            return
        cpu = _num(usage.get("cpu_usage_percent"))
        if cpu is not None:
            breach = self._percent_breach(cpu, self._thr.cpu_major_percent, self._thr.cpu_critical_percent, "CPU")
            self._update(AlarmId.CPU_OVERLOAD, "app", breach)
        mem = _num(usage.get("mem_usage_percent"))
        if mem is not None:
            breach = self._percent_breach(mem, self._thr.mem_major_percent, self._thr.mem_critical_percent, "memory")
            self._update(AlarmId.MEMORY_OVERLOAD, "app", breach)

    def _check_scheduler(self, cells) -> None:
        for cell in _dicts(cells):
            metrics = cell.get("cell_metrics")
            if not isinstance(metrics, dict):
                continue
            error_ind = _num(metrics.get("error_indication_count"))
            late_dl = _num(metrics.get("late_dl_harqs"))
            late_ul = _num(metrics.get("late_ul_harqs"))
            if error_ind is None and late_dl is None and late_ul is None:
                continue
            error_ind, late_dl, late_ul = error_ind or 0, late_dl or 0, late_ul or 0
            pci = metrics.get("pci")
            # TODO: raise a CELL_PRB_USAGE_HIGH alarm (2002, MINOR, probable cause "congestion") on high
            # DL/UL PRB usage, once the gNB reports it relative to the cell's PRBs (e.g. as a percentage or
            # with the cell's PRB count); pdsch/pusch_prbs_used_per_tdd_slot_idx are absolute and TDD only.
            breach: Breach = None
            if error_ind > 0:
                # the L1 rejected late slot messages
                breach = (AlarmSeverity.CRITICAL, f"PCI {pci}: {int(error_ind)} FAPI error indications")
            elif late_dl > 0 or late_ul > 0:
                breach = (AlarmSeverity.MAJOR, f"PCI {pci}: {int(late_dl)} DL / {int(late_ul)} UL late HARQs")
            self._update(AlarmId.L2_REALTIME_DEADLINE_MISS, f"sched-pci{pci}", breach)

    def _check_mac(self, mac) -> None:
        if not isinstance(mac, dict):
            return
        for cell in _dicts(mac.get("dl")):
            avg_us = _num(cell.get("average_latency_us"))
            max_us = _num(cell.get("max_latency_us"))
            cpu = _num(cell.get("cpu_usage_percent"))
            if avg_us is None or max_us is None or cpu is None or avg_us <= 0:
                continue
            # cpu_usage_percent is the average latency in % of the slot duration, which the
            # report does not carry itself; scale it to the max latency
            max_slot_percent = max_us / avg_us * cpu
            pci = cell.get("pci")
            breach: Breach = None
            if max_slot_percent > self._thr.l2_max_slot_percent:
                breach = (
                    AlarmSeverity.MAJOR,
                    f"PCI {pci}: MAC DL max slot latency {max_us:.0f} us ({max_slot_percent:.0f}% of the slot)",
                )
            self._update(AlarmId.L2_REALTIME_DEADLINE_MISS, f"mac-pci{pci}", breach)

    def _check_ofh(self, ofh) -> None:
        if not isinstance(ofh, dict):
            return
        timing = ofh.get("timing_stats")
        if isinstance(timing, dict):
            skipped = _num(timing.get("nof_skipped_symbols"))
            if skipped is not None:
                breach: Breach = None
                if skipped > 0:
                    breach = (AlarmSeverity.MAJOR, f"{int(skipped)} OFH symbols skipped by the timing source")
                self._update(AlarmId.FRONTHAUL_TIMING_VIOLATION, "ofh-timing", breach)
        for cell in _dicts(ofh.get("cells")):
            self._check_ofh_cell(cell)

    def _check_ofh_cell(self, cell: dict) -> None:
        pci = cell.get("pci")
        ul, dl = _dict(cell.get("ul")), _dict(cell.get("dl"))
        # every counter of these two blocks is a missed or late symbol, slot or message
        counters = {**_dict(ul.get("rx_window_stats")), **_dict(dl.get("transmitter_stats"))}

        # missed rather than late: their own alarms
        missed_prach = _num(counters.pop("nof_missed_prach_occasions", None))
        self._check_reception(AlarmId.PRACH_RECEPTION_FAILURE, pci, missed_prach, "PRACH occasions")
        missed_ul = _num(counters.pop("nof_missed_uplink_symbols", None))
        self._check_reception(AlarmId.UL_RECEPTION_FAILURE, pci, missed_ul, "UL symbols")

        self._check_fh_timing(pci, _dict(ul.get("received_packets")), counters)

    def _check_fh_timing(self, pci, rx: dict, counters: dict) -> None:
        if not rx and not counters:
            return

        problems = []
        total, late = _num(rx.get("total")), _num(rx.get("late"))
        if total and late is not None:
            late_percent = 100.0 * late / total
            if late_percent > self._thr.fh_late_percent:
                problems.append(f"{late_percent:.1f}% late UL packets")
        for name, value in counters.items():
            count = _num(value)
            if count:
                problems.append(f"{name}={int(count)}")

        breach: Breach = (AlarmSeverity.MAJOR, f"PCI {pci}: " + ", ".join(problems)) if problems else None
        self._update(AlarmId.FRONTHAUL_TIMING_VIOLATION, f"ofh-pci{pci}", breach)

    def _check_reception(self, alarm_id: AlarmId, pci, missed: Optional[float], what: str) -> None:
        """PRACH occasions or UL symbols whose reception window closed without their packets: the
        O-RU's U-plane does not reach the DU (e.g. a misconfigured VLAN or eAxC), or not in time."""
        if missed is None:
            return
        breach: Breach = None
        if missed > 0:
            breach = (AlarmSeverity.MAJOR, f"PCI {pci}: {int(missed)} {what} missed")
        self._update(alarm_id, f"ofh-pci{pci}", breach)

    def _check_amf(self, ngaps) -> None:
        if not isinstance(ngaps, dict):
            return
        for amf in _dicts(ngaps.get("ngap")):
            connected = amf.get("connected")
            if not isinstance(connected, bool):
                continue
            name = amf.get("amf_name") or "AMF"
            breach: Breach = None if connected else (AlarmSeverity.MAJOR, f"{name} not connected")
            # the connection state is reported as is, not sampled: no debouncing
            self._update(AlarmId.AMF_CONNECTION_LOSS, f"amf-{name}", breach, raise_consecutive=1, clear_consecutive=1)

    def _check_ue_capacity(self, rrcs) -> None:
        if not isinstance(rrcs, dict) or self._thr.max_nof_ues <= 0:
            return
        peaks = [
            _num(_dict(du.get("rrc_connection_number")).get("max_nof_rrc_connections")) for du in _dicts(rrcs.get("du"))
        ]
        counts = [c for c in peaks if c is not None]
        if not counts:
            return
        # the sum of the per-DU peaks, an upper bound of the CU-CP's peak
        ues = sum(counts)
        percent = 100.0 * ues / self._thr.max_nof_ues
        breach: Breach = None
        if percent >= 100.0:
            breach = (AlarmSeverity.CRITICAL, f"{int(ues)} RRC connections, at the limit of {self._thr.max_nof_ues}")
        elif percent >= self._thr.ue_major_percent:
            breach = (AlarmSeverity.MAJOR, f"{int(ues)} RRC connections, {percent:.0f}% of {self._thr.max_nof_ues}")
        self._update(AlarmId.UE_CAPACITY_NEAR_LIMIT, "cu-cp", breach)

    def _check_rrc_establishments(self, rrcs) -> None:
        if not isinstance(rrcs, dict):
            return
        for du in _dicts(rrcs.get("du")):
            est = _dict(du.get("rrc_connection_establishment"))
            attempted = _counts(est.get("attempted_rrc_connection_establishments"))
            failed = _counts(est.get("failed_rrc_connection_establishments"))
            if attempted is not None and failed is not None:
                self._check_failures(
                    AlarmId.RRC_ESTABLISHMENT_FAILURES,
                    f"DU {du.get('gnb_du_id')}",
                    "RRC connection establishments",
                    counts=(sum(attempted.values()), failed),
                    thresholds=(self._thr.rrc_fail_percent, self._thr.rrc_min_attempts),
                )

    def _check_pdu_sessions(self, ngaps) -> None:
        if not isinstance(ngaps, dict):
            return
        for amf in _dicts(ngaps.get("ngap")):
            # the gNB reports one S-NSSAI only, the last one with PDU sessions
            sessions = _dict(amf.get("pdu_session_management"))
            requested = _num(sessions.get("nof_pdu_sessions_requested_to_setup"))
            failed = _counts(sessions.get("nof_pdu_sessions_failed_to_setup"))
            if requested is not None and failed is not None:
                self._check_failures(
                    AlarmId.PDU_SESSION_SETUP_FAILURES,
                    f"{amf.get('amf_name') or 'AMF'} {sessions.get('s-nssai', '')}".strip(),
                    "PDU session setups",
                    counts=(requested, failed),
                    thresholds=(self._thr.pdu_fail_percent, self._thr.pdu_min_attempts),
                )

    def _check_failures(  # pylint: disable=too-many-arguments
        self,
        alarm_id: AlarmId,
        source: str,
        what: str,
        *,
        counts: Tuple[float, Dict[str, float]],
        thresholds: Tuple[float, int],
    ) -> None:
        """Failure ratio of cumulative (attempted, {cause: failed}) counts. It is judged once at
        least min_attempts were made, so that a quiet cell does not alarm on one failure out of two."""
        (attempted, failed), (fail_percent, min_attempts) = counts, thresholds
        last = self._failures.get((alarm_id, source))
        if last is None or attempted < last.attempted:
            # first report, or the gNB restarted: a new baseline
            self._failures[(alarm_id, source)] = _CumulativeFailures(attempted, failed)
            return
        window = last.add(attempted, failed, min_attempts)
        if window is None:
            return

        self._update(alarm_id, source, self._failure_breach(source, what, window, fail_percent))

    @staticmethod
    def _failure_breach(source: str, what: str, window: Tuple[float, Dict[str, float]], fail_percent: float) -> Breach:
        nof_attempted, nof_failed = window[0], sum(window[1].values())
        percent = 100.0 * nof_failed / nof_attempted
        if percent <= fail_percent:
            return None
        causes = ", ".join(f"{cause}={int(n)}" for cause, n in window[1].items() if n)
        return (
            AlarmSeverity.CRITICAL if nof_failed >= nof_attempted else AlarmSeverity.MAJOR,
            f"{source}: {int(nof_failed)} of {int(nof_attempted)} {what} failed ({percent:.0f}%; {causes})",
        )

    @staticmethod
    def _percent_breach(value: float, major: float, critical: float, what: str) -> Breach:
        if value >= critical:
            return AlarmSeverity.CRITICAL, f"{what} usage {value:.0f}% (critical threshold {critical:.0f}%)"
        if value >= major:
            return AlarmSeverity.MAJOR, f"{what} usage {value:.0f}% (major threshold {major:.0f}%)"
        return None

    # --- debouncing and alarm bookkeeping ---

    def _update(  # pylint: disable=too-many-arguments
        self,
        alarm_id: AlarmId,
        source: str,
        breach: Breach,
        *,
        raise_consecutive: Optional[int] = None,
        clear_consecutive: Optional[int] = None,
    ) -> None:
        """Feed one report's outcome for (alarm_id, source) and sync the alarm on a change.

        Only a change of the active state or severity goes northbound: the message of a
        breach carries per-report counts and is refreshed with the next such change.
        """
        cond = self._conditions[alarm_id].setdefault(source, _Condition())
        was = (cond.active, cond.severity)
        if breach:
            cond.breaches += 1
            cond.cleans = 0
            if cond.active or cond.breaches >= (raise_consecutive or self._thr.raise_consecutive):
                cond.active = True
                cond.severity, cond.message = breach
        else:
            cond.cleans += 1
            cond.breaches = 0
            if cond.active and cond.cleans >= (clear_consecutive or self._thr.clear_consecutive):
                cond.active = False
                cond.severity, cond.message = AlarmSeverity.CLEARED, ""
        if (cond.active, cond.severity) != was:
            self._sync_alarm(alarm_id)

    def _sync_alarm(self, alarm_id: AlarmId) -> None:
        active = {src: c for src, c in self._conditions[alarm_id].items() if c.active}
        if not active:
            logging.info("Metrics alarm %d cleared", alarm_id)
            self._alarm_mgr.clear_alarm(alarm_id, message="back to normal")
            return
        severity = max((c.severity for c in active.values()), key=_SEVERITY_RANK.__getitem__)
        self._alarm_mgr.set_alarm(
            alarm_id,
            severity=severity,
            message="; ".join(c.message for _, c in sorted(active.items())),
            details={src: c.message for src, c in sorted(active.items())},
        )
