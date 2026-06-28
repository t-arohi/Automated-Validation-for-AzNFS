"""Phase 2 orchestration: validate AzNFS coverage against PMC **prod**.

Prod-only design (no PMC API, no tux-dev, no ADO build). For each image handed
over by Phase 1 the orchestrator walks three checks built straight on the public
``packages.microsoft.com`` version-indexed layout:

    Gate 1  repo exists?      GET /<distro>/<version>/prod/ returns 200
              no  -> DB known_unsupported  (reason: "repo is missing")
    Gate 2  package exists?   the aznfs dir lists a 0.3.x build for this arch
              no  -> DB pending_publish    (reason: publish manually; retried next run)
    Gate 3  validation needed?  numeric-latest 0.3.x prod version p  vs  DB last_validated_version
              no  (p == v_last) -> DB known_supported  (trusted)
              yes (first time, or p > v_last) -> emit LISA job + DB pending_validation

Phase 2 sends EXACTLY ONE e-mail per run: the end-of-run summary, which lists
every distro and -- for the failing ones -- the reason. No per-distro mail is
sent. External effects (prod client, DB, notifier) stay injectable so the flow
is easy to unit-test and to wire into the CLI/workflow layer (see ``run.py``).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Protocol

from . import pmc_packages
import requests

logger = logging.getLogger(__name__)

# Validation states written back to the DB ``validated`` column (mirror db_manager).
KNOWN_SUPPORTED = "known_supported"
KNOWN_UNSUPPORTED = "known_unsupported"
PENDING_PUBLISH = "pending_publish"
PENDING_VALIDATION = "pending_validation"


class ProdLike(Protocol):
    """The PMC-prod read surface the gates need (see pmc_packages.ProdPackageIndex)."""
    def resolve_repo(self, distro: str, candidates: list[str], family: str = "") -> str | None: ...
    def list_packages(self, distro: str, version: str, family: str) -> list[str]: ...


class DbLike(Protocol):
    def set_validation_state(self, identity: tuple[str, str, str, str, str], state: str) -> None: ...


class NotifierLike(Protocol):
    """Phase 2 emits a single end-of-run summary; there are no per-distro mails.

    Each bucket is a list of small dicts so the summary e-mail renders one table
    per outcome, one column per field:
      * to_phase3       -> {"label", "arch", "url"}
      * trusted         -> {"label", "arch"}
      * pending_publish -> {"label", "arch", "reason"}
      * unsupported     -> {"label", "arch", "reason"}
    ``errors`` stays a list of ``(label, reason)`` tuples.
    """
    def notify_summary(
        self,
        processed: int,
        to_phase3: list[dict],
        trusted: list[dict],
        pending_publish: list[dict],
        unsupported: list[dict],
        errors: list[tuple[str, str]],
    ) -> None: ...


@dataclass
class Phase2Result:
    outcome: str  # known_unsupported | pending_publish | trusted | to_phase3
    reason: str = ""
    lisa_job: dict | None = None


@dataclass
class GateResult:
    passed: bool
    reason: str = ""
    details: str = ""
    segment: str | None = None
    resolved_version: str | None = None


def _identity(entry: dict) -> tuple[str, str, str, str, str]:
    return (
        entry.get("publisher", ""),
        entry.get("image") or entry.get("offer") or "",
        entry.get("sku", ""),
        entry.get("region", ""),
        entry.get("architecture") or entry.get("arch") or "",
    )


_AZNFS_PACKAGES_CSV_URL = (
    "https://raw.githubusercontent.com/Azure/AZNFS-mount/main/packages.csv"
)
_SUPPORTED_UBUNTU = {"18.04", "20.04", "22.04", "24.04", "26.04"}
_SUPPORTED_CENTOS = {"7", "8"}
_SUPPORTED_RHEL = {"7", "8", "9", "10"}
_SUPPORTED_ROCKY = {"8", "9"}
_SUPPORTED_SLES = {"15", "16"}


def _major_minor(label: str) -> tuple[str, str]:
    m = re.search(r"(10|\d+)(?:\.(\d+))?", label)
    if not m:
        return "", ""
    return m.group(1), m.group(2) or ""


def _is_aznfs_supported_distro(label: str) -> bool:
    s = (label or "").strip().lower()
    major, minor = _major_minor(s)

    if "ubuntu" in s:
        ver = f"{major}.{minor}" if major and minor else ""
        return ver in _SUPPORTED_UBUNTU
    if "centos" in s:
        return major in _SUPPORTED_CENTOS
    if "rhel" in s or "redhat" in s or "red hat" in s:
        return major in _SUPPORTED_RHEL
    if "rocky" in s:
        return major in _SUPPORTED_ROCKY
    if "sles" in s or "suse" in s:
        return major in _SUPPORTED_SLES
    return False


def _packages_csv_mentions_distro(label: str) -> bool:
    """Best-effort check whether AZNFS-mount/packages.csv has this distro family."""
    tokens = []
    s = (label or "").strip().lower()
    major, minor = _major_minor(s)

    if "ubuntu" in s and major and minor:
        tokens.extend([f"ubuntu {major}.{minor}", f"ubuntu-{major}.{minor}"])
    elif "rhel" in s and major:
        tokens.extend([f"rhel {major}", f"rhel-{major}", f"redhat {major}"])
    elif "centos" in s and major:
        tokens.extend([f"centos {major}", f"centos-{major}"])
    elif "rocky" in s and major:
        tokens.extend([f"rocky {major}", f"rocky-{major}"])
    elif "sles" in s and major:
        tokens.extend([f"sles {major}", f"sles-{major}"])
    elif "debian" in s and major:
        tokens.extend([f"debian {major}", f"debian-{major}"])
    elif "azure linux" in s:
        tokens.extend(["azure linux", "azurelinux"])
    elif "mariner" in s:
        tokens.extend(["cbl-mariner", "mariner"])

    if not tokens:
        return False

    try:
        resp = requests.get(_AZNFS_PACKAGES_CSV_URL, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return False

    text = resp.text.lower()
    if any(tok in text for tok in tokens):
        return True

    # Fallback to CSV cell scan in case formatting changes.
    reader = csv.reader(io.StringIO(resp.text))
    for row in reader:
        row_txt = " ".join(c.strip().lower() for c in row)
        if any(tok in row_txt for tok in tokens):
            return True
    return False


# ---------------------------------------------------------------------------
# Gate 1: does a prod repo exist for this distro release?
# ---------------------------------------------------------------------------
def gate1_repo_exists(entry: dict, prod: ProdLike) -> GateResult:
    """A PMC prod pocket exists for this image's distro release.

    Resolves the ``<distro>`` segment + ``<version>`` candidates from the image's
    ``distro_label`` (no codename map) and probes ``/<distro>/<version>/prod/``.
    """
    label = entry.get("distro_label", "")
    family = entry.get("family") or ""
    segment = pmc_packages.distro_segment(label, entry.get("publisher", ""))
    if not segment:
        return GateResult(False, "unmapped distro", details=label or entry.get("publisher", ""))

    candidates = pmc_packages.version_candidates(label, entry.get("version", ""))
    if not candidates:
        return GateResult(False, "unparseable version", details=f"{label!r}")

    resolved = prod.resolve_repo(segment, candidates, family)
    if not resolved:
        return GateResult(False, "prod repo missing", details=f"{segment} {candidates}")
    return GateResult(True, segment=segment, resolved_version=resolved)


# ---------------------------------------------------------------------------
# LISA job (Phase 3 hand-off)
# ---------------------------------------------------------------------------
def _make_lisa_job(entry: dict, distro: str, version: str, family: str,
                   package_filename: str, aznfs_version: str) -> dict:
    """Assemble the Phase 3 LISA job for a prod-published package needing validation.

    The field names match Phase 3's ``LisaJob`` dataclass EXACTLY so Phase 3's
    ``load_jobs`` consumes this artifact directly (it keeps only known fields):
    ``publisher / image / sku / version / region / arch`` identify the
    marketplace image + DB row, and ``aznfs_package_url / aznfs_version`` are the
    published package Phase 3 installs and asserts. ``distro_label`` is carried
    through for human-readable reporting.
    """
    download_url = pmc_packages.aznfs_dir_url(distro, version, family) + package_filename
    return {
        "publisher": entry.get("publisher"),
        "image": entry.get("image") or entry.get("offer"),
        "sku": entry.get("sku"),
        "version": entry.get("version"),
        "region": entry.get("region"),
        "arch": entry.get("architecture") or entry.get("arch"),
        "distro_label": entry.get("distro_label"),
        "aznfs_package_url": download_url,
        "aznfs_version": aznfs_version,
    }


def write_lisa_jobs(jobs: list[dict], path: str) -> None:
    """Persist the run's LISA jobs as the Phase 3 hand-off artifact."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(jobs, fh, indent=2)
    logger.info("Wrote %d LISA job(s) -> %s", len(jobs), path)


