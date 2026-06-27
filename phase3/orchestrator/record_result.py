"""
Phase 3 - record the validation verdict (DB update + one summary e-mail).

Runs AFTER LISA validation. Phase 3 is LISA testing ONLY: there is no PMC prod
query here (Phase 2 already owns the "is it on prod?" check). For each distro we
simply record the outcome and, at the end of the run, send a SINGLE summary
e-mail listing every distro and -- for the failures -- which tier/step failed.

  LISA passed -> validated = known_supported
  LISA failed -> validated = known_unsupported   (terminal; a human resets the
                 DB row to 'unknown' if a transient/flaky failure buried a good
                 distro -- there is no automatic retry).

The DB row is matched on the SAME 5-key identity Phase 1/Phase 2 use
(publisher, image, sku, region, architecture) so the update actually lands.
"""

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from . import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job model (one entry per distro handed over from Phase 2 / fed to LISA)
# ---------------------------------------------------------------------------
@dataclass
class LisaJob:
    """A single distro job. Field names match Phase 2's lisa_jobs.json exactly,
    so ``load_jobs`` consumes that artifact directly.

    The image identity fields (publisher/image/sku/region/arch) match the Phase 1
    ``images`` table so the DB row can be updated. ``version`` is the marketplace
    image version used to build the URN for LISA. ``aznfs_package_url`` is the
    published package Phase 3 installs; ``aznfs_version`` is asserted in Tier 1/3.
    ``failure_reason`` is filled by the driver (the failing tier) on a LISA fail.
    """

    publisher: str
    image: str
    sku: str
    version: str
    region: str
    arch: str = "x86_64"
    aznfs_version: str = ""
    aznfs_package_url: str = ""
    lisa_passed: bool = False
    distro_label: str = ""
    failure_reason: str = ""

    def image_key(self) -> Dict[str, str]:
        # The 5-key identity Phase 1/Phase 2 use (NOT version; WITH architecture).
        return {
            "publisher": self.publisher,
            "image": self.image,
            "sku": self.sku,
            "region": self.region,
            "architecture": self.arch,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# SQLite update (extends the Phase 1 images table with last_validated)
# ---------------------------------------------------------------------------
def _ensure_phase3_columns(conn: sqlite3.Connection) -> None:
    """Add the last_validated column if it does not exist (idempotent)."""
    try:
        conn.execute("ALTER TABLE images ADD COLUMN last_validated TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # duplicate column name - already added in a prior run


def _record_validation(image_key: Dict[str, str], validated: str) -> None:
    """Set validated + last_validated on the matching images row.

    Matches on (publisher, image, sku, region, architecture) - the SAME identity
    Phase 1/Phase 2 use - so the row is found (the earlier version matched on
    `version`, which is 'latest' in the job and never equals the stored
    marketplace version, so it updated zero rows).
    """
    now = _now_iso()
    conn = sqlite3.connect(config.DB_PATH)
    try:
        _ensure_phase3_columns(conn)
        cur = conn.execute(
            """
            UPDATE images
               SET validated      = ?,
                   last_validated = ?,
                   last_modified  = ?
             WHERE publisher    = ?
               AND image        = ?
               AND sku          = ?
               AND region       = ?
               AND architecture = ?
            """,
            (
                validated,
                now,
                now,
                image_key["publisher"],
                image_key["image"],
                image_key["sku"],
                image_key["region"],
                image_key["architecture"],
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            logger.warning("no images row matched %s (state=%s)", image_key, validated)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Notifications - reuse the Phase 1 ACS notifier (one summary per run)
# ---------------------------------------------------------------------------
def _notify(subject: str, body: str) -> None:
    """Send via the Phase 1 ACS notifier when importable; otherwise just log.

    The workflow puts ``scripts/`` on PYTHONPATH (same as Phase 2), so
    ``import notifier`` resolves to scripts/notifier.py and uses its ACS path.
    Lazy import keeps this module testable without Phase 1 on the path.
    """
    try:
        import notifier  # type: ignore
    except ModuleNotFoundError:
        try:
            from scripts import notifier  # type: ignore
        except ModuleNotFoundError:
            logger.info("NOTIFY (no notifier module):\n%s\n%s", subject, body)
            return
    notifier.notify(subject, body)


def _send_summary(
    processed: int,
    supported: List[str],
    unsupported: List[Tuple[str, str]],
) -> None:
    """The single end-of-run e-mail: every distro + the failing tier for fails."""
    subject = (
        f"[AzNFS Phase 3] validation summary: {len(supported)} supported, "
        f"{len(unsupported)} unsupported (of {processed})"
    )
    lines = [f"Phase 3 validated {processed} distro(s) with LISA.", ""]
    lines.append(f"Supported (known_supported) ({len(supported)}):")
    if supported:
        lines += [f"  - {lbl}" for lbl in supported]
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append(f"Unsupported (known_unsupported) ({len(unsupported)}):")
    if unsupported:
        lines += [f"  - {lbl}: {reason}" for lbl, reason in unsupported]
    else:
        lines.append("  (none)")
    _notify(subject, "\n".join(lines))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def process_job(job: LisaJob) -> Tuple[str, str]:
    """Record one distro's verdict. Returns (validated_state, failure_reason)."""
    if job.lisa_passed:
        _record_validation(job.image_key(), "known_supported")
        return "known_supported", ""
    _record_validation(job.image_key(), "known_unsupported")
    return "known_unsupported", job.failure_reason


def run(jobs: List[LisaJob]) -> Dict[str, int]:
    """Record every job's verdict and send ONE summary e-mail. Returns counts."""
    supported: List[str] = []
    unsupported: List[Tuple[str, str]] = []
    for job in jobs:
        label = job.distro_label or f"{job.publisher}/{job.image}/{job.sku}"
        state, reason = process_job(job)
        if state == "known_supported":
            supported.append(label)
        else:
            unsupported.append((label, reason or "validation failed"))
    _send_summary(len(jobs), supported, unsupported)
    logger.info(
        "Phase 3: %d supported, %d unsupported (of %d)",
        len(supported), len(unsupported), len(jobs),
    )
    return {
        "known_supported": len(supported),
        "known_unsupported": len(unsupported),
    }


def load_jobs(path: str) -> List[LisaJob]:
    """Load LISA jobs from a JSON file (Phase 2's lisa_jobs.json)."""
    with open(path, "r") as fh:
        raw = json.load(fh)
    known = {f for f in LisaJob.__dataclass_fields__}  # noqa: B019
    return [LisaJob(**{k: v for k, v in item.items() if k in known}) for item in raw]


def main() -> None:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Phase 3: record LISA verdicts in the DB + send a summary."
    )
    parser.add_argument(
        "jobs_json", help="JSON list of LISA job results (with lisa_passed set)."
    )
    args = parser.parse_args()
    run(load_jobs(args.jobs_json))


if __name__ == "__main__":
    main()

