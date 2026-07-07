"""Configuration commands."""

import re
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from infracontext.config import load_config
from infracontext.paths import EnvironmentNotFoundError, EnvironmentPaths

app = typer.Typer(no_args_is_help=True)
console = Console()

credential_app = typer.Typer(help="Manage credentials in system keychain")
app.add_typer(credential_app, name="credential")

env_app = typer.Typer(help="Manage the global environment registry (reach envs from anywhere)")
app.add_typer(env_app, name="env")

_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@app.command("show")
def config_show() -> None:
    """Show current configuration."""
    try:
        environment = EnvironmentPaths.current()
        config = load_config(environment)

        console.print("[bold]Configuration[/bold]")
        console.print()
        console.print(f"  [dim]Environment root:[/dim] {environment.root}")
        console.print(f"  [dim]Config file:[/dim] {environment.config_yaml}")
        console.print(f"  [dim]Local overrides:[/dim] {environment.local_overrides}")
        console.print()
        console.print(f"  [cyan]Active project:[/cyan] {config.active_project or '(none)'}")
    except EnvironmentNotFoundError:
        console.print("[red]Not in an infracontext environment. Run 'ic init' first.[/red]")
        raise typer.Exit(1) from None


@credential_app.command("set")
def credential_set(
    account: Annotated[str, typer.Argument(help="Account identifier (e.g., 'proxmox:prod')")],
) -> None:
    """Store a credential in the system keychain.

    The secret is read from stdin (or interactively if a TTY is attached).
    There is intentionally no CLI flag for the password: passing it as an
    argument would leak it to shell history and to ``ps``.
    """
    import sys

    from infracontext.credentials.keychain import KeychainError, set_credential

    if sys.stdin.isatty():
        password = typer.prompt("Password/Secret", hide_input=True)
    else:
        # Piped input: read a single line and strip the trailing newline.
        password = sys.stdin.readline().rstrip("\n")

    if not password:
        console.print("[red]Empty password; refusing to store.[/red]")
        raise typer.Exit(1) from None

    try:
        set_credential(account, password)
        console.print(f"[green]Stored credential for '{account}'[/green]")
    except KeychainError as e:
        console.print(f"[red]Failed to store credential: {e}[/red]")
        raise typer.Exit(1) from None


@credential_app.command("get")
def credential_get(
    account: Annotated[str, typer.Argument(help="Account identifier")],
    show: Annotated[bool, typer.Option("--show", "-s", help="Show the password")] = False,
) -> None:
    """Check if a credential exists (optionally show it)."""
    from infracontext.credentials.keychain import get_credential

    password = get_credential(account)
    if password is None:
        console.print(f"[yellow]No credential found for '{account}'[/yellow]")
        raise typer.Exit(1)

    if show:
        console.print(f"[green]{account}:[/green] {password}")
    else:
        console.print(f"[green]Credential exists for '{account}' (use --show to reveal)[/green]")


@credential_app.command("list")
def credential_list() -> None:
    """List all stored credentials."""
    from infracontext.credentials.keychain import KeychainError, list_credentials

    try:
        accounts = list_credentials()
    except KeychainError as e:
        console.print(f"[red]Failed to list credentials: {e}[/red]")
        raise typer.Exit(1) from None

    if not accounts:
        console.print("[dim]No credentials stored.[/dim]")
        return

    table = Table(title="Stored Credentials")
    table.add_column("Account", style="cyan")

    for account in sorted(accounts):
        table.add_row(account)

    console.print(table)


@credential_app.command("delete")
def credential_delete(
    account: Annotated[str, typer.Argument(help="Account identifier")],
    force: Annotated[bool, typer.Option("--force", "-f", help="Skip confirmation")] = False,
) -> None:
    """Delete a credential from the system keychain."""
    from infracontext.credentials.keychain import delete_credential

    if not force:
        confirm = typer.confirm(f"Delete credential for '{account}'?")
        if not confirm:
            raise typer.Abort()

    if delete_credential(account):
        console.print(f"[green]Deleted credential for '{account}'[/green]")
    else:
        console.print(f"[yellow]No credential found for '{account}'[/yellow]")


