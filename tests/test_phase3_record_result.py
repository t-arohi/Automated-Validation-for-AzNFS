from __future__ import annotations

import sqlite3

import pytest

from phase3.orchestrator import record_result
from phase3 import run_phase3


# ---------------------------------------------------------------------------
# load_jobs: keeps only LisaJob fields (drops Phase 2 extras / legacy `repo`)
# ---------------------------------------------------------------------------
def test_load_jobs_filters_unknown_fields(tmp_path):
    p = tmp_path / "jobs.json"
    p.write_text(
        '[{"publisher":"redhat","image":"rhel","sku":"9_5","version":"latest",'
        '"region":"eastus","arch":"x86_64","distro_label":"RHEL 9.5",'
        '"aznfs_package_url":"https://x/aznfs-0.3.458-1.x86_64.rpm",'
        '"aznfs_version":"0.3.458","repo":"legacy-should-be-dropped"}]'
    )
    jobs = record_result.load_jobs(str(p))
    assert len(jobs) == 1
    j = jobs[0]
    assert j.image == "rhel" and j.version == "latest" and j.arch == "x86_64"
    assert j.aznfs_package_url.endswith("aznfs-0.3.458-1.x86_64.rpm")
    assert not hasattr(j, "repo")  # dropped field never becomes an attribute


def test_image_key_is_the_five_key_identity():
    j = record_result.LisaJob(
        publisher="redhat", image="rhel", sku="9_5", version="latest",
        region="eastus", arch="x86_64",
    )
    assert j.image_key() == {
        "publisher": "redhat", "image": "rhel", "sku": "9_5",
        "region": "eastus", "architecture": "x86_64",
    }


# ---------------------------------------------------------------------------
# _record_validation: matches on the 5-key identity (publisher/image/sku/region/arch)
# ---------------------------------------------------------------------------
def _make_db(tmp_path):
    db = tmp_path / "marketplace.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE images (
            publisher TEXT, image TEXT, sku TEXT, version TEXT, region TEXT,
            architecture TEXT, validated TEXT, last_modified TEXT, last_validated TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO images VALUES (?,?,?,?,?,?,?,?,?)",
        ("redhat", "rhel", "9_5", "9.5.20240101", "eastus", "x86_64",
         "pending_validation", "t0", None),
    )
    conn.commit()
    conn.close()
    return db


def test_record_validation_updates_matching_row(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    record_result._record_validation(
        {"publisher": "redhat", "image": "rhel", "sku": "9_5",
         "region": "eastus", "architecture": "x86_64"},
        "known_supported",
    )

    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT validated, last_validated FROM images"
    ).fetchone()
    conn.close()
    assert row[0] == "known_supported"
    assert row[1] is not None  # last_validated stamped


# ---------------------------------------------------------------------------
# run(): one summary e-mail; pass -> supported, fail -> unsupported + reason
# ---------------------------------------------------------------------------
def test_run_sends_single_summary_with_reasons(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    monkeypatch.setattr(record_result.config, "DB_PATH", str(db))
    monkeypatch.setattr(record_result.config, "PHASE3_SCHEMA_PATH", "/nonexistent.sql")

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(record_result, "_notify", lambda s, b: sent.append((s, b)))

    jobs = [
        record_result.LisaJob(
            publisher="redhat", image="rhel", sku="9_5", version="latest",
            region="eastus", arch="x86_64", distro_label="RHEL 9.5",
            lisa_passed=True,
        ),
        record_result.LisaJob(
            publisher="suse", image="sles", sku="15-sp5", version="latest",
            region="eastus", arch="x86_64", distro_label="SLES 15.5",
            lisa_passed=False, failure_reason="[Tier 4: mount] failed to mount ... via aznfs",
        ),
    ]
    summary = record_result.run(jobs)

    assert summary == {"known_supported": 1, "known_unsupported": 1}
    assert len(sent) == 1  # exactly ONE e-mail for the whole run
    subject, body = sent[0]
    assert "1 supported, 1 unsupported" in subject
    # Pass line: distro + DB state transition.
    assert "validation done for distro RHEL 9.5" in body
    assert "validation_state changed to known_supported in DB" in body
    # Fail line: distro (quoted) + failing tier reason + DB state transition + URN/logs.
    assert 'validation fails for distro "SLES 15.5"' in body
    assert "[Tier 4: mount] failed to mount" in body
    assert "validation_state changed to known_unsupported in DB" in body
    assert "image URN:" in body
    assert "logs URL:" in body


# ---------------------------------------------------------------------------
# _parse_junit: extracts the failing tier from the failure message
# ---------------------------------------------------------------------------
def test_parse_junit_extracts_tier_reason(tmp_path):
    xml = tmp_path / "lisa.junit.xml"
    xml.write_text(
        '<testsuites><testsuite tests="3" failures="1" errors="0" skipped="0">'
        '<testcase name="verify_aznfs_install_lifecycle"/>'
        '<testcase name="verify_aznfs_nfs_functional">'
        '<failure message="[Tier 4: mount] failed to mount src via aznfs">trace</failure>'
        '</testcase>'
        '<testcase name="verify_aznfs_resilience"/>'
        '</testsuite></testsuites>'
    )
    total, failed, skipped, reason = run_phase3._parse_junit(xml)
    assert (total, failed, skipped) == (3, 1, 0)
    assert reason == "[Tier 4: mount] failed to mount src via aznfs"


def test_parse_junit_clean_pass_has_no_reason(tmp_path):
    xml = tmp_path / "lisa.junit.xml"
    xml.write_text(
        '<testsuite tests="3" failures="0" errors="0" skipped="0">'
        '<testcase name="verify_aznfs_install_lifecycle"/>'
        '</testsuite>'
    )
    total, failed, skipped, reason = run_phase3._parse_junit(xml)
    assert (total, failed, skipped, reason) == (3, 0, 0, "")
