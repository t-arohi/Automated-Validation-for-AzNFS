from __future__ import annotations

import json

import pytest

from src.phase2 import run


# ---------------------------------------------------------------------------
# Phase 1 module fakes (stand in for scripts/notifier.py + scripts/db_manager.py)
# ---------------------------------------------------------------------------
class FakeNotifierMod:
    """Phase 2 calls only send_phase2_summary (one mail per run)."""

    def __init__(self):
        self.summaries = []

    def send_phase2_summary(self, processed, unsupported=None, pending_publish=None,
                            to_phase3=None, trusted=None, skipped=0, errors=None, recipients=None):
        self.summaries.append({
            "processed": processed,
            "unsupported": unsupported or [],
            "pending_publish": pending_publish or [],
            "to_phase3": to_phase3 or [],
            "trusted": trusted or [],
            "errors": errors or [],
        })


class FakeDbMod:
    def __init__(self, matched=True, records=None, pending=None):
        self.calls = []
        self.matched = matched
        self.records = records or {}     # identity tuple -> row dict
        self.pending = pending or []     # rows currently pending_publish

    def set_validation_state(self, db_path, identity, state, last_validated_version=None):
        self.calls.append((db_path, identity, state, last_validated_version))
        return self.matched

    def get_image_record(self, db_path, publisher, image, sku, region, architecture):
        return self.records.get((publisher, image, sku, region, architecture), {})

    def get_rows_by_state(self, db_path, state):
        return list(self.pending) if state == "pending_publish" else []


class FakeProd:
    def __init__(self, repos=None, packages=None):
        self.repos = repos or {}
        self.packages = packages or {}

    def resolve_repo(self, distro, candidates, family=""):
        present = self.repos.get(distro, set())
        for v in candidates:
            if v in present:
                return v
        return None

    def list_packages(self, distro, version, family):
        return list(self.packages.get((distro, version), []))


