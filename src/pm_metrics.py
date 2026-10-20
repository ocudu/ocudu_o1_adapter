# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""
PM (performance management) metrics processing for the O1 adapter.

Handles gNB WS metric payloads (flattening, KPI rewriting per TS 28.554) and
O-RU samples from the M-plane performance bridge, filtering both per PM job
and pushing envelopes to the configured stream targets or PM files.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from netconf_to_yaml import active_job_ids
from pm_files import PmFiles
from state import AppState

# Version (major, minor) of the gNB JSON metrics this adapter is written for, reported by the gNB in its
# metrics_subscribe response: the major version must match (a different one renamed or removed fields),
# the minor one is the oldest supported (a later one only added fields).
SUPPORTED_METRICS_VERSION = (1, 0)

# 1:1 gNB scalar -> 3GPP KPI rename + rescale (TS 28.554). Aggregated KPIs live in
# _derived_kpis. Note: spec §6.4.2 spells "VirtualResUtilization" as "VirtualResUtilizaiton";
# we emit the corrected form.
_KPI_MAPPINGS: dict[str, tuple[str, float]] = {
    "cu-up.pdcp.dl.average_latency_us": ("DLDelay_gNBCUUP", 0.01),
    "cu-up.pdcp.ul.average_latency_us": ("ULDelay_gNBCUUP", 0.01),
    "app_resource_usage.cpu_usage_percent": ("VirtualResUtilization", 1.0),
}


def _collect_scalars(prefix: str, value, out: list) -> None:
    """Walk a nested dict; append {name: <prefix.path>, value: v} for each numeric leaf.

    Lists and strings are skipped; bool is treated as non-numeric. Names in _KPI_MAPPINGS
    are rewritten and rescaled.
    """
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        mapping = _KPI_MAPPINGS.get(prefix)
        if mapping is None:
            out.append({"name": prefix, "value": value})
        else:
            kpi_name, scale = mapping
            out.append({"name": kpi_name, "value": value * scale})
        return
    if isinstance(value, dict):
        for k, v in value.items():
            child = f"{prefix}.{k}" if prefix else str(k)
            _collect_scalars(child, v, out)


def _derived_kpis(data: dict) -> list:
    """Aggregated 3GPP KPIs; entries whose source fields are absent are skipped."""
    out = []

    dl_samples, ul_samples = [], []
    for cell in data.get("cells") or []:
        if not isinstance(cell, dict):
            continue
        for ue in cell.get("ue_list") or []:
            if not isinstance(ue, dict):
                continue
            if isinstance(ue.get("dl_brate"), (int, float)):
                dl_samples.append(ue["dl_brate"])
            if isinstance(ue.get("ul_brate"), (int, float)):
                ul_samples.append(ue["ul_brate"])
    if dl_samples:
        out.append({"name": "DlUeThroughput_Cell", "value": sum(dl_samples) / len(dl_samples) / 1000.0})
    if ul_samples:
        out.append({"name": "UlUeThroughput_Cell", "value": sum(ul_samples) / len(ul_samples) / 1000.0})

    # DLLat_gNB-DU: TS 28.554 §6.3.1.1 restricts this to packets arriving with an empty
    # DL queue. The source field sum_sdu_latency_us accumulates over every SDU, so under
    # load this value drifts above the spec quantity.
    tx = (data.get("rlc_metrics") or {}).get("tx") or {}
    sdus = tx.get("num_sdus")
    if isinstance(sdus, (int, float)) and sdus > 0:
        total_us = tx.get("sum_sdu_latency_us")
        if isinstance(total_us, (int, float)):
            out.append({"name": "DLLat_gNB-DU", "value": (total_us / sdus) * 0.01})
        dropped = tx.get("num_dropped_sdus") or 0
        discarded = tx.get("num_discarded_sdus") or 0
        out.append({"name": "DLRelPSR_Uu", "value": 100.0 * (1.0 - (dropped + discarded) / sdus)})

    rx = (data.get("rlc_metrics") or {}).get("rx") or {}
    pdus = rx.get("num_pdus")
    if isinstance(pdus, (int, float)) and pdus > 0:
        lost = rx.get("num_lost_pdus") or 0
        malformed = rx.get("num_malformed_pdus") or 0
        out.append({"name": "ULRelPSR_Uu", "value": 100.0 * (1.0 - (lost + malformed) / pdus)})

    return out


