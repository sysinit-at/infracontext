# Infracontext

Infrastructure context for humans and agents.

## Features

- **Incident hot path**: one-word `ic ssh` / `ic ctx` / `ic status` / `ic learn`, each resolving a node fuzzily
- **System Description**: Document infrastructure in "peace times" for faster incident response
- **Triage and Tracing**: USE method diagnostics and cross-stack request tracing, driven by Claude
- **Living Documentation**: Accumulates learnings over time from both humans and Claude
- **Run from anywhere**: `IC_ROOT` or a global environment registry lets `ic` work from any directory
- **Multi-project**: Hierarchical organization (customer/project)
- **Federation**: Compose multiple repos (fleet + per-app) into one unified view
- **File-based**: Human-editable YAML files, no database required
- **Monitoring Queries**: Query Prometheus, Loki, CheckMK, Monit, and SOS from the CLI
- **Source Sync**: Import nodes from Proxmox VE clusters, SSH config files, and Kubernetes
- **MCP server**: Expose node context and monitoring queries to any MCP client (`ic mcp serve`)

## Installation

Requires Python 3.14+.

```bash
git clone https://github.com/sysinit-at/infracontext.git
cd infracontext

# Recommended: install the `ic` command onto your PATH
# ('[mcp]' bundles the MCP server; plain `uv tool install .` works without it)
uv tool install '.[mcp]'

# Alternative: run from the checkout without installing
uv sync
alias ic='uv run --directory /path/to/infracontext ic'
```

After installing, `ic` still needs to find your environment. It walks up from
the current directory looking for `.infracontext/`, so it works out of the box
inside your infra repo. To reach an environment from *anywhere*, register it
once (see [Run From Anywhere](#run-from-anywhere)).

Enable shell completion for node IDs and project names:

```bash
ic --install-completion   # then restart your shell
```

## Quick Start

```bash
# Initialize (also gitignores the local-overrides file for you)
ic init

# Register this environment so `ic` reaches it from any directory
ic config env add home . --default

# Create a project
ic describe project create acme/production

# Add a node from an SSH alias in one step (sets ssh_alias, derives the slug)
ic describe node add web-prod

# Import many nodes from an SSH config file
ic import ssh-config --path ~/.ssh/config

# Create relationships
ic describe relationship wizard

# Analyze and render infrastructure
ic graph spof
ic graph render --open                       # → infracontext-graph.html, opens in browser

# Validate data
ic doctor
```

### During an Incident

The short commands are the fast path. Each resolves a node fuzzily, so a bare
`web` works when there is only one match (use the exact `type:slug` when
ambiguous):

```bash
ic ssh web              # context banner to stderr, then ssh onto the node
ic ssh web uptime       # run a remote command instead of an interactive shell
ic ctx web              # full triage context (services, learnings, dependencies)
ic status web           # all configured monitoring sources at once
ic find example.com     # which node serves this domain / IP / alias?
ic learn web "PHP-FPM pool was misconfigured"   # capture a finding on the spot
```

The long forms (`ic describe node context`, `ic query status`, `ic describe
node learning`) remain the canonical, scriptable interface.

## Run From Anywhere

`ic` discovers its environment in this order:

1. `IC_ROOT` environment variable, if it points at a directory containing
   `.infracontext/`.
2. Walk up from the current working directory (works inside your infra repo).
3. The default environment registered with `ic config env`.

```bash
ic config env add home ~/infra --default   # register + make default
ic config env add work ~/work/infra        # register another
ic config env list                         # show all, mark default and validity
ic config env default work                 # switch the default
IC_ROOT=~/other/infra ic doctor            # one-off override
```

The registry lives at `$XDG_CONFIG_HOME/infracontext/environments.yaml`
(falling back to `~/.config`).

## Claude Code Integration

Install the skills and agents:

```bash
# Triage skill
ln -s /path/to/infracontext/commands/ic-triage.md ~/.claude/commands/ic-triage.md

# Node collector skill
ln -s /path/to/infracontext/commands/ic-collect.md ~/.claude/commands/ic-collect.md

# Diagnostic agents (used by triage)
ln -s /path/to/infracontext/agents ~/.claude/agents/infracontext
```

Then in Claude Code:

```
# Collect info from a server and create a node YAML
/ic-collect web-prod

# Triage with the USE method
/ic-triage vm:web-server "high CPU"
```

Claude gets context from `ic`, performs SSH-based diagnostics, and records learnings.

### MCP Server

`ic mcp serve` exposes infracontext to any MCP client (Claude Desktop, other
agents) over stdio. It surfaces the same read paths the CLI uses, plus learning
capture:

- `find_node` — fuzzy node lookup
- `get_context` — full triage context for a node
- `query_status` — aggregated monitoring status
- `add_learning` — record a finding on a node

```bash
# tool install: include the extra          # dev checkout: sync it
uv tool install '.[mcp]'                   # uv sync --extra mcp
ic mcp serve                               # serve over stdio
```

## Federating Multiple Repositories

An admin managing several repos (e.g., a shared fleet repo for hypervisors
plus per-app repos) can compose them via `external_roots` in
`.infracontext/config.yaml`:

```yaml
active_project: prod
external_roots:
  - alias: fleet
    path: ../infra-fleet
    mode: read-only
```

Cross-root references use `@alias:type:slug`:

```yaml
- source: vm:web-01
  target: "@fleet:physical_host:pve-01"
  type: runs_on
```

`ic describe node list -A` and `ic graph *` span all roots. `ic doctor`
validates external paths, alias/project collisions, and duplicate node IDs.
See [docs/USAGE.md](docs/USAGE.md#federating-multiple-repositories-external-roots).

## Documentation

- [docs/USAGE.md](docs/USAGE.md) - User guide
- [docs/SCHEMA.md](docs/SCHEMA.md) - Node YAML schema reference

## License

MIT
