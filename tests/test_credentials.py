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
import subprocess
from unittest.mock import patch

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

    def test_concurrent_adds_do_not_lose_entries(self, tmp_index):
        """The file lock serializes the read-modify-write so two concurrent
        ``set`` calls can't both read the pre-update set and clobber each other.

        We simulate concurrency by interleaving the read and write halves of
        two ``_index_add`` operations: if the lock were absent, the second
        write would clobber the first. With the lock, ``_index_add`` is atomic
        and both accounts survive.
        """
        import threading

        accounts = [f"acct-{i}" for i in range(20)]
        threads = [threading.Thread(target=keychain._index_add, args=(a,)) for a in accounts]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        listed = keychain.list_credentials()
        assert sorted(listed) == sorted(accounts)


# ── migration / upgrade-path coverage ─────────────────────────────


_MACOS_DUMP_WITH_INFRACONTEXT = """\
keychain: "/Users/x/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    0x00000007 <blob>=<NULL>
    "acct"<blob>="legacy-acct-one"
    "cdat"<timedate>=0x32...
    "svce"<blob>="infracontext"
    "type"<uint32>=<NULL>
keychain: "/Users/x/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    0x00000007 <blob>=<NULL>
    "acct"<blob>="legacy-acct-two"
    "cdat"<timedate>=0x32...
    "svce"<blob>="infracontext"
    "type"<uint32>=<NULL>
keychain: "/Users/x/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    "acct"<blob>="other-svc-account"
    "svce"<blob>="some-other-service"
"""


def _fake_run(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TestMigration:
    """Regression for Codex round-5: upgraders whose credentials live in
    the keychain (from a pre-index version of ic) must be able to
    backfill the index without re-entering secrets."""

    def test_migrate_populates_index_from_macos_keychain(self, tmp_index):
        with (
            patch("infracontext.credentials.keychain.platform.system", return_value="Darwin"),
            patch(
                "infracontext.credentials.keychain.subprocess.run",
                return_value=_fake_run(_MACOS_DUMP_WITH_INFRACONTEXT),
            ),
        ):
            added = keychain.migrate_from_keychain()
        assert added == ["legacy-acct-one", "legacy-acct-two"]
        assert keychain.list_credentials() == ["legacy-acct-one", "legacy-acct-two"]

    def test_migrate_is_idempotent(self, tmp_index):
        """Running migrate twice doesn't double-list or fail."""
        with (
            patch("infracontext.credentials.keychain.platform.system", return_value="Darwin"),
            patch(
                "infracontext.credentials.keychain.subprocess.run",
                return_value=_fake_run(_MACOS_DUMP_WITH_INFRACONTEXT),
            ),
        ):
            keychain.migrate_from_keychain()
            second = keychain.migrate_from_keychain()
        assert second == []  # nothing new
        assert keychain.list_credentials() == ["legacy-acct-one", "legacy-acct-two"]

    def test_migrate_preserves_existing_index_entries(self, tmp_index):
        """User added 'new-acct' post-upgrade, then runs migrate; both old
        keychain entries and the new index entry must coexist."""
        keychain._index_add("new-acct")
        with (
            patch("infracontext.credentials.keychain.platform.system", return_value="Darwin"),
            patch(
                "infracontext.credentials.keychain.subprocess.run",
                return_value=_fake_run(_MACOS_DUMP_WITH_INFRACONTEXT),
            ),
        ):
            added = keychain.migrate_from_keychain()
        assert added == ["legacy-acct-one", "legacy-acct-two"]
        assert keychain.list_credentials() == [
            "legacy-acct-one",
            "legacy-acct-two",
            "new-acct",
        ]

    def test_migrate_holds_lock_across_read_modify_write(self, tmp_index):
        """The index backfill must take the file lock around its
        read-modify-write, exactly like _index_add/_index_remove, so a
        concurrent `set` can't clobber it. Verify structurally: the lock is
        entered before the read and released only after the write.
        """
        # Ensure the index file exists so migrate takes the read branch.
        keychain._index_add("preexisting")

        events: list[str] = []
        real_lock = keychain.file_lock

        @contextlib.contextmanager
        def _tracking_lock(path):
            events.append("lock_enter")
            with real_lock(path):
                yield
            events.append("lock_exit")

        with (
            patch("infracontext.credentials.keychain.platform.system", return_value="Darwin"),
            patch(
                "infracontext.credentials.keychain.subprocess.run",
                return_value=_fake_run(_MACOS_DUMP_WITH_INFRACONTEXT),
            ),
            patch("infracontext.credentials.keychain.file_lock", _tracking_lock),
            patch(
                "infracontext.credentials.keychain._index_read",
                side_effect=lambda: events.append("read") or set(),
            ),
            patch(
                "infracontext.credentials.keychain._index_write",
                side_effect=lambda accounts: events.append("write"),  # noqa: ARG005
            ),
        ):
            keychain.migrate_from_keychain()

        assert events == ["lock_enter", "read", "write", "lock_exit"]

    def test_migrate_refuses_on_linux(self, tmp_index):
        with (
            patch("infracontext.credentials.keychain.platform.system", return_value="Linux"),
            pytest.raises(keychain.KeychainError, match="only supported on macOS"),
        ):
            keychain.migrate_from_keychain()

    def test_list_warns_when_index_missing_on_macos(self, tmp_index, capsys):
        """The whole point of the warning is so upgraders don't mistake
        empty output for 'nothing stored'."""
        # tmp_index points at a path; the file itself doesn't exist yet.
        assert not tmp_index.exists()
        with patch("infracontext.credentials.keychain.platform.system", return_value="Darwin"):
            result = keychain.list_credentials()
        assert result == []
        captured = capsys.readouterr()
        assert "credential migrate" in captured.err
        assert "credential index not found" in captured.err

    def test_list_does_not_warn_when_index_exists(self, tmp_index, capsys):
        keychain._index_add("anything")
        with patch("infracontext.credentials.keychain.platform.system", return_value="Darwin"):
            keychain.list_credentials()
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_list_does_not_warn_on_linux_no_migration_path(self, tmp_index, capsys):
        """No need to advertise migrate where it's not supported."""
        assert not tmp_index.exists()
        with patch("infracontext.credentials.keychain.platform.system", return_value="Linux"):
            keychain.list_credentials()
        captured = capsys.readouterr()
        assert captured.err == ""


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
