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
import platform
import subprocess
import sys
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

    Upgrade note: if the index file does not exist and we're on a
    platform that *can* enumerate the keychain safely (macOS via
    ``security dump-keychain``, which is metadata-only), emit a one-time
    warning to stderr so upgraders don't mistake empty output for
    "nothing stored." Run ``ic config credential migrate`` to backfill.

    Note: credentials added directly to the system keychain outside of
    ``ic`` are intentionally NOT listed. The keychain is the source of
    truth for secret values; the index is the source of truth for "what
    did ``ic`` put there." See module docstring for rationale.
    """
    index_path = _index_path()
    if not index_path.exists():
        # Upgraders may have credentials in the keychain that aren't in the
        # new index. Surface this once so they know to migrate.
        if platform.system() == "Darwin":
            print(
                "warning: credential index not found at "
                f"{index_path}. If you previously stored credentials with an "
                "older version of ic, they may exist in the keychain but not "
                "in this list. Run 'ic config credential migrate' to backfill.",
                file=sys.stderr,
            )
        return []

    try:
        return sorted(_index_read())
    except (OSError, ValueError) as e:
        raise KeychainError(f"Failed to read credential index: {e}") from e


# ── migration (one-shot backfill from system keychain) ───────────


def migrate_from_keychain() -> list[str]:
    """Discover account names already in the system keychain and add them
    to the metadata index.

    Only macOS is supported because ``security dump-keychain`` is the only
    enumeration path that *doesn't* materialize secret values. On other
    platforms this raises :class:`KeychainError` — operators there must
    re-run ``set`` for each known account name (the index will populate
    from those calls).

    Returns the list of accounts that were newly added to the index (i.e.
    discovered in the keychain but not already indexed). Safe to re-run.
    """
    system = platform.system()
    if system != "Darwin":
        raise KeychainError(
            f"Credential migration from the keychain is only supported on "
            f"macOS ('{system}' has no metadata-only enumeration). Re-run "
            "'ic config credential set <name>' for each account to populate "
            "the index."
        )

    discovered = _enumerate_macos_keychain()
    if not discovered:
        return []

    existing = _index_read() if _index_path().exists() else set()
    new_accounts = sorted(discovered - existing)
    if new_accounts:
        _index_write(existing | discovered)
    return new_accounts


def _enumerate_macos_keychain() -> set[str]:
    """Read account names from macOS Keychain entries with svce == infracontext.

    ``security dump-keychain`` prints attribute metadata only; no password
    is dumped. Entries are separated by ``keychain:`` header lines; within
    an entry the attribute order is not stable (real dumps put ``acct``
    before ``svce``), so we accumulate the candidate account *per entry*
    and only commit it when that entry's ``svce`` matches ours.
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
        return set()

    accounts: set[str] = set()
    pending_acct: str | None = None
    matched_svce: bool = False

    def _flush() -> None:
        nonlocal pending_acct, matched_svce
        if matched_svce and pending_acct:
            accounts.add(pending_acct)
        pending_acct = None
        matched_svce = False

    for line in result.stdout.splitlines():
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
    return accounts


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
