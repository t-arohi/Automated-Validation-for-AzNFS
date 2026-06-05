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


def _img(**kw):
    base = {
        "publisher": "RedHat", "image": "RHEL", "sku": "9-lvm-gen2",
        "version": "9.3.2026", "region": "eastus", "architecture": "x86_64",
        "family": "yum", "distro_label": "RHEL 9",
        "validated": "unknown", "date_added": "2026-06-05T00:00:00Z",
    }
    base.update(kw)
    return base


def test_summary_with_new_only(notifier):
    mod, email_client_cls = notifier
    instance = email_client_cls.return_value
    instance.begin_send.return_value.result.return_value = mock.Mock(id="msg-1")

    mod.send_phase1_summary([_img(), _img(family="apt", distro_label="Ubuntu 24.04")])

    instance.begin_send.assert_called_once()
    msg = instance.begin_send.call_args[0][0]
    assert "2 new" in msg["content"]["subject"]
    assert "version bump" not in msg["content"]["subject"]
    assert "RHEL 9" in msg["content"]["plainText"]
    assert "x86_64" in msg["content"]["html"]
    assert "Ubuntu 24.04" in msg["content"]["html"]
    # image (offer) must be present in both HTML and plain text — it is part of
    # a SKU's identity, so omitting it makes distinct rows look like duplicates.
    assert "image (offer)" in msg["content"]["html"]
    assert "RHEL" in msg["content"]["html"]
    assert "RHEL" in msg["content"]["plainText"]


def test_summary_includes_distro_rollup(notifier):
    mod, email_client_cls = notifier
    instance = email_client_cls.return_value
    instance.begin_send.return_value.result.return_value = mock.Mock(id="msg-d")

    rollup = [
        {"family": "apt", "distro_label": "Ubuntu 22.04",
         "publishers": ["Canonical"], "architectures": ["x86_64"], "sku_count": 3},
    ]
    mod.send_phase1_summary([_img()], [], new_distro_rollup=rollup)

    msg = instance.begin_send.call_args[0][0]
    assert "Distro releases to validate" in msg["content"]["html"]
    assert "Ubuntu 22.04" in msg["content"]["html"]
    assert "Ubuntu 22.04" in msg["content"]["plainText"]



def test_summary_with_updates_only(notifier):
    mod, email_client_cls = notifier
    instance = email_client_cls.return_value
    instance.begin_send.return_value.result.return_value = mock.Mock(id="msg-2")

    mod.send_phase1_summary([], [_img(validated="known_supported")])

    msg = instance.begin_send.call_args[0][0]
    assert "1 version bump" in msg["content"]["subject"]
    assert "Version bumps" in msg["content"]["html"]
    assert "known_supported" in msg["content"]["html"]


def test_summary_mixed(notifier):
    mod, email_client_cls = notifier
    instance = email_client_cls.return_value
    instance.begin_send.return_value.result.return_value = mock.Mock(id="msg-3")

    mod.send_phase1_summary(
        [_img(sku="new-sku")],
        [_img(sku="bumped-sku", validated="known_unsupported")],
    )

    msg = instance.begin_send.call_args[0][0]
    assert "1 new + 1 version bump" in msg["content"]["subject"]
    assert "new-sku" in msg["content"]["html"]
    assert "bumped-sku" in msg["content"]["html"]


def test_summary_noop_on_empty(notifier):
    mod, email_client_cls = notifier
    mod.send_phase1_summary([], [])
    email_client_cls.return_value.begin_send.assert_not_called()


def test_summary_swallows_errors(notifier):
    mod, email_client_cls = notifier
    email_client_cls.return_value.begin_send.side_effect = RuntimeError("boom")
    mod.send_phase1_summary([_img()])  # must not raise


def test_backwards_compatible_alias(notifier):
    mod, email_client_cls = notifier
    instance = email_client_cls.return_value
    instance.begin_send.return_value.result.return_value = mock.Mock(id="msg-4")
    mod.send_phase1_new_distros([_img()])
    assert email_client_cls.return_value.begin_send.called
