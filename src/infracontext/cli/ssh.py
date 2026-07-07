"""``ic ssh <query> [command...]`` -- the incident hot path.

Resolves a node fuzzily, prints a short context banner to *stderr* (so piping
the remote command's stdout stays clean), then ``exec``s ``ssh``. The banner is
strictly best-effort: any failure assembling it degrades to a plain connection
rather than blocking the operator from reaching the box.
"""

from __future__ import annotations

import os
from contextlib import suppress

import typer
from rich.console import Console

# Banner goes to stderr; a separate console keeps it off stdout so
# `ic ssh web cat /etc/hosts > hosts.txt` captures only the remote output.
_err = Console(stderr=True)
console = Console()


def _ssh_target(node) -> tuple[str | None, str | None]:
    """Pick the SSH target for a node.

    Returns ``(target, field)`` where ``field`` names which node field the
    target came from, or ``(None, None)`` if the node has no usable target.
    Preference mirrors the rest of the tool: ssh_alias, then first domain,
    then first IP.
    """
    if node.ssh_alias:
        return node.ssh_alias, "ssh_alias"
    if node.domains:
        return node.domains[0], "domains"
    if node.ip_addresses:
        return node.ip_addresses[0], "ip_addresses"
    return None, None


def _count_direct_dependents(target) -> int:
    """Count nodes that depend on this node, cheaply.

    Reads only the project's ``relationships.yaml`` (no graph build, no
    networkx) and counts relationships whose *target* is this node -- i.e. the
    "source depends on target" edges that make this node a dependency. Returns
    0 on any read/parse problem; the banner never fails the connection.
    """
    from infracontext.models.relationship import RelationshipFile, parse_node_ref
    from infracontext.storage import read_model

    rel_file = read_model(target.paths.relationships_yaml, RelationshipFile)
    if not rel_file:
        return 0

    count = 0
    for rel in rel_file.relationships:
        try:
            _scope, node_id = parse_node_ref(rel.target, target.project)
        except ValueError:
            continue
        if node_id == target.node_id:
            count += 1
    return count


def _print_banner(node, target) -> None:
    """Print a <=5 line dim context banner to stderr. Best-effort only."""
    lines: list[str] = [f"[dim]● {node.id}  {node.name}[/dim]"]

    if node.triage:
        bits: list[str] = []
        if node.triage.services:
            bits.append("services: " + ", ".join(node.triage.services))
        if node.triage.context:
            bits.append(node.triage.context)
        if bits:
            lines.append(f"[dim]  {' — '.join(bits)}[/dim]")

    if node.learnings:
        last = node.learnings[-1]
        finding = last.finding
        if len(finding) > 100:
            finding = finding[:97] + "..."
        lines.append(f"[dim]  last learning ({last.date}): {finding}[/dim]")

    dependents = _count_direct_dependents(target)
    if dependents:
        noun = "dependent" if dependents == 1 else "dependents"
        lines.append(f"[dim]  {dependents} direct {noun} — docs: ic ctx {node.id}[/dim]")
    else:
        lines.append(f"[dim]  docs: ic ctx {node.id}[/dim]")

    for line in lines[:5]:
        _err.print(line)


def run_ssh(query: str, *, command_args: list[str], no_banner: bool) -> None:
    """Resolve ``query`` and exec ``ssh`` to it, optionally running a command."""
    from infracontext.cli.describe import (
        _node_file_from_id_or_exit,
        read_node_with_overrides,
    )
    from infracontext.cli.resolve import resolve_node_or_exit

    target = resolve_node_or_exit(query)
    node_file = _node_file_from_id_or_exit(target.paths, target.node_id)
    if not node_file.exists():
        console.print(f"[red]Node '{target.node_id}' not found.[/red]")
        raise typer.Exit(1)

    node = read_node_with_overrides(node_file, target.environment, target.project)
    if not node:
        console.print(f"[red]Failed to read node '{target.node_id}'.[/red]")
        raise typer.Exit(1)

    ssh_target, _field = _ssh_target(node)
    if not ssh_target:
        console.print(f"[red]No SSH target for node '{node.id}'.[/red]")
        console.print(
            "[dim]Checked ssh_alias, domains, and ip_addresses — all empty. "
            f"Set one with: ic describe node edit {node.id}[/dim]"
        )
        raise typer.Exit(1)

    # A target like "-oProxyCommand=..." would be parsed by ssh as an option and
    # execute arbitrary commands; node data may come from an untrusted federated
    # repo. Reject leading-dash targets and place "--" before the target below.
    if ssh_target.startswith("-"):
        console.print(
            f"[red]Refusing SSH target '{ssh_target}': a leading '-' would be parsed "
            "as an ssh option.[/red]"
        )
        console.print(f"[dim]Fix the node's ssh_alias/domain/IP: ic describe node edit {node.id}[/dim]")
        raise typer.Exit(1)

    if not no_banner:
        # The banner is best-effort: it must never stop the operator reaching
        # the machine, so any assembly failure is swallowed.
        with suppress(Exception):
            _print_banner(node, target)

    # os.execvp replaces this process on success; only failure paths return.
    argv = ["ssh", "--", ssh_target, *command_args]
    try:
        os.execvp("ssh", argv)
    except FileNotFoundError:
        console.print("[red]ssh not found on PATH.[/red]")
        raise typer.Exit(1) from None
    except OSError as e:  # pragma: no cover - exec failure is environment-specific
        console.print(f"[red]Failed to exec ssh: {e}[/red]")
        raise typer.Exit(1) from None
