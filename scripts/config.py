"""
Central configuration for the marketplace scanner.
All tuneable values live here; secrets come from environment variables.
"""

import os

# ---------------------------------------------------------------------------
# Azure regions to scan
# ---------------------------------------------------------------------------
# AzNFS is currently validated only in eastus; expand this list if the
# project starts publishing per-region builds.
REGIONS = [
    "eastus",
]

# ---------------------------------------------------------------------------
# Publishers to scan
# ---------------------------------------------------------------------------
PUBLISHERS = [
    "Canonical",
    "RedHat",
    "SUSE",
    "Debian",
    # Microsoft's own distro: Azure Linux 3.x and CBL-Mariner 1.x/2.x.
    "MicrosoftCBLMariner",
]

# ---------------------------------------------------------------------------
# Azure credentials  (set via environment; never hardcode)
# For local dev:   run `az login` and DefaultAzureCredential picks it up.
# For the Azure VM runner: prefer Managed Identity.
#   - System-assigned MI: only AZURE_SUBSCRIPTION_ID is required.
#   - User-assigned MI: set AZURE_MANAGED_IDENTITY_CLIENT_ID as well.
# ---------------------------------------------------------------------------
AZURE_SUBSCRIPTION_ID: str = os.environ["AZURE_SUBSCRIPTION_ID"]
AZURE_MANAGED_IDENTITY_CLIENT_ID: str | None = os.environ.get(
    "AZURE_MANAGED_IDENTITY_CLIENT_ID"
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPTS_DIR)

DB_PATH: str = os.environ.get(
    "DB_PATH", os.path.join(_PROJECT_ROOT, "marketplace.db")
)

SCHEMA_PATH: str = os.path.join(_PROJECT_ROOT, "db", "schema.sql")

OUTPUT_DIR: str = os.environ.get(
    "OUTPUT_DIR", os.path.join(_PROJECT_ROOT, "output")
)

OUTPUT_JSON: str = os.path.join(OUTPUT_DIR, "needs_validation.json")

# Distro-level rollup: the de-duplicated list of OS releases AzNFS must validate,
# collapsed from the per-SKU rows (sku/version/region/architecture/offer folded away).
OUTPUT_DISTROS: str = os.path.join(OUTPUT_DIR, "distros_to_validate.json")

# ---------------------------------------------------------------------------
# Notifications  (Azure Communication Services Email)
# ---------------------------------------------------------------------------
# ACS_ENDPOINT example: https://<resource-name>.communication.azure.com
# ACS_SENDER  example: DoNotReply@<verified-domain>.azurecomm.net
ACS_ENDPOINT: str = os.environ.get("ACS_ENDPOINT", "")
ACS_SENDER: str = os.environ.get("ACS_SENDER", "")

# Comma-separated recipient list (env override supported).
_DEFAULT_RECIPIENTS = (
    "psachdeva@microsoft.com,"
    "rajasimandal@microsoft.com,"
    "Shyam.Prasad@microsoft.com,"
    "vaibsharma@microsoft.com,"
    "t-arohi@microsoft.com"
)
NOTIFY_RECIPIENTS: list[str] = [
    addr.strip()
    for addr in os.environ.get("NOTIFY_RECIPIENTS", _DEFAULT_RECIPIENTS).split(",")
    if addr.strip()
]
