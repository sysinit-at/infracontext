# Infracontext

Infrastructure context for humans and agents.

## Features

- **System Description**: Document infrastructure in "peace times" for faster incident response
- **LLM-Driven Triage**: Claude performs USE method diagnostics using your documentation as context
- **Living Documentation**: Accumulates learnings over time from both humans and Claude
- **Multi-project**: Hierarchical organization (customer/project)
- **File-based**: Human-editable YAML files, no database required
- **Monitoring Queries**: Query Prometheus, Loki, CheckMK, and Monit from the CLI
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
ic triage spof
ic triage analyze vm:web-server

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

## Documentation

- [docs/USAGE.md](docs/USAGE.md) - User guide
- [docs/SCHEMA.md](docs/SCHEMA.md) - Node YAML schema reference

## License

MIT
