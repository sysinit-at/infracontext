"""Shell-completion callbacks for node IDs and project names.

These run inside the user's shell on every ``<TAB>``, so they must be fast and
never raise: a broken environment (no ``.infracontext/``, unreadable config,
half-written project) must degrade to "no completions", not a stack trace in
the middle of the prompt.

Node-ID completion deliberately reads only the ``nodes/<type>/<slug>.yaml``
*directory structure* (type = directory name, slug = file stem) -- no YAML is
parsed -- so it stays well under the ~10 ms budget a shell allows.
"""

from __future__ import annotations


def complete_node_id(incomplete: str) -> list[str]:
    """Complete ``type:slug`` node IDs from the active project on disk.

    Enumerates ``nodes/<type>/<slug>.yaml`` without parsing any file. Matches
    are substring (case-insensitive) so a partial slug like ``web`` surfaces
    ``vm:web-01``. Returns ``[]`` on any error.
    """
    try:
        from infracontext.config import get_active_project
        from infracontext.paths import EnvironmentPaths, ProjectPaths

        environment = EnvironmentPaths.current()
        project = get_active_project(environment)
        if not project:
            return []

        paths = ProjectPaths.for_project(project, environment)
        if not paths.nodes_dir.exists():
            return []

        needle = incomplete.lower()
        ids: list[str] = []
        for type_dir in paths.nodes_dir.iterdir():
            if not type_dir.is_dir():
                continue
            for node_file in type_dir.glob("*.yaml"):
                node_id = f"{type_dir.name}:{node_file.stem}"
                if not needle or needle in node_id.lower():
                    ids.append(node_id)
        return sorted(ids)
    except Exception:
        return []


def complete_project(incomplete: str) -> list[str]:
    """Complete project slugs from the environment. Returns ``[]`` on error."""
    try:
        from infracontext.paths import list_projects

        needle = incomplete.lower()
        return [p for p in list_projects() if not needle or needle in p.lower()]
    except Exception:
        return []
