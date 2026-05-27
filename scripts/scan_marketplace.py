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
        codename_to_version = {
            "noble": "24.04",
            "jammy": "22.04",
            "focal": "20.04",
            "bionic": "18.04",
        }
        for codename, version in codename_to_version.items():
            if codename in o:
                return "apt", f"Ubuntu {version}"

        match = re.search(r"(\d{2})_(\d{2})", s)
        if match:
            return "apt", f"Ubuntu {match.group(1)}.{match.group(2)}"

        return "apt", "Ubuntu"

    # Debian family
    if p == "debian" or "debian" in o:
        match = re.search(r"debian-(\d+)", o) or re.search(r"^(\d+)", s)
        if match:
            return "apt", f"Debian {match.group(1)}"
        return "apt", "Debian"

    # Red Hat family
    if p == "redhat" or o == "rhel" or "rhel" in o:
        match = re.search(r"^(\d+)", s)
        if match:
            return "yum", f"RHEL {match.group(1)}"
        return "yum", "RHEL"

    # SUSE family
    if p == "suse" or "sles" in o or "suse" in o or "opensuse" in o:
        match = re.search(r"(?:sles[-_]?)(\d+)", o) or re.search(r"^(\d+)", s)
        if match:
            return "zypper", f"SLES {match.group(1)}"
        if "opensuse" in o:
            return "zypper", "openSUSE"
        return "zypper", "SUSE Linux"

    return "unknown", "Unknown"


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

                    for version in versions:
                        is_new = db_manager.check_and_upsert(
                            config.DB_PATH,
                            publisher,
                            offer,
                            sku,
                            version,
                            region,
                        )
                        if is_new:
                            record = db_manager.get_image_record(
                                config.DB_PATH,
                                publisher,
                                offer,
                                sku,
                                version,
                                region,
                            )
                            family, distro_label = derive_family_and_distro_label(
                                record.get("publisher", ""),
                                record.get("image", ""),
                                record.get("sku", ""),
                            )
                            record["family"] = family
                            record["distro_label"] = distro_label
                            new_images.append(record)

    # ------------------------------------------------------------------
    # Step 5 — Write JSON output
    # ------------------------------------------------------------------
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    with open(config.OUTPUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(new_images, fh, indent=2)

    # ------------------------------------------------------------------
    # Step 6 — Exit code
    # ------------------------------------------------------------------
    if new_images:
        logger.info(
            "Scan complete: %d new image(s) found. Output written to %s",
            len(new_images),
            config.OUTPUT_JSON,
        )
        return 1  # Signals GH Actions to send email notification

    logger.info("Scan complete: no new images found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
