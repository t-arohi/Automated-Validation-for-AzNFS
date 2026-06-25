from __future__ import annotations

import json
from dataclasses import dataclass, field

from src.phase2.orchestrator import (
    KNOWN_SUPPORTED,
    KNOWN_UNSUPPORTED,
    PENDING_PUBLISH,
    PENDING_VALIDATION,
    process_entry,
    run_phase2,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeProd:
    """Models PMC prod: which pockets exist and which aznfs files they list."""

    def __init__(self, repos=None, packages=None, raise_on_resolve=False):
        self.repos = repos or {}                 # distro -> {version segments}
        self.packages = packages or {}           # (distro, version) -> [filenames]
        self.raise_on_resolve = raise_on_resolve

    def resolve_repo(self, distro, candidates, family=""):
        if self.raise_on_resolve:
            raise RuntimeError("prod down")
        present = self.repos.get(distro, set())
        for v in candidates:
            if v in present:
                return v
        return None

    def list_packages(self, distro, version, family):
        return list(self.packages.get((distro, version), []))


@dataclass
class FakeDb:
    updates: list[tuple] = field(default_factory=list)

    def set_validation_state(self, identity, state):
        self.updates.append((identity, state))


@dataclass
class FakeNotifier:
    """Phase 2 sends exactly one mail per run -- the summary. Capture it."""

    summaries: list[dict] = field(default_factory=list)

    def notify_summary(self, processed, unsupported, pending_publish, trusted, to_phase3, errors):
        self.summaries.append({
            "processed": processed,
            "unsupported": unsupported,
            "pending_publish": pending_publish,
            "trusted": trusted,
            "to_phase3": to_phase3,
            "errors": errors,
        })


def entry(**kw):
    base = {
        "publisher": "Canonical",
        "image": "ubuntu-22_04-lts",
        "sku": "server",
        "version": "22.04.202506",
        "region": "eastus",
        "architecture": "x86_64",
        "family": "apt",
        "distro_label": "Ubuntu 22.04",
        "last_validated_version": "",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Gate 1 -> unsupported  (process_entry returns the outcome + reason; no mail)
# ---------------------------------------------------------------------------
def test_no_prod_repo_marks_unsupported():
    prod = FakeProd(repos={})  # no pocket exists
    db = FakeDb()

    r = process_entry(entry(), prod, db)

    assert r.outcome == "unsupported"
    assert r.reason == "repo is missing"
    assert db.updates[-1] == (
        ("Canonical", "ubuntu-22_04-lts", "server", "eastus", "x86_64"),
        KNOWN_UNSUPPORTED,
    )


# ---------------------------------------------------------------------------
# Gate 2 -> pending_publish
# ---------------------------------------------------------------------------
def test_repo_exists_but_no_aznfs_marks_pending_publish():
    # Debian real case: /debian/11/prod/ exists but no aznfs published.
    prod = FakeProd(repos={"debian": {"11"}}, packages={})
    db = FakeDb()

    r = process_entry(entry(publisher="Debian", distro_label="Debian 11"), prod, db)

    assert r.outcome == "pending_publish"
    assert "publish" in r.reason.lower()
    assert db.updates[-1][1] == PENDING_PUBLISH


def test_aznfs_present_for_other_arch_only_is_pending_publish():
    # Only arm64 published; the x86_64 image is still uncovered.
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.2_arm64.deb"]},
    )
    db = FakeDb()

    r = process_entry(entry(architecture="x86_64"), prod, db)

    assert r.outcome == "pending_publish"
    assert db.updates[-1][1] == PENDING_PUBLISH


# ---------------------------------------------------------------------------
# Gate 3 -> trusted (no validation needed)
# ---------------------------------------------------------------------------
def test_latest_already_validated_marks_trusted():
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.2_amd64.deb"]},
    )
    db = FakeDb()

    r = process_entry(entry(last_validated_version="0.3.2"), prod, db)

    assert r.outcome == "trusted"
    assert r.lisa_job is None
    assert "0.3.2" in r.reason
    assert db.updates[-1][1] == KNOWN_SUPPORTED


# ---------------------------------------------------------------------------
# Gate 3 -> to_phase3 (validation needed)
# ---------------------------------------------------------------------------
def test_first_validation_emits_lisa_job():
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.2_amd64.deb"]},
    )
    db = FakeDb()

    r = process_entry(entry(last_validated_version=""), prod, db)

    assert r.outcome == "to_phase3"
    assert r.lisa_job is not None
    job = r.lisa_job
    assert job["aznfs_version"] == "0.3.2"
    assert job["variant_name"] == "0.3.2"
    assert job["repository"] == "ubuntu/22.04/prod"
    assert job["package_filename"] == "aznfs_0.3.2_amd64.deb"
    assert job["download_url"].startswith("https://")
    assert job["download_url"].endswith("/ubuntu/22.04/prod/pool/main/a/aznfs/aznfs_0.3.2_amd64.deb")
    assert "distro_info" in job
    assert db.updates[-1][1] == PENDING_VALIDATION


