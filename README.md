# Infracontext

Infrastructure context for humans and agents.

## Features

- **System Description**: Document infrastructure in "peace times" for faster incident response
- **Triage and Tracing**: USE method diagnostics and cross-stack request tracing, driven by Claude
- **Living Documentation**: Accumulates learnings over time from both humans and Claude
- **Multi-project**: Hierarchical organization (customer/project)
- **Federation**: Compose multiple repos (fleet + per-app) into one unified view
- **File-based**: Human-editable YAML files, no database required
- **Monitoring Queries**: Query Prometheus, Loki, CheckMK, Monit, and SOS from the CLI
- **Source Sync**: Import nodes from Proxmox VE clusters and SSH config files

## Installation

Requires Python 3.14+.

```bash
# From source
git clone https://github.com/sysinit-at/infracontext.git
cd infracontext
uv sync

# Create shell alias (add to .zshrc/.bashrc)
alias ic='uv run --directory /path/to/infracontext ic'
```

## Quick Start

```bash
# Initialize and create a project
ic init
ic describe project create acme/production

# Add nodes
ic describe node create --type vm --name "web-server"
ic describe node edit vm:web-server  # Add ssh_alias, triage config

# Import from SSH config
ic import ssh --path ~/.ssh/config

# Create relationships
ic describe relationship wizard

# Query monitoring
ic query status vm:web-server

# Analyze infrastructure
ic graph spof
ic graph analyze vm:web-server

# Validate data
ic doctor
```

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