def _flatten_cells(data: dict, out: list) -> None:
    """Flatten data['cells'][*] scalar fields, cell_metrics, and ue_list into out."""
    cells = data.get("cells")
    if not isinstance(cells, list):
        return
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        cell_id = cell.get("cellId")
        for name, value in cell.items():
            if name == "cellId":
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out.append({"name": name, "value": value, "cellId": cell_id})
        cell_metrics = cell.get("cell_metrics")
        if isinstance(cell_metrics, dict):
            _collect_scalars("cells.cell_metrics", cell_metrics, out)
        ue_list = cell.get("ue_list")
        if isinstance(ue_list, list):
            for ue in ue_list:
                if isinstance(ue, dict):
                    _collect_scalars("cells.ue_list", ue, out)


def _flatten_mac_dl(data: dict, out: list) -> None:
    """Flatten data.du.du_high.mac.dl[*] scalar fields into out."""
    mac_dl = (((data.get("du") or {}).get("du_high") or {}).get("mac") or {}).get("dl")
    if not isinstance(mac_dl, list):
        return
    for entry in mac_dl:
        if isinstance(entry, dict):
            _collect_scalars("du.du_high.mac.dl", entry, out)


def _flatten_metrics(data: dict) -> list:
    """Flatten the gNB WS metric payload into a list of {name, value} entries."""
    out: list[dict[str, Any]] = []
    _flatten_cells(data, out)

    for top_key in ("rlc_metrics", "app_resource_usage", "buffer_pool", "du_low"):
        block = data.get(top_key)
        if isinstance(block, dict):
            _collect_scalars(top_key, block, out)

    pdcp = (data.get("cu-up") or {}).get("pdcp")
    if isinstance(pdcp, dict):
        _collect_scalars("cu-up.pdcp", pdcp, out)

    _flatten_mac_dl(data, out)

    out.extend(_derived_kpis(data))
    return out


def _check_metrics_version(result: Any) -> None:
    """Compare the metrics version reported in the gNB's metrics_subscribe response with the supported one."""
    version = result.get("metrics_version") if isinstance(result, dict) else None
    if not isinstance(version, str):
        logging.warning("gNB does not report a metrics version, assuming a gNB older than metrics versioning")
        return
    try:
        major, minor = (int(v) for v in version.split("."))
    except ValueError:
        logging.warning("gNB metrics version %s is not of the form major.minor", version)
        return
    expected_major, min_minor = SUPPORTED_METRICS_VERSION
    if major != expected_major or minor < min_minor:
        logging.warning(
            "gNB metrics version %s is not supported (%d.%d or later %d.x expected): metrics may be missing or misread",
            version,
            expected_major,
            min_minor,
            expected_major,
        )
        return
    logging.info("gNB metrics version %s", version)


