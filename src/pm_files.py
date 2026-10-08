# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

"""File-based PM reporting: TS 32.435 files announced by VES notifyFileReady, fetched by the SMO over SFTP."""

import asyncio
import gzip
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean
from xml.etree import ElementTree as ET

from state import AppState

_MEAS_COLLEC_NS = "http://www.3gpp.org/ftp/specs/archive/32_series/32.435#measCollec"
_RETENTION = timedelta(hours=24)


def _next_boundary(now: datetime, period_s: int) -> datetime:
    """Return the first wall-clock multiple of period_s strictly after now."""
    return datetime.fromtimestamp((int(now.timestamp()) // period_s + 1) * period_s, timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def file_name(job_id: str, sender: str, begin: datetime, end: datetime) -> str:
    """TS 32.432 name, e.g. A20261006.1000+0000-1015+0000_<job>_<sender>.xml.gz."""
    end_fmt = "%H%M%z" if end.date() == begin.date() else "%Y%m%d.%H%M%z"
    return f"A{begin.strftime('%Y%m%d.%H%M%z')}-{end.strftime(end_fmt)}_{job_id}_{sender}.xml.gz"


# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals
def meas_collec_xml(job_id: str, job: dict, profile: str, begin: datetime, end: datetime, periods: list) -> bytes:
    """Render a TS 32.435 measCollecFile; periods is [(period end, {(ldn, name): value})]."""
    me_dn = f"ManagedElement={job['nf_instance_id']}"
    root = ET.Element("measCollecFile", xmlns=_MEAS_COLLEC_NS)
    header = ET.SubElement(root, "fileHeader", fileFormatVersion="32.435 V10.0")
    ET.SubElement(header, "fileSender", localDn=me_dn, elementType=profile)
    ET.SubElement(header, "measCollec", beginTime=_iso(begin))
    data = ET.SubElement(root, "measData")
    ET.SubElement(data, "managedElement", localDn=me_dn)
    for period_end, values in periods:
        info = ET.SubElement(data, "measInfo", measInfoId=job_id)
        ET.SubElement(info, "job", jobId=job_id)
        ET.SubElement(info, "granPeriod", duration=f"PT{job['granularityPeriod']}S", endTime=_iso(period_end))
        ET.SubElement(info, "repPeriod", duration=f"PT{job['fileReportingPeriod'] * 60}S")
        names = sorted({name for _, name in values})
        for p, name in enumerate(names, 1):
            ET.SubElement(info, "measType", p=str(p)).text = name
        for ldn in sorted({ldn for ldn, _ in values}):
            meas_value = ET.SubElement(info, "measValue", measObjLdn=ldn)
            for p, name in enumerate(names, 1):
                if (ldn, name) in values:
                    ET.SubElement(meas_value, "r", p=str(p)).text = f"{values[(ldn, name)]:.15g}"
    footer = ET.SubElement(root, "fileFooter")
    ET.SubElement(footer, "measCollec", endTime=_iso(end))
    xml: bytes = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
    return xml


class PmFiles:
    """Collects samples of file-reporting PM jobs, writes the files and announces them over VES."""

    def __init__(self, state: AppState, ves, pm_dir: str, sftp_url: str, profile: str = "gnb"):
        self._state = state
        self._ves = ves
        self._dir = Path(pm_dir)
        self._sftp_url = sftp_url.rstrip("/")
        self._profile = profile
        self._jobs: dict = {}  # job id -> collection state

    def add_samples(self, job_id: str, job: dict, metrics: list) -> None:
        """Buffer one WS report's metrics, already filtered for the job."""
        st = self._jobs.get(job_id)
        if st is None:
            return
        base = f"{job['nf_key']}={job['nf_id']}"
        for m in metrics:
            # ponytail: cellId is the gNB cell id, not the NRCellDU id
            ldn = base if m.get("cellId") is None else f"{base},NRCellDU={m['cellId']}"
            st["samples"].setdefault((ldn, m["name"]), []).append(m["value"])

    def _sync_jobs(self, now: datetime) -> dict:
        """Track the unlocked file-reporting jobs; a job whose periods change starts over."""
        active = {
            jid: job
            for jid, job in self._state.pm_jobs.items()
            if job.get("administrativeState") == "UNLOCKED" and job.get("fileReportingPeriod")
        }
        for jid in self._jobs.keys() - active.keys():
            del self._jobs[jid]
        for jid, job in active.items():
            key = (job["granularityPeriod"], job["fileReportingPeriod"])
            if jid in self._jobs and self._jobs[jid]["key"] == key:
                continue
            self._jobs[jid] = {
                "key": key,
                "samples": {},
                "periods": [],
                "begin": now,
                "period_end": _next_boundary(now, job["granularityPeriod"]),
                "file_end": _next_boundary(now, job["fileReportingPeriod"] * 60),
            }
        return active

    async def run(self) -> None:
        """Close granularity periods and files on their wall-clock boundaries."""
        self._dir.mkdir(parents=True, exist_ok=True)
        while True:
            now = datetime.now(timezone.utc)
            for jid, job in self._sync_jobs(now).items():
                st = self._jobs[jid]
                if now >= st["period_end"]:
                    if st["samples"]:
                        # ponytail: mean for all metrics, counters should be summed
                        values = {k: fmean(v) for k, v in st["samples"].items()}
                        st["periods"].append((st["period_end"], values))
                    st["samples"] = {}
                    st["period_end"] = _next_boundary(now, job["granularityPeriod"])
                if now >= st["file_end"]:
                    begin, end, periods = st["begin"], st["file_end"], st["periods"]
                    st["begin"], st["periods"] = end, []
                    st["file_end"] = _next_boundary(now, job["fileReportingPeriod"] * 60)
                    if periods:
                        await asyncio.to_thread(self._publish, jid, job, begin, end, periods)
                    else:
                        logging.debug("PM job %s: no samples between %s and %s, no file", jid, begin, end)
            await asyncio.sleep(1)

    def _publish(self, job_id: str, job: dict, begin: datetime, end: datetime, periods: list) -> None:
        """Write the file, announce it and drop expired files."""
        name = file_name(job_id, job["nf_instance_id"], begin, end)
        path = self._dir / name
        path.write_bytes(gzip.compress(meas_collec_xml(job_id, job, self._profile, begin, end, periods)))
        ready = datetime.now(timezone.utc)
        logging.info("PM job %s: wrote %s", job_id, path)
        self._ves.send_file_ready(
            me_id=job["nf_instance_id"],
            file_location=f"{self._sftp_url}/{name}",
            file_size=path.stat().st_size,
            ready_time=_iso(ready),
            expiration_time=_iso(ready + _RETENTION),
        )
        cutoff = (ready - _RETENTION).timestamp()
        for old in self._dir.glob("A*.xml.gz"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
