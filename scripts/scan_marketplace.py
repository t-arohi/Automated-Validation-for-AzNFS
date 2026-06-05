#!/usr/bin/env python3
"""
scan_marketplace.py — Phase 1 entry point.

Workflow
--------
1. Ensure the local SQLite database exists (create from schema if not).
2. Authenticate to Azure and build a Compute client.
3. For every configured region × publisher, crawl offers → SKUs → versions.
4. For each version:
     - If the (publisher, offer, sku, version, region) tuple is NOT in the DB
       → insert it as validated='unknown' and flag it for the output JSON.
     - If it IS in the DB → update last_checked only; no output.
5. Write all flagged images to output/needs_validation.json.
6. Exit with code 0 when there are no new images, or 1 when new images were found
   so that the GitHub Actions step can branch on the result.

Running locally
---------------
  export AZURE_SUBSCRIPTION_ID=<your-sub-id>
  az login          # DefaultAzureCredential picks this up
  python scan_marketplace.py
"""

import json
import logging
import os
import re
import sys

import config
import azure_client
import db_manager
import notifier

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Derivations
# ---------------------------------------------------------------------------

def derive_family_and_distro_label(
    publisher: str, offer: str, sku: str
) -> tuple[str, str]:
    """Derive package family and human-readable distro label from image metadata."""
    p = (publisher or "").strip().lower()
    o = (offer or "").strip().lower()
    s = (sku or "").strip().lower()

    # Ubuntu family
    if p == "canonical" and "ubuntu" in o:
        # Ubuntu Core is a distinct product line (e.g. ubuntu-core-24-private).
        if "ubuntu-core" in o:
            match = re.search(r"ubuntu-core-(\d+)", o)
            if match:
                return "apt", f"Ubuntu Core {match.group(1)}"
            return "apt", "Ubuntu Core"

        # Releases are encoded as NN_NN or NN.NN in the offer (e.g.
        # ubuntu-22_04-lts, ubuntu-pro-26_04-lts) or the SKU (e.g. 16.04-LTS).
        match = re.search(r"(\d{2})[._](\d{2})", o) or re.search(r"(\d{2})[._](\d{2})", s)
        if match:
            return "apt", f"Ubuntu {match.group(1)}.{match.group(2)}"

        # Older / special offers use the release codename (e.g.
        # 0001-com-ubuntu-confidential-vm-focal).
        codename_to_version = {
            "xenial": "16.04",
            "bionic": "18.04",
            "focal": "20.04",
            "groovy": "20.10",
            "hirsute": "21.04",
            "impish": "21.10",
            "jammy": "22.04",
            "kinetic": "22.10",
            "lunar": "23.04",
            "mantic": "23.10",
            "noble": "24.04",
            "questing": "25.10",
            "resolute": "26.04",
        }
        for codename, version in codename_to_version.items():
            if codename in o:
                return "apt", f"Ubuntu {version}"

        return "apt", "Ubuntu"

    # Debian family
    if p == "debian" or "debian" in o:
        match = re.search(r"debian-(\d+)", o) or re.search(r"^(\d+)", s)
        if match:
            return "apt", f"Debian {match.group(1)}"
        return "apt", "Debian"

    # Red Hat family
    if p == "redhat" or o == "rhel" or "rhel" in o:
        # SKUs encode the release as major[.minor] with assorted separators or
        # none at all, e.g. 9-lvm-gen2 -> 9, 8_2 -> 8.2, 7.9 -> 7.9,
        # 90-gen2 -> 9.0, 810 -> 8.10, 100 -> 10.0.
        match = re.match(r"(10|\d)[._-]?(\d{1,2})?", s)
        if match:
            major, minor = match.group(1), match.group(2)
            if minor:
                return "yum", f"RHEL {major}.{minor}"
            return "yum", f"RHEL {major}"
        return "yum", "RHEL"

    # SUSE family
    if p == "suse" or "sles" in o or "suse" in o or "opensuse" in o:
        match = re.search(r"(?:sles[-_]?)(\d+)", o) or re.search(r"^(\d+)", s)
        if match:
            return "yum", f"SLES {match.group(1)}"
        if "opensuse" in o:
            return "yum", "openSUSE"
        return "yum", "SUSE Linux"

    # Azure Linux / CBL-Mariner family (Microsoft's own distro).
    # Uses RPM + tdnf, so it shares the "yum" repo family for Phase 2.
    if (
        p == "microsoftcblmariner"
        or "azure-linux" in o or "azurelinux" in o
        or "cbl-mariner" in o or "mariner" in o
    ):
        match = re.search(r"azure-?linux-?(\d+)", o) or re.search(r"azure-?linux-?(\d+)", s)
        if match:
            return "yum", f"Azure Linux {match.group(1)}"
        match = (
            re.search(r"cbl-?mariner-?(\d+)", o)
            or re.search(r"cbl-?mariner-?(\d+)", s)
            or re.match(r"(\d+)", s)
        )
        if match:
            return "yum", f"CBL-Mariner {match.group(1)}"
        return "yum", "Azure Linux"

    return "unknown", "Unknown"


