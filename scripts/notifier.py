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


# Validation-state display order + titles for the monthly reminder. The three
# groups the monthly digest is split into; "unknown" also folds in the
# not-yet-decided pending_* states (anything without a final supported verdict).
_STATE_ORDER = ["known_supported", "known_unsupported", "unknown"]
_STATE_TITLES = {
    "known_supported": "Known supported",
    "known_unsupported": "Known unsupported",
    "unknown": "Unknown (not yet validated)",
}


def _ordered_states(buckets: dict[str, list[dict]]) -> list[str]:
    # Always show the three canonical states (even when empty), then any extras.
    extras = sorted(s for s in buckets if s not in _STATE_ORDER)
    return _STATE_ORDER + extras


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
    """Monthly reminder: every tracked distro release, grouped by validation state.

    Three groups — known_supported / known_unsupported / unknown (the last also
    folds in the not-yet-decided pending_* states). ``buckets`` maps each state
    key to a distro-rollup list (one entry per OS release, with its latest
    version, contributing publishers and SKU count). Sent at most once per
    calendar month (on the first scan of the month), so the daily "nothing new"
    runs stay silent while the team still gets a periodic snapshot of everything
    tracked, by category.
    """
    recipients = list(recipients or config.NOTIFY_RECIPIENTS)
    if not recipients:
        logger.warning("No recipients configured — skipping notification.")
        return

    states = _ordered_states(buckets)
    total_distros = sum(len(buckets.get(s, [])) for s in states)
    total_skus = sum(d.get("sku_count", 0) for s in states for d in buckets.get(s, []))
    counts = {s: len(buckets.get(s, [])) for s in _STATE_ORDER}

    subject = (
        f"[AzFilesAutoPackager] Monthly reminder: "
        f"{counts['known_supported']} supported, "
        f"{counts['known_unsupported']} unsupported, "
        f"{counts['unknown']} unknown"
    )

    plain_parts = [
        f"Monthly snapshot of all distro releases tracked by AzFilesAutoPackager "
        f"({total_distros} release(s) across {total_skus} SKU(s)), grouped by AzNFS "
        f"validation state:",
        "",
    ]
    for st in states:
        rows = buckets.get(st, [])
        plain_parts.append(f"[{_STATE_TITLES.get(st, st)}] ({len(rows)})")
        if rows:
            for d in rows:
                plain_parts.append(
                    f"  - {d.get('distro_label')} "
                    f"(latest {d.get('version')}; {_fmt(d.get('publishers', []))}; "
                    f"{d.get('sku_count')} SKU(s))"
                )
        else:
            plain_parts.append("  (none)")
        plain_parts.append("")
    plain = "\n".join(plain_parts)

    sections = ""
    for st in states:
        rows = buckets.get(st, [])
        title = _STATE_TITLES.get(st, st)
        sections += (
            f"<h4 style='font-family:Segoe UI,sans-serif;margin:12px 0 4px'>"
            f"{html.escape(title)} "
            f"<span style='color:#888;font-weight:normal'>({len(rows)})</span></h4>"
            + (
                _reminder_table_html(rows)
                if rows
                else "<p style='font-family:Segoe UI,sans-serif;color:#888'>(none)</p>"
            )
        )
    html_body = (
        f"<h3 style='font-family:Segoe UI,sans-serif'>Monthly distro tracking reminder "
        f"<span style='color:#888;font-weight:normal'>({total_distros})</span></h3>"
        f"<p style='font-family:Segoe UI,sans-serif;color:#555'>"
        f"All distro releases currently tracked by AzFilesAutoPackager, grouped by "
        f"AzNFS validation state. This is a once-a-month snapshot — daily scans with "
        f"nothing new stay silent.</p>"
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
