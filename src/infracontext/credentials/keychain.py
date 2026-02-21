"""Credential management using system keychain.

On macOS: Uses the Keychain via the `security` command
On Linux: Uses secret-tool (libsecret)
"""

import platform
import subprocess
from dataclasses import dataclass

SERVICE_NAME = "infracontext"


@dataclass
class Credential:
    """A stored credential."""

    account: str
    password: str


class KeychainError(Exception):
    """Error accessing the system keychain."""


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _is_linux() -> bool:
    return platform.system() == "Linux"


def set_credential(account: str, password: str, label: str | None = None) -> None:
    """Store a credential in the system keychain.

    Args:
        account: Account identifier (e.g., "proxmox:prod:api-token")
        password: The secret to store
        label: Optional human-readable label
    """
    if _is_macos():
        _set_credential_macos(account, password, label)
    elif _is_linux():
        _set_credential_linux(account, password, label)
    else:
        raise KeychainError(f"Unsupported platform: {platform.system()}")


def get_credential(account: str) -> str | None:
    """Retrieve a credential from the system keychain.

    Args:
        account: Account identifier

    Returns:
        The password/secret, or None if not found
    """
    if _is_macos():
        return _get_credential_macos(account)
    elif _is_linux():
        return _get_credential_linux(account)
    else:
        raise KeychainError(f"Unsupported platform: {platform.system()}")


def delete_credential(account: str) -> bool:
    """Delete a credential from the system keychain.

    Args:
        account: Account identifier

    Returns:
        True if deleted, False if not found
    """
    if _is_macos():
        return _delete_credential_macos(account)
    elif _is_linux():
        return _delete_credential_linux(account)
    else:
        raise KeychainError(f"Unsupported platform: {platform.system()}")


def list_credentials() -> list[str]:
    """List all credential accounts for this service.

    Returns:
        List of account identifiers
    """
    if _is_macos():
        return _list_credentials_macos()
    elif _is_linux():
        return _list_credentials_linux()
    else:
        raise KeychainError(f"Unsupported platform: {platform.system()}")


# ============================================
# macOS Implementation (Keychain)
# ============================================


def _set_credential_macos(account: str, password: str, label: str | None = None) -> None:
    """Store credential in macOS Keychain."""
    # Delete existing if present (update)
    _delete_credential_macos(account)

    cmd = [
        "security",
        "add-generic-password",
        "-a",
        account,
        "-s",
        SERVICE_NAME,
        "-w",
        password,
        "-U",  # Update if exists
    ]
    if label:
        cmd.extend(["-l", label])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise KeychainError(f"Failed to store credential: {result.stderr}")


def _get_credential_macos(account: str) -> str | None:
    """Retrieve credential from macOS Keychain."""
    cmd = [
        "security",
        "find-generic-password",
        "-a",
        account,
        "-s",
        SERVICE_NAME,
        "-w",  # Output only the password
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _delete_credential_macos(account: str) -> bool:
    """Delete credential from macOS Keychain."""
    cmd = [
        "security",
        "delete-generic-password",
        "-a",
        account,
        "-s",
        SERVICE_NAME,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def _list_credentials_macos() -> list[str]:
    """List credentials in macOS Keychain."""
    cmd = [
        "security",
        "dump-keychain",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return []

    accounts = []
    lines = result.stdout.split("\n")
    in_service = False

    for line in lines:
        if f'"svce"<blob>="{SERVICE_NAME}"' in line:
            in_service = True
        elif in_service and '"acct"<blob>="' in line:
            # Extract account name
            start = line.find('"acct"<blob>="') + len('"acct"<blob>="')
            end = line.rfind('"')
            if start < end:
                accounts.append(line[start:end])
            in_service = False

    return accounts


# ============================================
# Linux Implementation (secret-tool / libsecret)
# ============================================


def _set_credential_linux(account: str, password: str, label: str | None = None) -> None:
    """Store credential using secret-tool."""
    label = label or f"{SERVICE_NAME}: {account}"
    cmd = [
        "secret-tool",
        "store",
        "--label",
        label,
        "service",
        SERVICE_NAME,
        "account",
        account,
    ]
    result = subprocess.run(cmd, input=password, capture_output=True, text=True)
    if result.returncode != 0:
        raise KeychainError(f"Failed to store credential: {result.stderr}")


def _get_credential_linux(account: str) -> str | None:
    """Retrieve credential using secret-tool."""
    cmd = [
        "secret-tool",
        "lookup",
        "service",
        SERVICE_NAME,
        "account",
        account,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _delete_credential_linux(account: str) -> bool:
    """Delete credential using secret-tool."""
    cmd = [
        "secret-tool",
        "clear",
        "service",
        SERVICE_NAME,
        "account",
        account,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0


def _list_credentials_linux() -> list[str]:
    """List credentials using secret-tool.

    Note: secret-tool doesn't have a direct list command, so we use search.
    This may not work on all systems.
    """
    cmd = [
        "secret-tool",
        "search",
        "--all",
        "service",
        SERVICE_NAME,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return []

    accounts = []
    for line in result.stdout.split("\n"):
        if line.startswith("attribute.account = "):
            account = line[len("attribute.account = ") :]
            accounts.append(account)

    return accounts
