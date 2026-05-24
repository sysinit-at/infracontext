"""Tests for the credentials/keychain wrapper.

The current design splits the concerns:
- ``keyring`` library handles secret storage on every platform (the
  set/get/delete path).
- A small JSON metadata index (account names only) backs ``list``, so
  no platform-specific subprocess parsing is needed and no secret is
  ever materialized to enumerate accounts.

Tests verify the round-trip behavior with the index, plus an opt-in
live macOS round-trip against the real Keychain.
"""

from __future__ import annotations

import contextlib
import json
import platform

import pytest

from infracontext.credentials import keychain


@pytest.fixture()
def tmp_index(tmp_path, monkeypatch):
    """Point the credential index at a temp file."""
    index = tmp_path / "credentials-index.json"
    monkeypatch.setenv("INFRACONTEXT_CREDENTIALS_INDEX", str(index))
    return index


# ── account index round-trip ──────────────────────────────────────


class TestAccountIndex:
    def test_list_empty_when_no_index_file(self, tmp_index):
        # Index file does not exist yet.
        assert not tmp_index.exists()
        assert keychain.list_credentials() == []

    def test_index_add_creates_file(self, tmp_index):
        keychain._index_add("acct-one")
        assert tmp_index.exists()
        payload = json.loads(tmp_index.read_text())
        assert payload == {"accounts": ["acct-one"]}

    def test_index_round_trip(self, tmp_index):
        keychain._index_add("b")
        keychain._index_add("a")
        keychain._index_add("c")
        keychain._index_add("a")  # dedupe
        assert keychain.list_credentials() == ["a", "b", "c"]

    def test_index_remove(self, tmp_index):
        keychain._index_add("a")
        keychain._index_add("b")
        keychain._index_remove("a")
        assert keychain.list_credentials() == ["b"]

    def test_index_remove_missing_is_noop(self, tmp_index):
        keychain._index_add("a")
        keychain._index_remove("never-there")
        assert keychain.list_credentials() == ["a"]

    def test_index_remove_before_create_is_noop(self, tmp_index):
        keychain._index_remove("any")
        assert not tmp_index.exists()

    def test_malformed_index_raises_keychainerror(self, tmp_index):
        tmp_index.write_text("not json")
        with pytest.raises(keychain.KeychainError):
            keychain.list_credentials()

    def test_env_var_overrides_default_path(self, tmp_path, monkeypatch):
        """The env-var hook is documented; pin it so it can't silently move."""
        target = tmp_path / "elsewhere.json"
        monkeypatch.setenv("INFRACONTEXT_CREDENTIALS_INDEX", str(target))
        keychain._index_add("x")
        assert target.exists()

    def test_xdg_config_home_respected(self, tmp_path, monkeypatch):
        monkeypatch.delenv("INFRACONTEXT_CREDENTIALS_INDEX", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        expected = tmp_path / "cfg" / "infracontext" / "credentials-index.json"
        keychain._index_add("x")
        assert expected.exists()


# ── live round-trip (skipped on non-macOS) ────────────────────────


@pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="Live keychain round-trip is only exercised on macOS hosts; "
    "Linux CI without a session bus would prompt for unlock.",
)
class TestLiveKeychainRoundTrip:
    """End-to-end set / list / get / delete against the real Keychain.

    Verifies that the hybrid (keyring secret-path + index list-path)
    wrapper actually enumerates the accounts a ``set`` call produced,
    which is the property the earlier keyring-only implementation broke.
    """

    ACCOUNTS = ["ic-test-rt-1", "ic-test-rt-2"]

    @pytest.fixture(autouse=True)
    def _scoped_index(self, tmp_index):
        # tmp_index already pointed INFRACONTEXT_CREDENTIALS_INDEX at a
        # writable temp path; nothing else needed here.
        yield
        for acct in self.ACCOUNTS:
            with contextlib.suppress(keychain.KeychainError):
                keychain.delete_credential(acct)

    def test_set_then_list_enumerates_all(self):
        for acct in self.ACCOUNTS:
            keychain.set_credential(acct, "test-secret")
        listed = keychain.list_credentials()
        for acct in self.ACCOUNTS:
            assert acct in listed, f"missing '{acct}' in {listed}"

    def test_delete_drops_from_list(self):
        keychain.set_credential(self.ACCOUNTS[0], "x")
        keychain.set_credential(self.ACCOUNTS[1], "y")
        keychain.delete_credential(self.ACCOUNTS[0])
        listed = keychain.list_credentials()
        assert self.ACCOUNTS[0] not in listed
        assert self.ACCOUNTS[1] in listed
