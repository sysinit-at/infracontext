"""Credential management using the system keychain.

**Secret-handling path** (set / get / delete) goes through the cross-platform
``keyring`` library, which calls native APIs and never puts the secret on
argv:

- macOS  : Security framework
- Linux  : libsecret (Secret Service / D-Bus)
- Windows: Credential Manager

**Listing** is not part of ``keyring``'s portable contract, and the obvious
fallbacks each have a problem: ``security dump-keychain`` is brittle to
parse, and ``secret-tool search --all`` *decrypts* every matching item to
stdout — pulling all secrets through our process even when we only want
account names.

To support audit/rotation without those gotchas, we maintain a small
metadata-only index of account names that ``set_credential`` and
``delete_credential`` keep in sync. ``list_credentials`` reads from the
index. Credentials added to the keychain *outside* ``ic`` won't appear in
``list`` — that matches the tool-scoped meaning users expect from
``ic config credential list``.

Index location: ``$INFRACONTEXT_CREDENTIALS_INDEX`` if set, else
``$XDG_CONFIG_HOME/infracontext/credentials-index.json``, else
``~/.config/infracontext/credentials-index.json``. The file contains only
account names; no secrets are ever written to it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import keyring
import keyring.errors

log = logging.getLogger(__name__)

SERVICE_NAME = "infracontext"
_INDEX_ENV_VAR = "INFRACONTEXT_CREDENTIALS_INDEX"


@dataclass
class Credential:
    """A stored credential."""

    account: str
    password: str


class KeychainError(Exception):
    """Error accessing the system keychain or its account index."""


# ── secret-handling (keyring) ──────────────────────────────────────


def set_credential(account: str, password: str, label: str | None = None) -> None:
    """Store a credential in the system keychain.

    Args:
        account: Account identifier (e.g. ``"proxmox:prod:api-token"``).
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

    # Update the metadata index after the secret is safely stored.
    # If the index write fails, the secret is still in the keychain — log
    # so an operator can recover, but don't roll back the working keychain
    # entry (re-running `set` will reconcile).
    try:
        _index_add(account)
    except OSError as e:
        log.warning("credential stored, but failed to update account index: %s", e)


def get_credential(account: str) -> str | None:
    """Retrieve a credential. Returns ``None`` if no entry exists."""
    try:
        return keyring.get_password(SERVICE_NAME, account)
    except keyring.errors.KeyringError as e:
        raise KeychainError(f"Keychain error: {e}") from e


def delete_credential(account: str) -> bool:
    """Delete a credential. Returns True on success, False if not present."""
    deleted = True
    try:
        keyring.delete_password(SERVICE_NAME, account)
    except keyring.errors.PasswordDeleteError:
        deleted = False
    except keyring.errors.KeyringError as e:
        raise KeychainError(f"Keychain error: {e}") from e

    # Always try to drop it from the index too — covers the case where the
    # keychain entry was already gone but the index still listed it.
    try:
        _index_remove(account)
    except OSError as e:
        log.warning("failed to update account index after delete: %s", e)
    return deleted


def list_credentials() -> list[str]:
    """List all credential accounts known to ``ic``.

    Reads from the metadata index. Returns an empty list if the index
    doesn't exist yet (e.g. nothing has been stored). Account names only;
    no secrets are touched.

    Note: credentials added directly to the system keychain outside of
    ``ic`` are intentionally NOT listed. The keychain is the source of
    truth for secret values; the index is the source of truth for "what
    did ``ic`` put there." See module docstring for rationale.
    """
    try:
        return sorted(_index_read())
    except (OSError, ValueError) as e:
        raise KeychainError(f"Failed to read credential index: {e}") from e


# ── account index ──────────────────────────────────────────────────


def _index_path() -> Path:
    """Resolve the credential-index file path.

    Order:
        1. ``$INFRACONTEXT_CREDENTIALS_INDEX`` (used by tests; also a hook
           for operators that want to relocate it).
        2. ``$XDG_CONFIG_HOME/infracontext/credentials-index.json``
        3. ``~/.config/infracontext/credentials-index.json``
    """
    override = os.environ.get(_INDEX_ENV_VAR)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "infracontext" / "credentials-index.json"


def _index_read() -> set[str]:
    path = _index_path()
    if not path.exists():
        return set()
    raw = json.loads(path.read_text(encoding="utf-8"))
    accounts = raw.get("accounts") if isinstance(raw, dict) else None
    if not isinstance(accounts, list):
        raise ValueError(f"unexpected index format in {path}")
    return {a for a in accounts if isinstance(a, str)}


def _index_write(accounts: set[str]) -> None:
    """Atomic write so a concurrent read never sees a half-written file."""
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"accounts": sorted(accounts)}, indent=2) + "\n"
    fd, tmp_str = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _index_add(account: str) -> None:
    accounts = _index_read() if _index_path().exists() else set()
    if account in accounts:
        return
    accounts.add(account)
    _index_write(accounts)


def _index_remove(account: str) -> None:
    if not _index_path().exists():
        return
    accounts = _index_read()
    if account not in accounts:
        return
    accounts.discard(account)
    _index_write(accounts)
