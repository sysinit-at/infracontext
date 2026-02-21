"""Health check and validation for infracontext data."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from infracontext.models.node import COMPUTE_NODE_TYPES, Node
from infracontext.models.project import ProjectConfig
from infracontext.models.relationship import Relationship, RelationshipFile
from infracontext.paths import EnvironmentNotFoundError, EnvironmentPaths, ProjectPaths, list_projects

app = typer.Typer(name="doctor", help="Validate infrastructure data")
console = Console()
_yaml = YAML()


class Severity(StrEnum):
    """Issue severity levels."""

    ERROR = "error"  # Invalid data, will cause failures
    WARNING = "warning"  # Missing recommended info
    INFO = "info"  # Suggestions for improvement


@dataclass
class Issue:
    """A validation issue found during health check."""

    severity: Severity
    category: str
    file: Path | None
    message: str
    suggestion: str | None = None


@dataclass
class DoctorReport:
    """Validation results from doctor check."""

    issues: list[Issue] = field(default_factory=list)
    files_checked: int = 0
    nodes_checked: int = 0
    relationships_checked: int = 0
    projects_checked: int = 0

    def add(
        self,
        severity: Severity,
        category: str,
        message: str,
        file: Path | None = None,
        suggestion: str | None = None,
    ) -> None:
        self.issues.append(Issue(severity, category, file, message, suggestion))

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.WARNING)

    @property
    def info_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == Severity.INFO)

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0


def _check_yaml_syntax(path: Path, report: DoctorReport) -> dict | None:
    """Check YAML syntax and return parsed data if valid."""
    try:
        with path.open("r") as f:
            data = _yaml.load(f)
            return dict(data) if data else {}
    except YAMLError as e:
        report.add(
            Severity.ERROR,
            "syntax",
            f"YAML syntax error: {e}",
            file=path,
        )
        return None
    except Exception as e:
        report.add(
            Severity.ERROR,
            "syntax",
            f"Failed to read file: {e}",
            file=path,
        )
        return None


def _check_node(path: Path, data: dict, report: DoctorReport) -> Node | None:
    """Validate a node file against the schema."""
    try:
        node = Node.model_validate(data)
        report.nodes_checked += 1

        # Check for missing ssh_alias on compute nodes
        if node.type in COMPUTE_NODE_TYPES and not node.ssh_alias:
            report.add(
                Severity.WARNING,
                "missing_info",
                f"Compute node '{node.id}' has no ssh_alias",
                file=path,
                suggestion="Add ssh_alias for SSH-based operations",
            )

        # Check for empty description
        if not node.description and not node.notes:
            report.add(
                Severity.INFO,
                "missing_info",
                f"Node '{node.id}' has no description or notes",
                file=path,
                suggestion="Consider adding a description for documentation",
            )

        # Check for observability config on compute nodes
        if node.type in COMPUTE_NODE_TYPES and not node.observability:
            report.add(
                Severity.INFO,
                "missing_info",
                f"Compute node '{node.id}' has no observability config",
                file=path,
                suggestion="Add prometheus/loki/monit config for 'ic query' support",
            )

        return node
    except ValidationError as e:
        for error in e.errors():
            loc = ".".join(str(x) for x in error["loc"])
            report.add(
                Severity.ERROR,
                "schema",
                f"Validation error at '{loc}': {error['msg']}",
                file=path,
            )
        return None


def _check_project_config(path: Path, data: dict, report: DoctorReport) -> ProjectConfig | None:
    """Validate a project config file."""
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as e:
        for error in e.errors():
            loc = ".".join(str(x) for x in error["loc"])
            report.add(
                Severity.ERROR,
                "schema",
                f"Validation error at '{loc}': {error['msg']}",
                file=path,
            )
        return None


def _check_relationships(
    path: Path,
    data: dict,
    all_node_ids: set[str],
    report: DoctorReport,
) -> list[Relationship]:
    """Validate relationships file and check for orphaned references."""
    relationships: list[Relationship] = []

    try:
        rel_file = RelationshipFile.model_validate(data)
        relationships = rel_file.relationships
        report.relationships_checked += len(relationships)
    except ValidationError as e:
        for error in e.errors():
            loc = ".".join(str(x) for x in error["loc"])
            report.add(
                Severity.ERROR,
                "schema",
                f"Validation error at '{loc}': {error['msg']}",
                file=path,
            )
        return []

    # Check for orphaned relationships (references to non-existent nodes)
    for rel in relationships:
        if rel.source not in all_node_ids:
            report.add(
                Severity.ERROR,
                "orphan",
                f"Relationship references non-existent source node: '{rel.source}'",
                file=path,
                suggestion=f"Remove relationship or create node '{rel.source}'",
            )
        if rel.target not in all_node_ids:
            report.add(
                Severity.ERROR,
                "orphan",
                f"Relationship references non-existent target node: '{rel.target}'",
                file=path,
                suggestion=f"Remove relationship or create node '{rel.target}'",
            )

    # Check for duplicate relationships
    seen: set[tuple[str, str, str]] = set()
    for rel in relationships:
        key = (rel.source, rel.type, rel.target)
        if key in seen:
            report.add(
                Severity.WARNING,
                "redundant",
                f"Duplicate relationship: {rel.source} --{rel.type}--> {rel.target}",
                file=path,
            )
        seen.add(key)

    return relationships


def _check_project(slug: str, environment: EnvironmentPaths, report: DoctorReport) -> None:
    """Check all files in a project."""
    paths = ProjectPaths.for_project(slug, environment)
    report.projects_checked += 1

    # Check project.yaml if it exists
    project_yaml = paths.root / "project.yaml"
    if project_yaml.exists():
        report.files_checked += 1
        data = _check_yaml_syntax(project_yaml, report)
        if data is not None:
            _check_project_config(project_yaml, data, report)

    # Collect all node IDs for relationship validation
    all_node_ids: set[str] = set()

    # Check all node files
    if paths.nodes_dir.exists():
        for node_type_dir in paths.nodes_dir.iterdir():
            if not node_type_dir.is_dir():
                continue
            for node_file in node_type_dir.glob("*.yaml"):
                report.files_checked += 1
                data = _check_yaml_syntax(node_file, report)
                if data is not None:
                    node = _check_node(node_file, data, report)
                    if node:
                        all_node_ids.add(node.id)

    # Check relationships
    if paths.relationships_yaml.exists():
        report.files_checked += 1
        data = _check_yaml_syntax(paths.relationships_yaml, report)
        if data is not None:
            _check_relationships(paths.relationships_yaml, data, all_node_ids, report)

    # Check for empty project
    if not all_node_ids:
        report.add(
            Severity.INFO,
            "empty",
            f"Project '{slug}' has no nodes",
            file=paths.root,
            suggestion="Add nodes with 'ic describe node create'",
        )


def run_doctor(environment: EnvironmentPaths | None = None) -> DoctorReport:
    """Run all health checks and return report."""
    if environment is None:
        environment = EnvironmentPaths.current()

    report = DoctorReport()

    # Check config.yaml
    if environment.config_yaml.exists():
        report.files_checked += 1
        _check_yaml_syntax(environment.config_yaml, report)

    # Check all projects
    projects = list_projects(environment)
    if not projects:
        report.add(
            Severity.INFO,
            "empty",
            "No projects found",
            file=environment.projects_dir,
            suggestion="Create a project with 'ic describe project create <name>'",
        )

    for slug in projects:
        _check_project(slug, environment, report)

    return report


def _display_report(report: DoctorReport) -> None:
    """Display the doctor report to the console."""
    # Summary
    console.print()
    console.print(
        f"[bold]Checked:[/bold] {report.files_checked} files, {report.nodes_checked} nodes, {report.relationships_checked} relationships, {report.projects_checked} projects"
    )
    console.print()

    if not report.issues:
        console.print("[green]✓ No issues found[/green]")
        return

    # Group issues by severity
    errors = [i for i in report.issues if i.severity == Severity.ERROR]
    warnings = [i for i in report.issues if i.severity == Severity.WARNING]
    infos = [i for i in report.issues if i.severity == Severity.INFO]

    def _print_issues(issues: list[Issue], label: str, style: str) -> None:
        if not issues:
            return
        console.print(f"[{style}]{label} ({len(issues)}):[/{style}]")
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Category", style="dim")
        table.add_column("Message")
        table.add_column("File", style="dim")

        for issue in issues:
            file_str = str(issue.file.name) if issue.file else ""
            table.add_row(issue.category, issue.message, file_str)

        console.print(table)
        console.print()

    _print_issues(errors, "Errors", "red bold")
    _print_issues(warnings, "Warnings", "yellow bold")
    _print_issues(infos, "Info", "blue bold")

    # Summary line
    parts = []
    if report.error_count:
        parts.append(f"[red]{report.error_count} error(s)[/red]")
    if report.warning_count:
        parts.append(f"[yellow]{report.warning_count} warning(s)[/yellow]")
    if report.info_count:
        parts.append(f"[blue]{report.info_count} suggestion(s)[/blue]")

    console.print("Summary: " + ", ".join(parts))


@app.callback(invoke_without_command=True)
def doctor(
    fix: bool = typer.Option(False, "--fix", "-f", help="Attempt to auto-fix issues (not yet implemented)"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show all checks, including passed (not yet implemented)"
    ),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON"),
) -> None:
    """Check infrastructure data for validity and completeness.

    Validates:
    - YAML syntax
    - Schema compliance (Pydantic models)
    - Missing recommended info (ssh_alias, descriptions)
    - Orphaned relationships (references to non-existent nodes)
    - Duplicate/redundant entries
    """
    try:
        report = run_doctor()
    except EnvironmentNotFoundError:
        console.print("[red]No infracontext environment found. Run 'ic init' first.[/red]")
        raise typer.Exit(1) from None

    if json_output:
        import json

        output = {
            "summary": {
                "files_checked": report.files_checked,
                "nodes_checked": report.nodes_checked,
                "relationships_checked": report.relationships_checked,
                "projects_checked": report.projects_checked,
                "errors": report.error_count,
                "warnings": report.warning_count,
                "info": report.info_count,
            },
            "issues": [
                {
                    "severity": i.severity,
                    "category": i.category,
                    "file": str(i.file) if i.file else None,
                    "message": i.message,
                    "suggestion": i.suggestion,
                }
                for i in report.issues
            ],
        }
        console.print(json.dumps(output, indent=2))
    else:
        _display_report(report)

    if report.has_errors:
        raise typer.Exit(1)