def _entry(**kw):
    base = {
        "publisher": "Canonical",
        "image": "ubuntu-22_04-lts",
        "sku": "server",
        "version": "22.04.202506",
        "region": "eastus",
        "architecture": "x86_64",
        "family": "apt",
        "distro_label": "Ubuntu 22.04",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Notifier adapter: the only call is notify_summary, passed straight through
# ---------------------------------------------------------------------------
def test_notifier_adapter_summary_passes_through_with_reasons():
    mod = FakeNotifierMod()
    ad = run.Phase1NotifierAdapter(mod)

    ad.notify_summary(
        processed=4,
        unsupported=[("Plan9 4", "repo is missing")],
        pending_publish=[("Debian 11", "no AzNFS packages are found (amd64); please publish manually then re-run Phase 2")],
        trusted=["RHEL 9"],
        to_phase3=["Ubuntu 22.04"],
        errors=[],
    )

    s = mod.summaries[-1]
    assert s["processed"] == 4
    assert s["unsupported"] == [("Plan9 4", "repo is missing")]
    assert s["pending_publish"][0][0] == "Debian 11"
    assert "publish" in s["pending_publish"][0][1].lower()
    assert s["trusted"] == ["RHEL 9"]
    assert s["to_phase3"] == ["Ubuntu 22.04"]
    assert s["errors"] == []


# ---------------------------------------------------------------------------
# DB adapter
# ---------------------------------------------------------------------------
def test_db_adapter_forwards_path_identity_state():
    mod = FakeDbMod()
    ad = run.Phase1DbAdapter(mod, "/tmp/marketplace.db")
    ident = ("Canonical", "ubuntu-22_04-lts", "server", "eastus", "x86_64")

    ad.set_validation_state(ident, "known_supported")

    assert mod.calls == [("/tmp/marketplace.db", ident, "known_supported", None)]


def test_db_adapter_warns_when_no_row(caplog):
    mod = FakeDbMod(matched=False)
    ad = run.Phase1DbAdapter(mod, "db")
    with caplog.at_level("WARNING"):
        ad.set_validation_state(("p", "i", "s", "r", "a"), "known_unsupported")
    assert "No DB row matched" in caplog.text


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------
def test_load_entries_rejects_non_list(tmp_path):
    p = tmp_path / "needs.json"
    p.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(ValueError):
        run.load_entries(str(p))


def test_load_entries_reads_list(tmp_path):
    p = tmp_path / "needs.json"
    p.write_text(json.dumps([{"distro_label": "Ubuntu 22.04"}]))
    assert run.load_entries(str(p)) == [{"distro_label": "Ubuntu 22.04"}]


# ---------------------------------------------------------------------------
# enrich_and_merge: DB last_validated_version + pending_publish re-entry
# ---------------------------------------------------------------------------
def test_enrich_adds_last_validated_version_from_db():
    ident = ("Canonical", "ubuntu-22_04-lts", "server", "eastus", "x86_64")
    db = FakeDbMod(records={ident: {"last_validated_version": "0.3.2"}})

    out = run.enrich_and_merge([_entry()], db, "db")

    assert out[0]["last_validated_version"] == "0.3.2"


def test_enrich_merges_pending_publish_rows_and_dedupes():
    e = _entry()
    dup_row = {**e, "last_validated_version": ""}        # same identity -> not duplicated
    extra_row = {
        "publisher": "Debian", "image": "debian-11", "sku": "d",
        "region": "eastus", "architecture": "x86_64",
        "family": "apt", "distro_label": "Debian 11", "last_validated_version": "",
    }
    db = FakeDbMod(pending=[dup_row, extra_row])

    out = run.enrich_and_merge([e], db, "db")

    assert len(out) == 2
    assert {r["distro_label"] for r in out} == {"Ubuntu 22.04", "Debian 11"}


def _ident(e):
    return (e["publisher"], e["image"], e["sku"], e["region"], e["architecture"])


def test_enrich_skips_in_flight_and_terminal_db_states():
    # A reused Phase 1 artifact still lists images Phase 2 already handled. The DB
    # state is authoritative, so an in-flight (pending_validation) or already-ruled
    # (known_supported/known_unsupported) image is NOT re-dispatched -- only the
    # fresh `unknown` survives. This is the idempotency / no-double-dispatch guard.
    e_inflight = _entry(sku="inflight")
    e_supported = _entry(sku="supported")
    e_unsupported = _entry(sku="unsupported")
    e_fresh = _entry(sku="fresh")
    db = FakeDbMod(records={
        _ident(e_inflight): {"validated": "pending_validation"},
        _ident(e_supported): {"validated": "known_supported", "last_validated_version": "0.3.458"},
        _ident(e_unsupported): {"validated": "known_unsupported"},
        _ident(e_fresh): {"validated": "unknown"},
    })

    out = run.enrich_and_merge([e_inflight, e_supported, e_unsupported, e_fresh], db, "db")

    assert [r["sku"] for r in out] == ["fresh"]


def test_enrich_keeps_pending_publish_artifact_entry():
    # An image whose DB state is pending_publish is NOT terminal -- it must keep
    # flowing so it re-checks prod for the (now hopefully published) package.
    e = _entry()
    db = FakeDbMod(records={_ident(e): {"validated": "pending_publish"}})

    out = run.enrich_and_merge([e], db, "db")

    assert len(out) == 1
    assert out[0]["sku"] == "server"


# ---------------------------------------------------------------------------
# End-to-end run() with injected fakes (no network, no Phase 1 modules)
# ---------------------------------------------------------------------------
def test_run_end_to_end_to_phase3_writes_lisa_jobs(tmp_path):
    notifier_mod = FakeNotifierMod()
    db_mod = FakeDbMod()
    out = tmp_path / "lisa_jobs.json"
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.2_amd64.deb"]},
    )

    jobs = run.run(
        entries=[_entry()],
        prod=prod,
        notifier_obj=run.Phase1NotifierAdapter(notifier_mod),
        db=run.Phase1DbAdapter(db_mod, "marketplace.db"),
        lisa_jobs_path=str(out),
    )

    assert len(jobs) == 1
    written = json.loads(out.read_text())
    assert written[0]["aznfs_package_url"].endswith("aznfs_0.3.2_amd64.deb")
    assert db_mod.calls[-1][2] == "pending_validation"
    # Exactly one mail: the summary, with this distro in the to_phase3 bucket.
    assert len(notifier_mod.summaries) == 1
    assert notifier_mod.summaries[-1]["to_phase3"] == ["Ubuntu 22.04"]


def test_run_end_to_end_trusted(tmp_path):
    notifier_mod = FakeNotifierMod()
    db_mod = FakeDbMod()
    out = tmp_path / "lisa_jobs.json"
    prod = FakeProd(
        repos={"ubuntu": {"22.04"}},
        packages={("ubuntu", "22.04"): ["aznfs_0.3.2_amd64.deb"]},
    )

    jobs = run.run(
        entries=[_entry(last_validated_version="0.3.2")],
        prod=prod,
        notifier_obj=run.Phase1NotifierAdapter(notifier_mod),
        db=run.Phase1DbAdapter(db_mod, "marketplace.db"),
        lisa_jobs_path=str(out),
    )

    assert jobs == []
    assert db_mod.calls[-1][2] == "known_supported"
    assert len(notifier_mod.summaries) == 1
    assert notifier_mod.summaries[-1]["trusted"] == ["Ubuntu 22.04"]
