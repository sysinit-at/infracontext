"""Access tier definitions for infracontext triage operations."""

from enum import IntEnum


class AccessTier(IntEnum):
    """Access tiers controlling what diagnostic methods are available.

    Tiers are ordered from most restrictive to least restrictive.
    Using IntEnum allows comparisons like `tier >= AccessTier.UNPRIVILEGED`.
    """

    LOCAL_ONLY = 0  # Local data + observability APIs (Prometheus, Loki, CheckMK)
    COLLECTOR = 1  # + Execute pre-deployed collector script
    UNPRIVILEGED = 2  # + Arbitrary read-only SSH commands (no sudo)
    PRIVILEGED = 3  # + SSH with sudo/root
    REMEDIATE = 4  # + Autonomous fixes after diagnosis
