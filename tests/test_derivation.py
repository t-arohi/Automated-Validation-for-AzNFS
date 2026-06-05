"""Unit tests for distro derivation and the distro-level rollup."""
from __future__ import annotations

import os

# scan_marketplace imports config, which requires a subscription id at import time.
os.environ.setdefault("AZURE_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000")

import pytest

from scan_marketplace import derive_family_and_distro_label as derive, rollup_by_distro


@pytest.mark.parametrize(
    "publisher,offer,sku,expected",
    [
        # Ubuntu — version from offer (NN_NN) even when the SKU lacks it.
        ("Canonical", "ubuntu-22_04-lts", "server", ("apt", "Ubuntu 22.04")),
        ("Canonical", "ubuntu-pro-26_04-lts", "pro-server", ("apt", "Ubuntu 26.04")),
        # Ubuntu — version from offer codename.
        ("Canonical", "0001-com-ubuntu-confidential-vm-focal", "20_04-lts-cvm",
         ("apt", "Ubuntu 20.04")),
        # Ubuntu — SKU dotted version (UbuntuServer/16.04-LTS).
        ("Canonical", "UbuntuServer", "16.04-LTS", ("apt", "Ubuntu 16.04")),
        # Ubuntu Core is a distinct product line.
        ("Canonical", "ubuntu-core-24-private", "ubuntu-core", ("apt", "Ubuntu Core 24")),
        # RHEL — major[.minor] with assorted separators / none.
        ("RedHat", "RHEL", "9-lvm-gen2", ("yum", "RHEL 9")),
        ("RedHat", "RHEL", "8_2", ("yum", "RHEL 8.2")),
        ("RedHat", "RHEL", "90-gen2", ("yum", "RHEL 9.0")),
        ("RedHat", "RHEL", "810", ("yum", "RHEL 8.10")),
        ("RedHat", "RHEL", "100", ("yum", "RHEL 10.0")),
        # Debian / SUSE.
        ("Debian", "debian-12", "12-gen2", ("apt", "Debian 12")),
        ("SUSE", "sles-15-sp5", "gen2", ("yum", "SLES 15")),
        # Azure Linux / CBL-Mariner (Microsoft).
        ("MicrosoftCBLMariner", "azure-linux-3", "azure-linux-3-gen2",
         ("yum", "Azure Linux 3")),
        ("MicrosoftCBLMariner", "cbl-mariner", "cbl-mariner-2-gen2",
         ("yum", "CBL-Mariner 2")),
        ("MicrosoftCBLMariner", "cbl-mariner", "1-gen2", ("yum", "CBL-Mariner 1")),
    ],
)
def test_derive(publisher, offer, sku, expected):
    assert derive(publisher, offer, sku) == expected


def test_rollup_collapses_skus_to_distro_releases():
    images = [
        {"publisher": "Canonical", "image": "ubuntu-22_04-lts", "sku": "server",
         "architecture": "x86_64", "family": "apt", "distro_label": "Ubuntu 22.04"},
        {"publisher": "Canonical", "image": "ubuntu-pro-22_04-lts", "sku": "pro",
         "architecture": "arm64", "family": "apt", "distro_label": "Ubuntu 22.04"},
        {"publisher": "RedHat", "image": "RHEL", "sku": "9-gen2",
         "architecture": "x86_64", "family": "yum", "distro_label": "RHEL 9"},
    ]
    rollup = rollup_by_distro(images)

    # Three SKU rows collapse to two distro releases.
    assert len(rollup) == 2
    ubuntu = next(r for r in rollup if r["distro_label"] == "Ubuntu 22.04")
    assert ubuntu["sku_count"] == 2
    assert ubuntu["architectures"] == ["arm64", "x86_64"]
    assert ubuntu["offer_count"] == 2
    assert ubuntu["publishers"] == ["Canonical"]
    # sku / version / region / offer are not part of the rollup identity.
    assert "sku" not in ubuntu and "image" not in ubuntu