@credential_app.command("migrate")
def credential_migrate() -> None:
    """Backfill the credential index from the system keychain.

    Use this once after upgrading from a version of ``ic`` that stored
    credentials in the keychain without the metadata index. macOS only —
    on other platforms there is no enumeration path that can run safely
    without decrypting secrets, so the migration is intentionally not
    supported there. Re-run ``credential set <name>`` for each account
    on those platforms instead.
    """
    from infracontext.credentials.keychain import KeychainError, migrate_from_keychain

    try:
        added = migrate_from_keychain()
    except KeychainError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None

    if not added:
        console.print(
            "[dim]No new accounts to backfill (index already in sync with the keychain).[/dim]"
        )
        return

    console.print(f"[green]Added {len(added)} account(s) to the credential index:[/green]")
    for acct in added:
        console.print(f"  - {acct}")


# ============================================
# Environment registry commands
# ============================================


@env_app.command("list")
def env_list() -> None:
    """List registered environments (with which is default and still valid)."""
    from infracontext.envregistry import is_valid_environment, load_registry, registry_path

    registry = load_registry()
    if not registry.environments:
        console.print("[dim]No environments registered.[/dim]")
        console.print("[dim]Add one with: ic config env add <name> <path> --default[/dim]")
        console.print(f"[dim]Registry file: {registry_path()}[/dim]")
        return

    table = Table(title="Registered environments")
    table.add_column("Name", style="cyan")
    table.add_column("Path")
    table.add_column("Default", style="green")
    table.add_column("Valid")

    for name, path in sorted(registry.environments.items()):
        is_default = "*" if name == registry.default else ""
        valid = "[green]yes[/green]" if is_valid_environment(Path(path).expanduser()) else "[red]no[/red]"
        table.add_row(name, path, is_default, valid)

    console.print(table)


@env_app.command("add")
def env_add(
    name: Annotated[str, typer.Argument(help="Short name for the environment")],
    path: Annotated[Path, typer.Argument(help="Path to the environment root (contains .infracontext/)")],
    make_default: Annotated[bool, typer.Option("--default", "-d", help="Set this as the default environment")] = False,
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Register even if the path has no .infracontext/ yet")
    ] = False,
) -> None:
    """Register an environment so it can be reached from anywhere.

    Once registered as the default (or with IC_ROOT), ``ic`` commands resolve
    the environment even when run outside its directory.
    """
    from infracontext.envregistry import add_environment, is_valid_environment, resolve_environment_path

    clean = name.strip()
    if not _ENV_NAME_RE.fullmatch(clean):
        console.print(
            f"[red]Invalid environment name '{name}'. Use letters, digits, '.', '_', and '-' "
            "(starting with a letter or digit).[/red]"
        )
        raise typer.Exit(1)

    resolved = resolve_environment_path(path)
    if not is_valid_environment(resolved) and not force:
        console.print(f"[red]{resolved} does not contain a .infracontext/ directory.[/red]")
        console.print("[dim]Run 'ic init' there first, or pass --force to register it anyway.[/dim]")
        raise typer.Exit(1)

    add_environment(clean, resolved, make_default=make_default)
    console.print(f"[green]Registered environment '{clean}' -> {resolved}[/green]")
    if make_default:
        console.print(f"[dim]'{clean}' is now the default environment.[/dim]")


@env_app.command("default")
def env_default(
    name: Annotated[str, typer.Argument(help="Environment name to make the default")],
) -> None:
    """Set the default environment used when outside any repo."""
    from infracontext.envregistry import load_registry, set_default

    registry = load_registry()
    if name not in registry.environments:
        console.print(f"[red]No environment named '{name}'.[/red]")
        if registry.environments:
            console.print(f"[dim]Known: {', '.join(sorted(registry.environments))}[/dim]")
        raise typer.Exit(1)

    set_default(name)
    console.print(f"[green]Default environment set to '{name}'[/green]")


@env_app.command("remove")
def env_remove(
    name: Annotated[str, typer.Argument(help="Environment name to remove")],
) -> None:
    """Remove an environment from the registry (does not touch its data)."""
    from infracontext.envregistry import remove_environment

    if remove_environment(name):
        console.print(f"[green]Removed environment '{name}'[/green]")
    else:
        console.print(f"[yellow]No environment named '{name}'.[/yellow]")
        raise typer.Exit(1)
