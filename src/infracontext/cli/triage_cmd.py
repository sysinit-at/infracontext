"""``ic triage`` -- serve the bundled diagnostic checker checklists.

The ``/ic-triage`` skill spawns named checker agents on Claude Code; on agents
without a subagent mechanism it falls back to running each checker's checklist
inline. This command makes that fallback self-contained: the checklists ship
with the installed CLI (wheel force-include, see pyproject), so they are
available even when only the skill file itself was copied into another agent's
command directory.
"""

import typer
from rich.console import Console

from infracontext.skill_data import list_skill_files

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command("checklist")
def checklist(
    name: str | None = typer.Argument(
        default=None,
        help="Checker name (e.g. ic-cpu-checker, with or without .md); omit to list all",
    ),
) -> None:
    """Print a bundled checker checklist, or list the available checkers.

    Without NAME: one checker name per line (machine-friendly). With NAME:
    the checker's full markdown on stdout, ready to be followed inline by an
    agent that cannot spawn subagents.
    """
    files = list_skill_files("agents")
    if not files:
        console.print(
            "[red]No bundled checker definitions found -- broken installation? "
            "Expected them next to the package (infracontext/data/agents) or in "
            "the repository root (agents/).[/red]"
        )
        raise typer.Exit(1)

    if name is None:
        for path in files:
            print(path.stem)
        return

    wanted = name.removesuffix(".md")
    for path in files:
        if path.stem == wanted:
            print(path.read_text(encoding="utf-8"))
            return

    available = ", ".join(path.stem for path in files)
    console.print(f"[red]Unknown checker '{name}'. Available: {available}[/red]")
    raise typer.Exit(1)
