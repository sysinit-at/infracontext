"""Tests for the credentials/keychain wrapper.

Focused on the regression Codex flagged: `list_credentials` must enumerate
all stored accounts, not just one. Set/get/delete round-trips are covered
by an opt-in live test (skipped in headless CI).
"""

from __future__ import annotations

import contextlib
import platform
import subprocess
from unittest.mock import patch

import pytest

from infracontext.credentials import keychain

# ── parser regression tests ───────────────────────────────────────


_MACOS_DUMP_TWO_ACCOUNTS = """\
keychain: "/Users/x/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    0x00000007 <blob>=<NULL>
    "acct"<blob>="account-one"
    "cdat"<timedate>=0x32...
    "svce"<blob>="infracontext"
    "type"<uint32>=<NULL>
keychain: "/Users/x/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    0x00000007 <blob>=<NULL>
    "acct"<blob>="account-two"
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


class TestListCredentialsParsing:
    def test_macos_parser_returns_all_accounts(self):
        with (
            patch("infracontext.credentials.keychain.platform.system", return_value="Darwin"),
            patch(
                "infracontext.credentials.keychain.subprocess.run",
                return_value=_fake_run(_MACOS_DUMP_TWO_ACCOUNTS),
            ),
        ):
            result = keychain.list_credentials()
        # Both infracontext entries surface; the other-service entry is filtered out.
        assert result == ["account-one", "account-two"]

    def test_macos_parser_deduplicates(self):
        # Same account appearing twice (e.g., two keychains) collapses to one.
        dump = _MACOS_DUMP_TWO_ACCOUNTS + _MACOS_DUMP_TWO_ACCOUNTS
        with (
            patch("infracontext.credentials.keychain.platform.system", return_value="Darwin"),
            patch(
                "infracontext.credentials.keychain.subprocess.run",
                return_value=_fake_run(dump),
            ),
        ):
            result = keychain.list_credentials()
        assert result == ["account-one", "account-two"]

    def test_linux_listing_refuses_to_enumerate(self):
        """Linux enumeration is intentionally NOT implemented.

        The available `secret-tool search --all` path requires libsecret to
        decrypt every matching secret to stdout, which would materialize
        every secret in this process even if the caller only wants account
        names. Refusing is the correct behavior; the test pins it so that a
        future "convenience" reintroduction can't silently regress the
        boundary that Codex flagged.
        """
        with (
            patch("infracontext.credentials.keychain.platform.system", return_value="Linux"),
            # subprocess.run must NOT be invoked. Set a sentinel that would
            # raise if anything calls it.
            patch(
                "infracontext.credentials.keychain.subprocess.run",
                side_effect=AssertionError(
                    "subprocess.run must not be invoked on Linux; secret-tool would decrypt secrets"
                ),
            ),
            pytest.raises(keychain.KeychainError, match="not supported on Linux"),
        ):
            keychain.list_credentials()

    def test_unsupported_platform_raises(self):
        with (
            patch("infracontext.credentials.keychain.platform.system", return_value="Windows"),
            pytest.raises(keychain.KeychainError),
        ):
            keychain.list_credentials()

    def test_macos_security_missing_raises_keychainerror(self):
        with (
            patch("infracontext.credentials.keychain.platform.system", return_value="Darwin"),
            patch(
                "infracontext.credentials.keychain.subprocess.run",
                side_effect=FileNotFoundError("security"),
            ),
            pytest.raises(keychain.KeychainError),
        ):
            keychain.list_credentials()


# ── live round-trip (skipped on non-macOS without a usable keychain) ──


@pytest.mark.skipif(
    platform.system() != "Darwin",
    reason="Live keychain round-trip is only exercised on macOS hosts; "
    "Linux CI without a session bus would prompt for unlock.",
)
class TestLiveKeychainRoundTrip:
    """End-to-end set / list / get / delete against the real Keychain.

    Verifies that the hybrid (keyring + subprocess) wrapper actually
    enumerates accounts a *set* call produced, which is the property the
    pre-fix code broke.
    """

    ACCOUNTS = ["ic-test-rt-1", "ic-test-rt-2"]

    @pytest.fixture(autouse=True)
    def _cleanup(self):
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
