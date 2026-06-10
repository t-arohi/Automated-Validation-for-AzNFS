"""Send notifications via Azure Communication Services Email using Managed Identity."""
from __future__ import annotations

import html
import logging
from typing import Iterable

from azure.communication.email import EmailClient
from azure.identity import DefaultAzureCredential

import config

logger = logging.getLogger(__name__)


def _client() -> EmailClient:
    credential = DefaultAzureCredential(
        managed_identity_client_id=config.AZURE_MANAGED_IDENTITY_CLIENT_ID
    )
    return EmailClient(config.ACS_ENDPOINT, credential)


# Columns shown in the distro-rollup table (the cut-down, per-OS-release view).
_DISTRO_COLUMNS = ["family", "distro_label", "publishers", "architectures", "sku_count"]
_DISTRO_LABELS = {"sku_count": "# SKUs", "architectures": "arch"}


def _fmt(value) -> str:
    return ", ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value)


def _distro_rows_html(rollup: list[dict]) -> str:
    head = "".join(
        f"<th style='text-align:left;padding:4px 8px;background:#f3f3f3'>"
        f"{_DISTRO_LABELS.get(h, h)}</th>"
        for h in _DISTRO_COLUMNS
    )
    body = ""
    for row in rollup:
        cells = "".join(
            f"<td style='padding:4px 8px;border-top:1px solid #ddd'>"
            f"{html.escape(_fmt(row.get(h, '')))}</td>"
            for h in _DISTRO_COLUMNS
        )
        body += f"<tr>{cells}</tr>"
    return (
        "<table style='border-collapse:collapse;font-family:Segoe UI,sans-serif;"
        f"font-size:13px'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def _distro_plain(rollup: list[dict]) -> str:
    return "\n".join(
        f"- {r.get('family')} / {r.get('distro_label')} "
        f"[{_fmt(r.get('publishers', []))}; {_fmt(r.get('architectures', []))}] "
        f"({r.get('sku_count')} SKU(s))"
        for r in rollup
    )


def send_phase1_summary(
    new_distros: list[dict],
    recipients: Iterable[str] | None = None,
) -> None:
    """Phase 1 notification: NEW distro releases that need AzNFS validation.

    Reports at the distro-release granularity only — the cut-down list. The
    underlying SKU / version / region / architecture churn is deliberately not
    shown (it is tracked in the DB and the per-SKU artifact for auditing), so a
    fresh scan reports a handful of OS releases, not hundreds of SKUs.
    """
    if not new_distros:
        logger.info("No new distro releases — skipping notification.")
        return

    recipients = list(recipients or config.NOTIFY_RECIPIENTS)
    if not recipients:
        logger.warning("No recipients configured — skipping notification.")
        return

    n = len(new_distros)
    subject = f"[AzFilesAutoPackager] {n} new distro release(s) need validation"

    plain = (
        f"{n} new distro release(s) need AzNFS validation "
        f"(collapsed from marketplace SKUs; sku/version/region/architecture are "
        f"not part of a distro's identity):\n\n"
        f"{_distro_plain(new_distros)}"
    )

    html_body = (
        f"<h3 style='font-family:Segoe UI,sans-serif'>Distro releases to validate "
        f"<span style='color:#888;font-weight:normal'>({n})</span></h3>"
        f"<p style='font-family:Segoe UI,sans-serif;color:#555'>"
        f"New OS releases discovered on the marketplace — the unit AzNFS validates. "
        f"SKU / version / region / architecture are collapsed (shown as counts).</p>"
        f"{_distro_rows_html(new_distros)}"
    )

    _send(subject, plain, html_body, recipients)


# Family display order + titles for the monthly reminder.
_FAMILY_ORDER = ["apt", "yum"]
_FAMILY_TITLES = {
    "apt": "apt — Debian / Ubuntu family",
    "yum": "yum — RHEL / SUSE / Azure Linux family",
}


def _ordered_families(buckets: dict[str, list[dict]]) -> list[str]:
    known = [f for f in _FAMILY_ORDER if f in buckets]
    rest = sorted(f for f in buckets if f not in _FAMILY_ORDER)
    return known + rest


def _reminder_table_html(distros: list[dict]) -> str:
    cols = [
        ("distro_label", "Distro"),
        ("version", "Latest version"),
        ("publishers", "Publishers"),
        ("sku_count", "# SKUs"),
    ]
    head = "".join(
        f"<th style='text-align:left;padding:4px 8px;background:#f3f3f3'>{lbl}</th>"
        for _, lbl in cols
    )
    body = ""
    for d in distros:
        cells = "".join(
            f"<td style='padding:4px 8px;border-top:1px solid #ddd'>"
            f"{html.escape(_fmt(d.get(key, '')))}</td>"
            for key, _ in cols
        )
        body += f"<tr>{cells}</tr>"
    return (
        "<table style='border-collapse:collapse;font-family:Segoe UI,sans-serif;"
        f"font-size:13px'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def send_monthly_reminder(
    buckets: dict[str, list[dict]],
    recipients: Iterable[str] | None = None,
) -> None:
    """Monthly reminder: every tracked distro release, grouped by package family.

    Sent at most once per calendar month (on the first scan of the month with no
    new releases), so the daily "nothing new" runs stay silent while the team
    still gets a periodic snapshot of everything being tracked.
    """
    recipients = list(recipients or config.NOTIFY_RECIPIENTS)
    if not recipients:
        logger.warning("No recipients configured — skipping notification.")
        return

    families = _ordered_families(buckets)
    total_distros = sum(len(buckets[f]) for f in families)
    total_skus = sum(d.get("sku_count", 0) for f in families for d in buckets[f])

    subject = (
        f"[AzFilesAutoPackager] Monthly reminder: {total_distros} distro release(s) tracked"
    )

    plain_parts = [
        f"Monthly snapshot of all distro releases tracked by AzFilesAutoPackager "
        f"({total_distros} release(s) across {total_skus} SKU(s)):",
        "",
    ]
    for fam in families:
        plain_parts.append(f"[{fam}]")
        for d in buckets[fam]:
            plain_parts.append(
                f"  - {d.get('distro_label')} "
                f"(latest {d.get('version')}; {_fmt(d.get('publishers', []))}; "
                f"{d.get('sku_count')} SKU(s))"
            )
        plain_parts.append("")
    plain = "\n".join(plain_parts)

    sections = ""
    for fam in families:
        title = _FAMILY_TITLES.get(fam, fam)
        sections += (
            f"<h4 style='font-family:Segoe UI,sans-serif;margin:12px 0 4px'>"
            f"{html.escape(title)} "
            f"<span style='color:#888;font-weight:normal'>({len(buckets[fam])})</span></h4>"
            f"{_reminder_table_html(buckets[fam])}"
        )
    html_body = (
        f"<h3 style='font-family:Segoe UI,sans-serif'>Monthly distro tracking reminder "
        f"<span style='color:#888;font-weight:normal'>({total_distros})</span></h3>"
        f"<p style='font-family:Segoe UI,sans-serif;color:#555'>"
        f"All distro releases currently tracked by AzFilesAutoPackager, grouped by "
        f"package family. This is a once-a-month snapshot — daily scans with nothing "
        f"new stay silent.</p>"
        f"{sections}"
    )

    _send(subject, plain, html_body, recipients)


def _send(subject: str, plain: str, html_body: str, recipients: list[str]) -> None:
    message = {
        "senderAddress": config.ACS_SENDER,
        "recipients": {"to": [{"address": addr.strip()} for addr in recipients if addr.strip()]},
        "content": {"subject": subject, "plainText": plain, "html": html_body},
    }
    try:
        poller = _client().begin_send(message)
        result = poller.result()
        logger.info("Email sent (id=%s) to %d recipient(s).",
                    getattr(result, "id", "?"), len(recipients))
    except Exception as exc:
        # Never let a notification failure crash the scan.
        logger.error("Failed to send notification: %s", exc)