# ---------------------------------------------------------------------------
# Per-image flow
# ---------------------------------------------------------------------------
def process_entry(entry: dict, prod: ProdLike, db: DbLike) -> Phase2Result:
    """Run the three prod checks for one image and apply the DB side-effect.

    Returns the per-image outcome + reason; the caller (:func:`run_phase2`)
    rolls every result into the single end-of-run summary e-mail. No per-distro
    notification is sent here.
    """
    ident = _identity(entry)
    family = entry.get("family") or ""
    arch = (entry.get("architecture") or entry.get("arch") or "").lower()

    # Gate 1: prod repo exists?
    g1 = gate1_repo_exists(entry, prod)
    if not g1.passed:
        db.set_validation_state(ident, KNOWN_UNSUPPORTED)
        return Phase2Result("known_unsupported", reason="prod repo is missing")

    distro, version = g1.segment, g1.resolved_version

    # Gate 2: is an aznfs package for this arch published in the tracked 0.3.x series?
    want_arch = pmc_packages.normalize_arch(arch, family)
    files = prod.list_packages(distro, version, family)
    arch_files = [
        f for f in files
        if pmc_packages.file_arch(f, family) == want_arch
        and pmc_packages.in_series(pmc_packages.version_from_filename(f))
    ]
    if not arch_files:
        label = entry.get("distro_label", "")
        # (a) the distro is outside the AzNFS support matrix -> terminal.
        if not _is_aznfs_supported_distro(label):
            db.set_validation_state(ident, KNOWN_UNSUPPORTED)
            return Phase2Result(
                "known_unsupported",
                reason="repo is found but packages are not found because distro is not supported by AzNFS",
            )
        # (b) supported distro already listed in AZNFS-mount/packages.csv -> the
        # csv does not need a change; a human just needs to publish the package.
        if _packages_csv_mentions_distro(label):
            db.set_validation_state(ident, PENDING_PUBLISH)
            return Phase2Result(
                "pending_publish",
                reason="no AzNFS packages found on prod and packages.csv does not "
                "require modification; publish packages manually and re-invoke Phase 2",
            )
        # (c) supported distro MISSING from packages.csv -> needs a csv/code
        # change first; mark known_unsupported until that branch is built.
        db.set_validation_state(ident, KNOWN_UNSUPPORTED)
        return Phase2Result(
            "known_unsupported",
            reason="team must update packages.csv + push branch + re-invoke Phase 2 with the new branch",
        )

    # Gate 3: validation needed? Numeric-latest 0.3.x prod version vs what Phase 3 last validated.
    best = max(arch_files, key=lambda f: pmc_packages.version_tuple(pmc_packages.version_from_filename(f)))
    p = pmc_packages.version_from_filename(best)
    v_last = (entry.get("last_validated_version") or "").strip()

    needs_validation = (not v_last) or (
        pmc_packages.version_tuple(p) > pmc_packages.version_tuple(v_last)
    )
    if not needs_validation:
        db.set_validation_state(ident, KNOWN_SUPPORTED)
        return Phase2Result("trusted", reason=f"already validated on prod (v{p})")

    lisa_job = _make_lisa_job(entry, distro, version, family, best, p)
    db.set_validation_state(ident, PENDING_VALIDATION)
    reason = f"validate v{p}" + (f" (was v{v_last})" if v_last else " (first validation)")
    return Phase2Result("to_phase3", reason=reason, lisa_job=lisa_job)


