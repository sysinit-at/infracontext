"""Credential management using system keychain.

Wraps the cross-platform ``keyring`` library, which calls native APIs:
- macOS  : Security framework (no subprocess; password never on argv)
- Linux  : libsecret (Secret Service / D-Bus)
- Windows: Credential Manager

This module previously shelled out to ``security`` / ``secret-tool``, which
exposed the secret on the command line briefly (visible to ``ps``). The
keyring library reads/writes credentials through C bindings instead, so the
secret stays in process memory only.
"""

from __future__ import annotations

from dataclasses import dataclass

import keyring
import keyring.errors

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

    Uses the ``get_credential`` Python API on backends that support it
    (macOS, libsecret). Returns an empty list when the active backend does
    not provide enumeration.
    """
    try:
        credentials = keyring.get_credential(SERVICE_NAME, None)
    except keyring.errors.KeyringError as e:
        raise KeychainError(f"Keychain error: {e}") from e

    # ``get_credential(service, None)`` returns a single credential on most
    # backends; full enumeration isn't part of the cross-platform contract.
    # We expose what we can: the accounts the backend reports back.
    if credentials is None:
        return []
    # Some backends return a SimpleCredential with a single .username; others
    # return the first matching entry. We can't enumerate the rest portably.
    username = getattr(credentials, "username", None)
    return [username] if username else []