class PmMetrics:
    """Processes gNB WS metric payloads and O-RU samples, and pushes PM envelopes to stream targets or PM files."""

    def __init__(self, state: AppState, profile: str = "gnb", pm_files: Optional[PmFiles] = None):
        self._state = state
        self._profile = profile
        self._pm_files = pm_files
        self._loop: Optional[asyncio.AbstractEventLoop] = None  # bound by run_pusher

    def bind_loop(self) -> None:
        """Bind the running event loop: O-RU samples from other threads are handed to it."""
        self._loop = asyncio.get_running_loop()

    def _active_jobs(self) -> dict:
        """PM jobs that are UNLOCKED and report by stream or by file."""
        return {job_id: self._state.pm_jobs[job_id] for job_id in active_job_ids(self._state.pm_jobs)}

    async def handle_ws_message(self, msg: str) -> None:
        """Dispatch WS messages by component type, and report them to the active PM jobs."""
        try:
            data = json.loads(msg)
        except json.JSONDecodeError:
            logging.error("WS non-JSON message: %s", msg)
            return

        if not isinstance(data, dict):
            logging.error("WS message is not a JSON object: %s", msg)
            return

        if data.get("cmd"):
            logging.debug("WS command response: %s", data)
            if data["cmd"] == "metrics_subscribe" and "error" not in data:
                _check_metrics_version(data.get("result"))
            return

        for key in ("cu-cp", "du", "cells"):
            block = data.get(key)
            if block:
                logging.debug("WS %s metrics: %s", key, block)
        # TODO: check for AMF connection status

        active_jobs = self._active_jobs()
        if not active_jobs:
            return
        self._stream_metrics(_flatten_metrics(data), active_jobs)

    def handle_ru_metrics(self, metrics: list) -> None:
        """Stream O-RU metric samples through the PM-job filter and push queue.

        Samples share the WS metrics' {"name", "value"} shape (the O-RU bridge
        prefixes names with "ru."), so PM jobs allowlist them like any other
        metric. The M-plane session runs its notification handlers on a
        worker thread while the push queue belongs to the event loop, so the
        samples are handed to the loop run_pusher bound; before a loop is
        bound (no pusher running) they are streamed inline, where nothing
        consumes the queue concurrently.
        """
        if not metrics:
            return
        if self._loop is None or self._loop.is_closed():
            self._stream_ru_metrics(metrics)
            return
        try:
            self._loop.call_soon_threadsafe(self._stream_ru_metrics, metrics)
        except RuntimeError:  # the loop closed underneath us: shutting down, the samples are moot
            logging.debug("Dropping %d O-RU samples: event loop closed", len(metrics))

    def _stream_ru_metrics(self, metrics: list) -> None:
        active_jobs = self._active_jobs()
        if active_jobs:
            self._stream_metrics(metrics, active_jobs)

    def _stream_metrics(self, metrics: list, active_jobs: dict) -> None:
        """Filter metrics per active PM job and enqueue one envelope per job."""
        if not metrics:
            return
        for job_id, job in active_jobs.items():
            allowed = set(job.get("performanceMetrics") or [])
            job_metrics = [m for m in metrics if m.get("name") in allowed] if allowed else metrics
            if not job_metrics:
                continue
            if job.get("streamTarget"):
                envelope = self._build_envelope(job_id, job, job_metrics)
                self._state.pm_metrics_queue.put_nowait((job["streamTarget"], envelope))
            elif self._pm_files:
                self._pm_files.add_samples(job_id, job, job_metrics)

    def _build_envelope(self, job_id: str, job: dict, metrics: list) -> dict:
        """Construct a JSON envelope for the PM stream per the agreed schema."""
        return {
            "nfType": self._profile,
            "nfInstanceId": job.get("nf_instance_id", "unknown"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "granularityPeriod": job.get("granularityPeriod", 1),
            "measuredObject": {
                "objectType": job.get("nf_key", ""),
                "objectId": job.get("nf_instance_id", "unknown"),
                "plmnId": job.get("plmn_id", ""),
            },
            "metrics": metrics,
            "jobId": job_id,
        }

    async def run_pusher(self) -> None:
        """Consume pm_metrics_queue and POST each envelope to its configured streamTarget."""
        self.bind_loop()
        async with aiohttp.ClientSession() as session:
            while True:
                target, envelope = await self._state.pm_metrics_queue.get()
                try:
                    async with session.post(
                        target,
                        json=envelope,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.status >= 400:
                            logging.warning("PM push to %s returned %s", target, resp.status)
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logging.warning("PM push to %s failed: %s", target, e)
                finally:
                    self._state.pm_metrics_queue.task_done()
