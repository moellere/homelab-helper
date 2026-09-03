"""Secret references: literal passthrough, env/file/keyring schemes, redaction."""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING

import pytest

from homelab_helper.secrets import (
    SecretError,
    forget_secrets,
    is_reference,
    redact,
    reference_scheme,
    resolve_secret,
    secret_from_env,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


@pytest.fixture(autouse=True)
def _clean() -> None:
    forget_secrets()


def test_literal_passes_through_and_is_remembered() -> None:
    assert resolve_secret("hunter22") == "hunter22"
    assert redact("token hunter22 leaked") == "token *** leaked"


def test_short_values_are_not_redacted() -> None:
    resolve_secret("no")
    assert redact("no change") == "no change"


def test_reference_detection() -> None:
    assert is_reference("file:/x#y")
    assert is_reference("keyring:svc/user")
    assert is_reference("env:OTHER")
    assert not is_reference("https://not-a-ref")
    assert not is_reference("file:")
    assert reference_scheme("keyring:a/b") == "keyring"
    assert reference_scheme("literal") is None


def test_env_scheme_one_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTHER", "from-other")
    assert resolve_secret("env:OTHER") == "from-other"
    monkeypatch.setenv("CHAIN", "env:OTHER")
    with pytest.raises(SecretError, match="one level"):
        resolve_secret("env:CHAIN")
    with pytest.raises(SecretError, match="unset"):
        resolve_secret("env:MISSING_FOR_SURE")


def test_file_scheme_plain_yaml_with_dotted_key(tmp_path: Path) -> None:
    f = tmp_path / "secrets.yaml"
    f.write_text("proxmox:\n  token: pve-secret-value\nflat: top\n")
    assert resolve_secret(f"file:{f}#proxmox.token") == "pve-secret-value"
    assert resolve_secret(f"file:{f}#flat") == "top"
    with pytest.raises(SecretError, match="not found"):
        resolve_secret(f"file:{f}#proxmox.nope")
    with pytest.raises(SecretError, match="not a scalar"):
        resolve_secret(f"file:{f}#proxmox")
    with pytest.raises(SecretError, match="needs"):
        resolve_secret(f"file:{f}")
    assert redact("pve-secret-value in a log") == "*** in a log"


def test_file_scheme_age_uses_operator_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = tmp_path / "age.key"
    identity.write_text("AGE-SECRET-KEY-1TEST")
    monkeypatch.setenv("HOMELAB_HELPER_AGE_IDENTITY", str(identity))
    enc = tmp_path / "secrets.yaml.age"
    enc.write_bytes(b"\x00ciphertext")
    calls: list[Sequence[str]] = []

    def fake_runner(argv: Sequence[str]) -> str:
        calls.append(argv)
        return "unifi_key: decrypted-value\n"

    assert resolve_secret(f"file:{enc}#unifi_key", runner=fake_runner) == "decrypted-value"
    assert calls == [["age", "-d", "-i", str(identity), str(enc)]]
    # Cached: a second resolve of the same reference does not re-run age.
    assert resolve_secret(f"file:{enc}#unifi_key", runner=fake_runner) == "decrypted-value"
    assert len(calls) == 1


def test_file_scheme_age_without_identity_names_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOMELAB_HELPER_AGE_IDENTITY", str(tmp_path / "missing.key"))
    enc = tmp_path / "s.age"
    enc.write_bytes(b"x")
    with pytest.raises(SecretError, match="HOMELAB_HELPER_AGE_IDENTITY"):
        resolve_secret(f"file:{enc}#k", runner=lambda argv: "")


def test_file_scheme_sops_by_name_and_by_marker(tmp_path: Path) -> None:
    by_name = tmp_path / "secrets.sops.yaml"
    by_name.write_text("k: ENC[AES256_GCM,...]\nsops: {}\n")
    marker = tmp_path / "plain-looking.yaml"
    marker.write_text("k: ENC[AES256_GCM,...]\nsops:\n  version: 3.9.0\n")
    seen: list[Sequence[str]] = []

    def fake_runner(argv: Sequence[str]) -> str:
        seen.append(argv)
        return "k: clear\n"

    assert resolve_secret(f"file:{by_name}#k", runner=fake_runner) == "clear"
    assert resolve_secret(f"file:{marker}#k", runner=fake_runner) == "clear"
    assert [a[0] for a in seen] == ["sops", "sops"]


def test_missing_binary_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    enc = tmp_path / "s.sops.yaml"
    enc.write_text("sops: {}\n")
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing on PATH
    with pytest.raises(SecretError, match="not installed"):
        resolve_secret(f"file:{enc}#k")


def test_keyring_scheme_uses_optional_package(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("keyring")

    def get_password(service: str, username: str) -> str | None:
        return "ring-value" if (service, username) == ("homelab-helper", "pve") else None

    fake.get_password = get_password  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert resolve_secret("keyring:homelab-helper/pve") == "ring-value"
    with pytest.raises(SecretError, match="no keyring entry"):
        resolve_secret("keyring:homelab-helper/nobody")
    with pytest.raises(SecretError, match="needs"):
        resolve_secret("keyring:malformed")


def test_keyring_scheme_without_package_points_at_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(SecretError, match=r"\[keyring\]"):
        resolve_secret("keyring:svc/user")


def test_secret_from_env_resolves_a_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "s.yaml"
    f.write_text("hass: long-lived-token-value\n")
    monkeypatch.setenv("HOMELAB_HELPER_HASS_TOKEN", f"file:{f}#hass")
    assert secret_from_env("HOMELAB_HELPER_HASS_TOKEN") == "long-lived-token-value"
    assert secret_from_env("HOMELAB_HELPER_NOT_SET_AT_ALL") is None


def test_adapter_config_accepts_a_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homelab_helper.adapters.proxmox import ProxmoxConfig

    f = tmp_path / "s.yaml"
    f.write_text("pve: 11111111-2222-3333-4444-555555555555\n")
    monkeypatch.setenv("HOMELAB_HELPER_PROXMOX_URL", "https://pve.lan:8006")
    monkeypatch.setenv("HOMELAB_HELPER_PROXMOX_TOKEN_ID", "root@pam!helper")
    monkeypatch.setenv("HOMELAB_HELPER_PROXMOX_TOKEN_SECRET", f"file:{f}#pve")
    cfg = ProxmoxConfig.from_env()
    assert cfg.token_secret == "11111111-2222-3333-4444-555555555555"


def test_config_reports_reference_scheme_not_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homelab_helper.config import variable_status

    monkeypatch.setenv("HOMELAB_HELPER_NETBOX_TOKEN", "keyring:homelab-helper/netbox")
    row = variable_status("HOMELAB_HELPER_NETBOX_TOKEN", secret=True)
    assert row["set"] is True
    assert row["value"] is None
    assert row["reference"] == "keyring"