def rollup_by_distro(images: list[dict]) -> list[dict]:
    """Collapse SKU-level rows to the unit AzNFS actually validates: a distro release.

    SKU, version, region, architecture and offer are *not* part of a distro's
    identity — many marketplace offers/SKUs ship the same OS release — so they
    are folded away here, leaving one entry per (family, distro_label).
    Architecture and the contributing publishers/offers are kept as aggregated
    details so no information is lost.
    """
    groups: dict[tuple[str, str], dict] = {}
    for img in images:
        key = (img.get("family", ""), img.get("distro_label", ""))
        g = groups.get(key)
        if g is None:
            g = {
                "family": key[0],
                "distro_label": key[1],
                "publishers": set(),
                "architectures": set(),
                "offers": set(),
                "sku_count": 0,
            }
            groups[key] = g
        if img.get("publisher"):
            g["publishers"].add(img["publisher"])
        if img.get("architecture"):
            g["architectures"].add(img["architecture"])
        if img.get("image"):
            g["offers"].add(img["image"])
        g["sku_count"] += 1

    rollup = [
        {
            "family": g["family"],
            "distro_label": g["distro_label"],
            "publishers": sorted(g["publishers"]),
            "architectures": sorted(g["architectures"]),
            "offer_count": len(g["offers"]),
            "sku_count": g["sku_count"],
        }
        for g in groups.values()
    ]
    rollup.sort(key=lambda r: (r["family"], r["distro_label"]))
    return rollup



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # ------------------------------------------------------------------
    # Step 1 — Initialise database
    # ------------------------------------------------------------------
    logger.info("Initialising database at: %s", config.DB_PATH)
    db_manager.initialize(config.DB_PATH, config.SCHEMA_PATH)

    # ------------------------------------------------------------------
    # Step 2 — Azure client
    # ------------------------------------------------------------------
    logger.info("Building Azure Compute client …")
    client = azure_client.get_compute_client()

    # ------------------------------------------------------------------
    # Step 3+4 — Scan and compare
    # ------------------------------------------------------------------
    new_images: list[dict] = []
    updated_images: list[dict] = []

    for region in config.REGIONS:
        logger.info("=== Region: %s ===", region)

        for publisher in config.PUBLISHERS:
            logger.info("  Publisher: %s", publisher)
            offers = azure_client.list_offers(client, region, publisher)

            if not offers:
                logger.info("    No offers found — skipping.")
                continue

            for offer in offers:
                skus = azure_client.list_skus(client, region, publisher, offer)

                for sku in skus:
                    versions = azure_client.list_versions(
                        client, region, publisher, offer, sku
                    )
                    if not versions:
                        continue

                    # Dedup: one row per (publisher, image, sku, region, arch).
                    # Marketplace versions sort lexicographically (date-style).
                    latest = max(versions)
                    architecture = azure_client.get_image_architecture(
                        client, region, publisher, offer, sku, latest
                    )
                    family, distro_label = derive_family_and_distro_label(
                        publisher, offer, sku
                    )

                    status = db_manager.check_and_upsert(
                        config.DB_PATH,
                        publisher, offer, sku, latest, region,
                        architecture, family, distro_label,
                    )
                    if status == db_manager.UNCHANGED:
                        continue

                    record = db_manager.get_image_record(
                        config.DB_PATH, publisher, offer, sku, region, architecture,
                    )
                    if status == db_manager.NEW:
                        new_images.append(record)
                    else:  # UPDATED
                        updated_images.append(record)

    # ------------------------------------------------------------------
    # Step 5 — Write JSON output  (only new unknowns go to Phase 2)
    # ------------------------------------------------------------------
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    with open(config.OUTPUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(new_images, fh, indent=2)

    # ------------------------------------------------------------------
    # Step 5b — Distro-level rollup  (the unit AzNFS validates)
    # ------------------------------------------------------------------
    # The cut-down master list: every tracked SKU collapsed to its unique OS
    # release. This is what Phase 2/3 consume — sku/version/region/arch/offer
    # are not part of a distro's identity.
    all_records = db_manager.get_all_records(config.DB_PATH)
    distro_rollup = rollup_by_distro(all_records)
    with open(config.OUTPUT_DISTROS, "w", encoding="utf-8") as fh:
        json.dump(distro_rollup, fh, indent=2)
    logger.info(
        "Distro rollup: %d unique release(s) collapsed from %d SKU row(s) -> %s",
        len(distro_rollup), len(all_records), config.OUTPUT_DISTROS,
    )

    # ------------------------------------------------------------------
    # Step 6 — Notify + exit code
    # ------------------------------------------------------------------
    if new_images or updated_images:
        logger.info(
            "Scan complete: %d new + %d updated SKU(s). Output: %s",
            len(new_images), len(updated_images), config.OUTPUT_JSON,
        )
        notifier.send_phase1_summary(
            new_images,
            updated_images,
            new_distro_rollup=rollup_by_distro(new_images),
        )
        return 1

    logger.info("Scan complete: no new or updated SKUs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
