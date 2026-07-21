"""Locate the bundled agent skill/checker markdown, wherever ic is installed.

The repo keeps ``agents/`` (diagnostic checker definitions) and ``commands/``
(triage/collect skills) at the repository root, where the Claude Code symlink
instructions expect them. Wheel builds additionally force-include both dirs
under ``infracontext/data/`` (see pyproject) so a plain ``uv tool install``
carries them too.

This module resolves whichever location exists, letting ``ic triage
checklist`` serve the checker checklists in EVERY advertised installation --
including ones where only a single skill/prompt file was copied to another
agent's command directory and the repo checkout is not available. That is the
substrate for the triage skill's no-subagent inline fallback.
"""

from __future__ import annotations

from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent

# (packaged wheel location, dev-checkout location) per kind.
_CANDIDATES = {
    "agents": (_PACKAGE_DIR / "data" / "agents", _PACKAGE_DIR.parents[1] / "agents"),
    "commands": (_PACKAGE_DIR / "data" / "commands", _PACKAGE_DIR.parents[1] / "commands"),
}


def skill_data_dir(kind: str) -> Path | None:
    """Directory holding the bundled ``agents``/``commands`` markdown, or None.

    Prefers the wheel-packaged copy (``infracontext/data/<kind>``), falling
    back to the repository root for editable/dev checkouts. Returns None when
    neither exists (a broken install) -- callers degrade with a clear error
    instead of crashing.
    """
    for candidate in _CANDIDATES.get(kind, ()):
        if candidate.is_dir():
            return candidate
    return None


def list_skill_files(kind: str) -> list[Path]:
    """Sorted markdown files of the given kind (empty when unresolvable)."""
    directory = skill_data_dir(kind)
    if directory is None:
        return []
    return sorted(directory.glob("*.md"))
