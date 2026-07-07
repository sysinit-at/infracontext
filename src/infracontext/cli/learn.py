"""``ic learn <query> [finding]`` -- the human shortcut for recording a learning.

Mirrors ``ic describe node learning`` but defaults the source to ``human`` (the
describe command keeps its ``agent`` default; the asymmetry is intentional --
``ic learn`` is what a person types). With no finding argument it opens
``$EDITOR`` on a commented template.
"""

from __future__ import annotations

import re
from pathlib import Path

import typer
from rich.console import Console

console = Console()

_DEFAULT_CONTEXT = "manual note"


def _parse_template(text: str) -> tuple[str, str]:
    """Extract ``(finding, context)`` from an edited template.

    Non-comment lines form the finding; a ``# context: ...`` comment sets the
    context (falling back to the default). Comment lines are otherwise ignored.
    """
    context = _DEFAULT_CONTEXT
    finding_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            m = re.match(r"#\s*context:\s*(.*)", stripped)
            if m and m.group(1).strip():
                context = m.group(1).strip()
            continue
        if stripped:
            finding_lines.append(stripped)
    return " ".join(finding_lines).strip(), context


def _edit_learning(node_id: str, context: str) -> tuple[str, str] | None:
    """Open ``$EDITOR`` on a template and return ``(finding, context)``.

    Returns ``None`` if the resulting finding is empty (operator aborted).
    """
    import os
    import shlex
    import subprocess
    import tempfile

    template = (
        "\n"
        f"# ic learn — record a finding for {node_id}.\n"
        "# Write the finding above; lines starting with '#' are ignored.\n"
        "# Saving without a finding aborts without writing.\n"
        f"# context: {context}\n"
    )

    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as tf:
        tf.write(template)
        tmp_path = Path(tf.name)
    try:
        subprocess.run([*shlex.split(editor), str(tmp_path)], check=False)
        text = tmp_path.read_text(encoding="utf-8")
    finally:
        tmp_path.unlink(missing_ok=True)

    finding, parsed_context = _parse_template(text)
    if not finding:
        return None
    return finding, parsed_context


def run_learn(query: str, finding: str | None, context: str) -> None:
    """Resolve a node fuzzily and append a human learning to it."""
    from infracontext.cli.describe import append_learning
    from infracontext.cli.resolve import resolve_node_or_exit

    target = resolve_node_or_exit(query, require_writable=True)

    if finding is None:
        result = _edit_learning(target.node_id, context)
        if result is None:
            console.print("[yellow]No finding entered — nothing recorded.[/yellow]")
            raise typer.Exit(0)
        finding, context = result

    append_learning(target, finding=finding, context=context, source="human")
