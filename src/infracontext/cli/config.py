"""Configuration commands."""

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
    password: Annotated[
        str | None, typer.Option("--password", "-p", help="Password (prompted if not provided)")
    ] = None,
) -> None:
    """Store a credential in the system keychain."""
    from infracontext.credentials.keychain import KeychainError, set_credential

    if password is None:
        password = typer.prompt("Password/Secret", hide_input=True)

    try:
        set_credential(account, password, label=f"infracontext: {account}")
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
