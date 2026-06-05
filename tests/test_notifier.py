"""Unit tests for the notifier module (ACS SDK fully mocked)."""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture
def notifier(monkeypatch):
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("ACS_ENDPOINT", "https://example.communication.azure.com")
    monkeypatch.setenv("ACS_SENDER", "DoNotReply@example.azurecomm.net")
    monkeypatch.setenv("NOTIFY_RECIPIENTS", "a@example.com,b@example.com")

    fake_email = types.ModuleType("azure.communication.email")
    fake_email.EmailClient = mock.MagicMock()
    fake_identity = types.ModuleType("azure.identity")
    fake_identity.DefaultAzureCredential = mock.MagicMock()
    monkeypatch.setitem(sys.modules, "azure.communication.email", fake_email)
    monkeypatch.setitem(sys.modules, "azure.identity", fake_identity)

    monkeypatch.syspath_prepend(str(SCRIPTS_DIR))
    for name in ("notifier", "config"):
        sys.modules.pop(name, None)
    mod = importlib.import_module("notifier")
    return mod, fake_email.EmailClient


def _distro(**kw):
    base = {
        "family": "yum", "distro_label": "RHEL 9",
        "publishers": ["RedHat"], "architectures": ["x86_64"],
        "offer_count": 2, "sku_count": 7,
    }
    base.update(kw)
    return base


def test_summary_reports_new_distro_releases(notifier):
    mod, email_client_cls = notifier
    instance = email_client_cls.return_value
    instance.begin_send.return_value.result.return_value = mock.Mock(id="msg-1")

    mod.send_phase1_summary(
        [_distro(), _distro(family="apt", distro_label="Ubuntu 24.04",
                             publishers=["Canonical"])]
    )

    instance.begin_send.assert_called_once()
    msg = instance.begin_send.call_args[0][0]
    # Subject is distro-centric, never per-SKU.
    assert "2 new distro release(s) need validation" in msg["content"]["subject"]
    # Both releases appear in HTML and plain text.
    assert "RHEL 9" in msg["content"]["html"]
    assert "RHEL 9" in msg["content"]["plainText"]
    assert "Ubuntu 24.04" in msg["content"]["html"]
    assert "Ubuntu 24.04" in msg["content"]["plainText"]
    assert "Distro releases to validate" in msg["content"]["html"]


def test_summary_collapses_sku_counts_not_rows(notifier):
    mod, email_client_cls = notifier
    instance = email_client_cls.return_value
    instance.begin_send.return_value.result.return_value = mock.Mock(id="msg-2")

    # A single release that collapses 950 SKUs must render as ONE row showing a
    # count — never 950 rows.
    mod.send_phase1_summary([_distro(sku_count=950)])

    msg = instance.begin_send.call_args[0][0]
    assert "950" in msg["content"]["html"]
    assert msg["content"]["html"].count("<tr>") == 2  # header + one data row


def test_summary_noop_on_empty(notifier):
    mod, email_client_cls = notifier
    mod.send_phase1_summary([])
    email_client_cls.return_value.begin_send.assert_not_called()


def test_summary_swallows_errors(notifier):
    mod, email_client_cls = notifier
    email_client_cls.return_value.begin_send.side_effect = RuntimeError("boom")
    mod.send_phase1_summary([_distro()])  # must not raise