def test_newer_prod_version_than_db_needs_validation_picks_numeric_max():
    # 0.3.18 must beat 0.3.9 (numeric, not lexical) and beat the DB's 0.3.9.
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): [
            "aznfs_0.3.9_amd64.deb",
            "aznfs_0.3.18_amd64.deb",
        ]},
    )
    db = FakeDb()

    r = process_entry(entry(last_validated_version="0.3.9"), prod, db)

    assert r.outcome == "to_phase3"
    assert r.lisa_job["aznfs_version"] == "0.3.18"
    assert db.updates[-1][1] == PENDING_VALIDATION


def test_series_filter_ignores_non_0_3_lineages():
    # Prod carries 0.3.x AND newer 1.x/2.x/3.x lineages; only 0.3.x is tracked,
    # so the latest must be 0.3.458 (numeric-max within the series), never 3.0.18.
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): [
            "aznfs_0.3.46_amd64.deb",
            "aznfs_0.3.458_amd64.deb",
            "aznfs_1.0.4_amd64.deb",
            "aznfs_2.1.3_amd64.deb",
            "aznfs_3.0.18_amd64.deb",
        ]},
    )
    db = FakeDb()

    r = process_entry(entry(last_validated_version="0.3.46"), prod, db)

    assert r.outcome == "to_phase3"
    assert r.lisa_job["aznfs_version"] == "0.3.458"
    assert r.lisa_job["package_filename"] == "aznfs_0.3.458_amd64.deb"


def test_only_non_series_builds_is_pending_publish():
    # Repo exists and aznfs is published, but only on the untracked 3.0.x/1.x line.
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): [
            "aznfs_3.0.18_amd64.deb",
            "aznfs_1.0.4_amd64.deb",
        ]},
    )
    db = FakeDb()

    r = process_entry(entry(architecture="x86_64"), prod, db)

    assert r.outcome == "pending_publish"
    assert db.updates[-1][1] == PENDING_PUBLISH


def test_yum_minor_fallback_and_arch_mapping_to_phase3():
    # RHEL 9.8 -> /rhel/9/; x86_64 image maps to the x86_64 rpm.
    prod = FakeProd(
        repos={"rhel": {"9"}},
        packages={("rhel", "9"): [
            "aznfs-0.3.2-1.x86_64.rpm",
            "aznfs-0.3.2-1.aarch64.rpm",
        ]},
    )
    db = FakeDb()

    r = process_entry(
        entry(publisher="RedHat", distro_label="RHEL 9.8", family="yum",
              architecture="x86_64", image="RHEL", sku="9-lvm"),
        prod, db,
    )

    assert r.outcome == "to_phase3"
    assert r.lisa_job["package_filename"] == "aznfs-0.3.2-1.x86_64.rpm"
    assert r.lisa_job["repository"] == "rhel/9/prod"
    assert r.lisa_job["download_url"].endswith("/rhel/9/prod/Packages/a/aznfs-0.3.2-1.x86_64.rpm")


# ---------------------------------------------------------------------------
# run_phase2 aggregation -> exactly one summary mail with every distro + reason
# ---------------------------------------------------------------------------
def test_run_phase2_buckets_writes_jobs_and_single_summary(tmp_path):
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}, "debian": {"11"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.2_amd64.deb"]},
    )
    db, notifier = FakeDb(), FakeNotifier()
    out = tmp_path / "lisa_jobs.json"

    entries = [
        entry(last_validated_version=""),                                   # to_phase3
        entry(distro_label="Ubuntu 22.04", last_validated_version="0.3.2",
              sku="other"),                                                  # trusted
        entry(publisher="Debian", distro_label="Debian 11", sku="deb"),     # pending_publish
        entry(publisher="BellLabs", distro_label="Plan9 4", sku="x"),       # unsupported
    ]

    jobs = run_phase2(entries, prod, db, notifier, lisa_jobs_path=str(out))

    assert len(jobs) == 1
    # Exactly one mail (the summary) for the whole run.
    assert len(notifier.summaries) == 1
    s = notifier.summaries[-1]
    assert s["processed"] == 4
    assert s["to_phase3"] == ["Ubuntu 22.04"]
    assert s["trusted"] == ["Ubuntu 22.04"]
    # Failing buckets carry (distro_label, reason) so the mail explains each one.
    assert [lbl for lbl, _ in s["pending_publish"]] == ["Debian 11"]
    assert "publish" in s["pending_publish"][0][1].lower()
    assert s["unsupported"] == [("Plan9 4", "repo is missing")]
    assert s["errors"] == []

    written = json.loads(out.read_text())
    assert written[0]["package_filename"] == "aznfs_0.3.2_amd64.deb"


def test_run_phase2_swallows_per_entry_errors_into_summary():
    prod = FakeProd(raise_on_resolve=True)
    db, notifier = FakeDb(), FakeNotifier()

    jobs = run_phase2([entry()], prod, db, notifier, lisa_jobs_path=None)

    assert jobs == []
    # Still exactly one summary; the failed entry shows up in the errors bucket.
    assert len(notifier.summaries) == 1
    s = notifier.summaries[-1]
    assert s["processed"] == 1
    assert s["errors"] and "error" in s["errors"][0][1].lower()