def _dedup_jobs_by_url(jobs: list[dict]) -> list[dict]:
    """One LISA job per distinct ``aznfs_package_url``, keeping the newest image.

    Many marketplace SKUs of the same OS release (e.g. CentOS 7.3 .. 7.9, or
    RHEL 9.0 .. 9.8) all resolve to the SAME prod package URL (centos/7, rhel/9,
    ...). Phase 3 only needs to validate that package once per architecture, so
    collapse them to the entry with the latest marketplace ``version`` -- a
    deterministic pick that also validates the freshest image. The result is a
    list whose ``aznfs_package_url`` values are all distinct.
    """
    best: dict[str, dict] = {}
    for j in jobs:
        url = j.get("aznfs_package_url", "")
        cur = best.get(url)
        if cur is None or pmc_packages.version_tuple(j.get("version", "")) > pmc_packages.version_tuple(cur.get("version", "")):
            best[url] = j
    return sorted(best.values(), key=lambda j: (j.get("distro_label") or "", j.get("arch") or ""))


def _dedup_label_arch(rows: list[dict]) -> list[dict]:
    """Collapse rows to one per (distro_label, arch), keeping the first reason.

    A distro that shows up under many SKUs of the same release+architecture
    should appear ONCE in the summary; different architectures stay separate
    rows (that is why the tables carry an ``arch`` column).
    """
    seen: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r.get("label") or "", r.get("arch") or "")
        if key not in seen:
            seen[key] = r
    return sorted(seen.values(), key=lambda r: (r.get("label") or "", r.get("arch") or ""))


