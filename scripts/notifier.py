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

    Reports at the distro-release granularity only ΓÇö the cut-down list. The
    underlying SKU / version / region / architecture churn is deliberately not
    shown (it is tracked in the DB and the per-SKU artifact for auditing), so a
    fresh scan reports a handful of OS releases, not hundreds of SKUs.
    """
    if not new_distros:
        logger.info("No new distro releases ΓÇö skipping notification.")
        return

    recipients = list(recipients or config.NOTIFY_RECIPIENTS)
    if not recipients:
        logger.warning("No recipients configured ΓÇö skipping notification.")
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
        f"New OS releases discovered on the marketplace ΓÇö the unit AzNFS validates. "
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


def _reminder_table_html(distros: list[dict], with_reason: bool = False) -> str:
    cols = [
        ("distro_label", "Distro"),
        ("version", "Latest version"),
        ("publishers", "Publishers"),
        ("sku_count", "# SKUs"),
    ]
    if with_reason:
        cols.append(("reason", "Reason"))
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

    Three groups ΓÇö known_supported / known_unsupported / unknown (the last also
    folds in the not-yet-decided pending_* states). ``buckets`` maps each state
    key to a distro-rollup list (one entry per OS release, with its latest
    version, contributing publishers and SKU count). Sent at most once per
    calendar month (on the first scan of the month), so the daily "nothing new"
    runs stay silent while the team still gets a periodic snapshot of everything
    tracked, by category.
    """
    recipients = list(recipients or config.NOTIFY_RECIPIENTS)
    if not recipients:
        logger.warning("No recipients configured ΓÇö skipping notification.")
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
                line = (
                    f"  - {d.get('distro_label')} "
                    f"(latest {d.get('version')}; {_fmt(d.get('publishers', []))}; "
                    f"{d.get('sku_count')} SKU(s))"
                )
                if st == "known_unsupported" and d.get("reason"):
                    line += f" -- {d['reason']}"
                plain_parts.append(line)
        else:
            plain_parts.append("  (none)")
        plain_parts.append("")
    plain = "\n".join(plain_parts)

    sections = ""
    for st in states:
        rows = buckets.get(st, [])
        title = _STATE_TITLES.get(st, st)
        # The verdict reason only applies to the known_unsupported bucket.
        with_reason = st == "known_unsupported"
        sections += (
            f"<h4 style='font-family:Segoe UI,sans-serif;margin:12px 0 4px'>"
            f"{html.escape(title)} "
            f"<span style='color:#888;font-weight:normal'>({len(rows)})</span></h4>"
            + (
                _reminder_table_html(rows, with_reason=with_reason)
                if rows
                else "<p style='font-family:Segoe UI,sans-serif;color:#888'>(none)</p>"
            )
        )
    html_body = (
        f"<h3 style='font-family:Segoe UI,sans-serif'>Monthly distro tracking reminder "
        f"<span style='color:#888;font-weight:normal'>({total_distros})</span></h3>"
        f"<p style='font-family:Segoe UI,sans-serif;color:#555'>"
        f"All distro releases currently tracked by AzFilesAutoPackager, grouped by "
        f"AzNFS validation state. This is a once-a-month snapshot ΓÇö daily scans with "
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


# ===========================================================================
# Phase 2 (PMC prod validation) notifications
# ===========================================================================
# Reuses the ACS email path (`_send`) for the team and the run summary, and a
# webhook for pre-flight ping / ops paging. Per the orchestrator's design we
# send one message per *actionable failure* and exactly one run summary; the
# successfully-published / already-published images are reported only in the
# summary (no per-image chatter).

def notify(
    subject: str,
    plain: str,
    html_body: str | None = None,
    recipients: Iterable[str] | None = None,
) -> None:
    """Generic e-mail helper (used by the Phase 2 / Phase 3 functions below)."""
    recipients = list(recipients or config.NOTIFY_RECIPIENTS)
    if not recipients:
        logger.warning("No recipients configured ΓÇö skipping notification.")
        return
    body = html_body or f"<pre style='font-family:Consolas,monospace'>{html.escape(plain)}</pre>"
    _send(subject, plain, body, recipients)


def notifications_ready() -> tuple[bool, str]:
    """True when the ACS e-mail channel is fully configured.

    Pre-flight uses this instead of sending a probe e-mail: if endpoint, sender
    or recipients are missing, actionable failures could not be delivered, so
    the run aborts (and page_ops still best-effort e-mails the abort reason).
    """
    missing = []
    if not config.ACS_ENDPOINT:
        missing.append("ACS_ENDPOINT")
    if not config.ACS_SENDER:
        missing.append("ACS_SENDER")
    if not config.NOTIFY_RECIPIENTS:
        missing.append("NOTIFY_RECIPIENTS")
    if missing:
        return False, "missing " + ", ".join(missing)
    return True, ""


def send_phase2_failure(
    distro_label: str,
    detail: str,
    recipients: Iterable[str] | None = None,
) -> None:
    """One actionable-failure notice (a gate marked the release known_unsupported).

    ``detail`` is the gate's human-actionable message (what a human must do to
    unblock the release).
    """
    subject = f"[AzNFS Phase 2] action needed: {distro_label}"
    plain = f"{distro_label}: {detail}"
    notify(subject, plain, recipients=recipients)


def send_phase2_pending_publish(
    distro_label: str,
    detail: str,
    recipients: Iterable[str] | None = None,
) -> None:
    """The prod repo exists but no AzNFS package is published for this release yet.

    Asks a human to publish the package manually; Phase 2 re-checks the release
    on its next run (the row is parked ``pending_publish`` in the DB), so no
    further action is needed here once the package appears on prod.
    """
    subject = f"[AzNFS Phase 2] publish needed: {distro_label}"
    plain = f"{distro_label}: {detail}"
    notify(subject, plain, recipients=recipients)


def send_phase2_trusted(
    distro_label: str,
    download_url: str | None = None,
    version: str | None = None,
    recipients: Iterable[str] | None = None,
) -> None:
    """Gate 3: the latest prod AzNFS version is already validated -- trusted.

    Includes the distro, the validated AzNFS version and the prod download URL
    so a human can locate the package straight from the mail.
    """
    subject = f"[AzNFS Phase 2] already validated (trusted): {distro_label}"
    lines = [f"{distro_label}: AzNFS is already validated on PMC prod -- trusted."]
    if version:
        lines.append(f"Version: {version}")
    if download_url:
        lines.append(f"Download (prod): {download_url}")
    notify(subject, "\n".join(lines), recipients=recipients)


def _summary_table_html(title: str, columns: list[tuple[str, str]], rows: list[dict]) -> str:
    """Render one titled HTML table. ``columns`` = [(dict_key, header), ...]."""
    head = "".join(
        "<th style='text-align:left;padding:6px 10px;background:#0078d4;color:#fff;"
        f"font-weight:600;white-space:nowrap'>{html.escape(hdr)}</th>"
        for _, hdr in columns
    )
    if rows:
        body = ""
        for i, r in enumerate(rows):
            bg = "#ffffff" if i % 2 == 0 else "#f6f8fa"
            cells = "".join(
                "<td style='padding:6px 10px;border-top:1px solid #e1e4e8;"
                f"word-break:break-all'>{html.escape(str(r.get(k, '') or ''))}</td>"
                for k, _ in columns
            )
            body += f"<tr style='background:{bg}'>{cells}</tr>"
    else:
        body = (
            f"<tr><td colspan='{len(columns)}' "
            "style='padding:6px 10px;color:#888'>(none)</td></tr>"
        )
    return (
        f"<h3 style='font-family:Segoe UI,sans-serif;font-size:15px;margin:18px 0 6px'>"
        f"{html.escape(title)}</h3>"
        "<table style='border-collapse:collapse;font-family:Segoe UI,sans-serif;"
        "font-size:13px;border:1px solid #e1e4e8'>"
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def send_phase2_summary(
    processed: int,
    to_phase3: list[dict] | None = None,
    trusted: list[dict] | None = None,
    pending_publish: list[dict] | None = None,
    unsupported: list[dict] | None = None,
    errors: list[tuple[str, str]] | None = None,
    recipients: Iterable[str] | None = None,
) -> None:
    """The single end-of-run summary, rendered as four readable HTML tables.

    Each bucket is a list of small dicts (see ``orchestrator.NotifierLike``):
      * ``to_phase3``       -> {label, arch, url}
      * ``trusted``         -> {label, arch}
      * ``pending_publish`` -> {label, arch, reason}
      * ``unsupported``     -> {label, arch, reason}
    ``arch`` is its own column so the same distro on x86_64 and arm64 reads as
    two clear rows rather than a confusing repeat.
    """
    to_phase3 = to_phase3 or []
    trusted = trusted or []
    pending_publish = pending_publish or []
    unsupported = unsupported or []
    errors = errors or []

    subject = (
        f"[AzNFS Phase 2] run summary: {len(to_phase3)} to Phase 3, "
        f"{len(trusted)} trusted, {len(pending_publish)} pending publish, "
        f"{len(unsupported)} known_unsupported"
    )

    def _plain(rows, keys):
        if not rows:
            return "  (none)"
        return "\n".join(
            "  - " + " | ".join(f"{k}={r.get(k, '')}" for k in keys) for r in rows
        )

    plain = (
        f"Phase 2 processed {processed} image(s).\n\n"
        f"a) Handed to Phase 3 ({len(to_phase3)}):\n{_plain(to_phase3, ['label', 'arch', 'url'])}\n\n"
        f"b) Already validated on prod, trusted ({len(trusted)}):\n{_plain(trusted, ['label', 'arch'])}\n\n"
        f"c) Pending manual publish ({len(pending_publish)}):\n{_plain(pending_publish, ['label', 'arch', 'reason'])}\n\n"
        f"d) Marked known_unsupported ({len(unsupported)}):\n{_plain(unsupported, ['label', 'arch', 'reason'])}"
        + (
            "\n\ne) Orchestrator errors (" + str(len(errors)) + "):\n"
            + "\n".join(f"  - {lbl}: {reason}" for lbl, reason in errors)
            if errors
            else ""
        )
    )

    html_body = (
        "<div style='font-family:Segoe UI,sans-serif;color:#24292e'>"
        f"<p style='font-size:14px'>Phase 2 processed <b>{processed}</b> image(s) &mdash; "
        f"<b>{len(to_phase3)}</b> to Phase 3, <b>{len(trusted)}</b> trusted, "
        f"<b>{len(pending_publish)}</b> pending publish, "
        f"<b>{len(unsupported)}</b> known_unsupported.</p>"
        + _summary_table_html(
            f"a) Handed to Phase 3 ({len(to_phase3)})",
            [("label", "Distro"), ("arch", "Arch"), ("url", "AzNFS package URL")],
            to_phase3,
        )
        + _summary_table_html(
            f"b) Already validated on prod, trusted ({len(trusted)})",
            [("label", "Distro"), ("arch", "Arch")],
            trusted,
        )
        + _summary_table_html(
            f"c) Pending manual publish ({len(pending_publish)})",
            [("label", "Distro"), ("arch", "Arch"), ("reason", "Reason")],
            pending_publish,
        )
        + _summary_table_html(
            f"d) Marked known_unsupported ({len(unsupported)})",
            [("label", "Distro"), ("arch", "Arch"), ("reason", "Reason")],
            unsupported,
        )
        + (
            _summary_table_html(
                f"e) Orchestrator errors ({len(errors)})",
                [("label", "Distro"), ("reason", "Reason")],
                [{"label": lbl, "reason": reason} for lbl, reason in errors],
            )
            if errors
            else ""
        )
        + "</div>"
    )
    notify(subject, plain, html_body=html_body, recipients=recipients)


def post_webhook(url: str | None, text: str, timeout: int = 15) -> bool:
    """POST a simple ``{"text": ...}`` payload to a webhook. Returns success.

    Used for the pre-flight reachability ping. Never raises ΓÇö a webhook problem
    is reported via the return value so the caller can decide.
    """
    if not url:
        logger.warning("No webhook URL configured ΓÇö skipping webhook post.")
        return False
    try:
        import requests  # local import: keeps Phase 1's import surface unchanged
        resp = requests.post(url, json={"text": text}, timeout=timeout)
        if not resp.ok:
            logger.error("Webhook POST -> %s", resp.status_code)
            return False
        return True
    except Exception as exc:  # pragma: no cover - network/dep guard
        logger.error("Webhook POST failed: %s", exc)
        return False


def page_ops(reason: str, target: str | None = None, timeout: int = 15) -> bool:
    """Page ops on a pre-flight abort (whole-run failure).

    Posts to the ops ``target`` webhook and also e-mails the team as a durable
    record. Returns whether the webhook page was delivered.
    """
    text = f"[AzNFS Phase 2 PRE-FLIGHT ABORT] {reason}"
    posted = post_webhook(target, text, timeout=timeout) if target else False
    try:
        notify("[AzNFS Phase 2] PRE-FLIGHT ABORT", text)
    except Exception as exc:  # pragma: no cover - email is a best-effort record
        logger.error("page_ops e-mail failed: %s", exc)
    return posted
