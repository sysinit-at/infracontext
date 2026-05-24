"""Credential management using system keychain.

Wraps the cross-platform ``keyring`` library for the secret-handling
operations (set/get/delete) — those call native APIs and never put the
secret on argv:
- macOS  : Security framework
- Linux  : libsecret (Secret Service / D-Bus)
- Windows: Credential Manager

For the metadata-only ``list`` operation, ``keyring`` does not expose
enumeration as part of its cross-platform contract. We fall back to a
small platform-specific subprocess call there — only account names are
read, never passwords — so operators auditing or rotating credentials
see what's actually stored.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from dataclasses import dataclass

import keyring
import keyring.errors

log = logging.getLogger(__name__)

SERVICE_NAME = "infracontext"


@dataclass
class Credential:
    """A stored credential."""

    account: str
    password: str


class KeychainError(Exception):
    """Error accessing the system keychain."""


def set_credential(account: str, password: str, label: str | None = None) -> None:
    """Store a credential in the system keychain.

    Args:
        account: Account identifier (e.g., ``"proxmox:prod:api-token"``).
        password: The secret to store.
        label: Ignored. Kept for API compatibility; backends don't
            consistently expose a separate label field through keyring.
    """
    del label  # intentionally unused
    try:
        keyring.set_password(SERVICE_NAME, account, password)
    except keyring.errors.PasswordSetError as e:
        raise KeychainError(f"Failed to store credential: {e}") from e
    except keyring.errors.KeyringError as e:
        raise KeychainError(f"Keychain error: {e}") from e


def get_credential(account: str) -> str | None:
    """Retrieve a credential from the system keychain.

    Returns the secret, or ``None`` if no entry exists for ``account``.
    """
    try:
        return keyring.get_password(SERVICE_NAME, account)
    except keyring.errors.KeyringError as e:
        raise KeychainError(f"Keychain error: {e}") from e


def delete_credential(account: str) -> bool:
    """Delete a credential. Returns True on success, False if not present."""
    try:
        keyring.delete_password(SERVICE_NAME, account)
        return True
    except keyring.errors.PasswordDeleteError:
        return False
    except keyring.errors.KeyringError as e:
        raise KeychainError(f"Keychain error: {e}") from e


def list_credentials() -> list[str]:
    """List all credential accounts stored under this service.

    The ``keyring`` library does not provide a portable enumeration API, so
    this falls back to a per-platform metadata read. Only account names
    leave the keychain — secrets are never inspected here.

    Returns the deduplicated, sorted list of accounts found. Raises
    :class:`KeychainError` on unsupported platforms (so callers can tell
    "list is unsupported here" apart from "no credentials stored").
    """
    system = platform.system()
    if system == "Darwin":
        return _list_credentials_macos()
    if system == "Linux":
        return _list_credentials_linux()
    raise KeychainError(
        f"Credential enumeration is not implemented for {system}. "
        f"Use 'credential get <account>' if you know the account name."
    )


def _list_credentials_macos() -> list[str]:
    """List accounts in the macOS Keychain for this service via ``security(1)``.

    ``security dump-keychain`` prints metadata only; no password is dumped.
    Entries are separated by ``keychain:`` header lines; within an entry the
    attribute order is not stable (real dumps put ``acct`` before ``svce``).
    We accumulate the candidate account *per entry* and only commit it when
    that entry's ``svce`` matches ours.
    """
    try:
        result = subprocess.run(
            ["security", "dump-keychain"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise KeychainError("'security' CLI not found on macOS") from e

    if result.returncode != 0:
        log.debug("security dump-keychain exited %d: %s", result.returncode, result.stderr)
        return []

    accounts: list[str] = []
    pending_acct: str | None = None
    matched_svce: bool = False

    def _flush() -> None:
        nonlocal pending_acct, matched_svce
        if matched_svce and pending_acct:
            accounts.append(pending_acct)
        pending_acct = None
        matched_svce = False

    for line in result.stdout.splitlines():
        # `keychain:` marks the start of a new entry; flush whatever we
        # accumulated for the previous one.
        if line.startswith("keychain:"):
            _flush()
            continue
        if '"acct"<blob>="' in line:
            start = line.find('"acct"<blob>="') + len('"acct"<blob>="')
            end = line.rfind('"')
            if start < end:
                pending_acct = line[start:end]
        elif f'"svce"<blob>="{SERVICE_NAME}"' in line:
            matched_svce = True
    _flush()  # last entry
    return sorted(set(accounts))


def _list_credentials_linux() -> list[str]:
    """List accounts in libsecret-backed keyrings via ``secret-tool search``.

    Only attribute metadata is returned, not values.
    """
    try:
        result = subprocess.run(
            ["secret-tool", "search", "--all", "service", SERVICE_NAME],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise KeychainError(
            "'secret-tool' (libsecret) not found. Install libsecret-tools to enumerate credentials."
        ) from e

    if result.returncode != 0:
        return []

    accounts: list[str] = []
    for line in result.stdout.splitlines():
        prefix = "attribute.account = "
        if line.startswith(prefix):
            accounts.append(line[len(prefix) :])
    return sorted(set(accounts))