def run_phase2(
    entries: list[dict],
    prod: ProdLike,
    db: DbLike,
    notifier: NotifierLike,
    lisa_jobs_path: str | None = None,
) -> list[dict]:
    """Process every image, write the Phase 3 hand-off, and send the single summary.

    The DB side-effect runs once per input image (each SKU row gets its state),
    but the LISA hand-off and the summary tables are de-duplicated: the jobs to
    one entry per distinct prod package URL (latest image wins) and the report
    buckets to one row per (distro_label, architecture).
    """
    raw_jobs: list[dict] = []
    unsupported: list[dict] = []
    pending_publish: list[dict] = []
    trusted: list[dict] = []
    errors: list[tuple[str, str]] = []

    for e in entries:
        label = e.get("distro_label", "?")
        arch = e.get("architecture") or e.get("arch") or ""
        try:
            result = process_entry(e, prod, db)
        except Exception as exc:  # one image's failure never aborts the run
            logger.exception("Unexpected error processing %s", label)
            errors.append((label, f"orchestrator error (will retry next run): {exc}"))
            continue

        if result.outcome == "known_unsupported":
            unsupported.append({"label": label, "arch": arch, "reason": result.reason})
        elif result.outcome == "pending_publish":
            pending_publish.append({"label": label, "arch": arch, "reason": result.reason})
        elif result.outcome == "trusted":
            trusted.append({"label": label, "arch": arch})
        elif result.lisa_job:  # to_phase3
            raw_jobs.append(result.lisa_job)

    lisa_jobs = _dedup_jobs_by_url(raw_jobs)
    to_phase3 = [
        {"label": j.get("distro_label"), "arch": j.get("arch"), "url": j.get("aznfs_package_url")}
        for j in lisa_jobs
    ]
    trusted = _dedup_label_arch(trusted)
    pending_publish = _dedup_label_arch(pending_publish)
    unsupported = _dedup_label_arch(unsupported)

    if lisa_jobs_path:
        write_lisa_jobs(lisa_jobs, lisa_jobs_path)
    notifier.notify_summary(
        processed=len(entries),
        to_phase3=to_phase3,
        trusted=trusted,
        pending_publish=pending_publish,
        unsupported=unsupported,
        errors=errors,
    )
    logger.info(
        "Phase 2: %d processed | %d to-phase3 | %d trusted | %d pending-publish | %d known_unsupported | %d errors",
        len(entries), len(to_phase3), len(trusted), len(pending_publish), len(unsupported), len(errors),
    )
    return lisa_jobs

