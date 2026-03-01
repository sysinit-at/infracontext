# Infracontext User Guide

Infracontext is a CLI tool for documenting infrastructure and troubleshooting issues. It combines two workflows:

1. **System Description** - Document your infrastructure during "peace times" so you're prepared for incidents
2. **Troubleshooting** - Claude performs diagnostics using the USE method with context from your documentation

## Key Concept: LLM-Driven Triage

Unlike traditional monitoring tools, infracontext provides **structured context** to Claude Code, which then performs the actual troubleshooting. Your node documentation is a "living document" that:

- Guides Claude during triage
- Accumulates learnings over time (from both humans and Claude)
- Evolves as you discover new things about your infrastructure

You don't need to document every detail - Claude fills gaps with its knowledge and assumptions. Focus on what's unique or non-obvious about your setup.

## Getting Started

### Installation

Requires Python 3.14+.

```bash
git clone <repo>
cd infracontext
uv sync

# Create shell alias (add to .zshrc/.bashrc)
alias ic='uv run --directory /path/to/infracontext ic'

# Verify installation
ic --help
```

### Initialize and Create a Project

Infracontext supports multiple projects with optional hierarchy (customer/project).

```bash
# Initialize
ic init

# Create a simple project
ic describe project create homelab

# Or use hierarchical organization (customer/project)
ic describe project create acme/production
ic describe project create acme/staging
ic describe project create bigcorp/prod

# List projects (shows hierarchy when present)
ic describe project list
#              Projects
# ┏━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━┓
# ┃ Customer ┃ Environment  ┃ Active ┃
# ┡━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━┩
# │ acme     │ production   │ *      │
# │ acme     │ staging      │        │
# │ bigcorp  │ prod         │        │
# └──────────┴──────────────┴────────┘

# Switch with full path
ic describe project switch acme/staging
```

## Documenting Infrastructure

### Adding Nodes

Nodes represent infrastructure components: VMs, containers, services, etc.

```bash
# Create nodes
ic describe node create --type vm --name "Web Server"
ic describe node create --type vm --name "Database"
ic describe node create --type physical_host --name "Hypervisor 01"

# List nodes
ic describe node list
ic describe node list --type vm  # Filter by type

# Find nodes by domain, IP, name, or ID
ic describe node find example.com
ic describe node find 192.168.1.100

# View node details
ic describe node show vm:web-server

# Edit in your editor
ic describe node edit vm:web-server

# Delete a node
ic describe node delete vm:web-server
```

### Essential Node Fields

The minimum viable node for triage:

```yaml
version: "2.0"
id: "vm:web-server"
slug: web-server
type: vm
name: "Web Server"

# SSH connection - CRITICAL for triage
ssh_alias: "web"  # Must match an alias in ~/.ssh/config
```

**Important**: `ssh_alias` is at the top level, not nested. SSH aliases are defined in your `~/.ssh/config` and handle hostnames, ports, jump hosts, keys, and usernames. Claude uses the alias directly: `ssh web`.

### Recommended Node Fields

```yaml
version: "2.0"
id: "vm:web-server"
slug: web-server
type: vm
name: "Production Web Server"

# SSH connection
ssh_alias: "web-prod"

# Network identity
ip_addresses: ["192.168.1.10"]
domains: ["web.example.com"]

# What you'd tell an on-call engineer
description: "Main web application server running nginx + PHP-FPM"
notes: |
  Peak traffic: 5-7pm weekdays
  Known issue: Memory climbs until weekly restart (Sunday 3am)
```

### Triage Configuration

Minimal hints for Claude - the agent discovers logs and commands itself:

```yaml
triage:
  # Services that matter on this node
  services:
    - nginx
    - php-fpm

  # Free-form context for Claude
  context: |
    This server handles 10k req/s at peak.
    If CPU high, check PHP-FPM pool status first.
    Redis cache runs on the same host.

  # Optional: override project default tier (0-4)
  tier: 3  # privileged

  # Optional: override collector script path
  collector_script: /opt/custom/collect.sh
```

## Access Tiers

Access tiers control what diagnostic methods Claude can use during triage. Tiers prevent over-privileged access to sensitive production systems.

### Tier Levels

| Tier | Value | Capabilities |
|------|-------|--------------|
| `local_only` | 0 | Local data + observability APIs only (Prometheus, Loki, CheckMK) |
| `collector` | 1 | + Execute pre-deployed `/usr/local/bin/ic-collect.sh` |
| `unprivileged` | 2 | + Arbitrary read-only SSH commands (no sudo) |
| `privileged` | 3 | + SSH with sudo/root access |
| `remediate` | 4 | + Autonomous fixes after diagnosis |

### Configuration Hierarchy

```
Project default → Node override → CLI restriction
     ↓                ↓               ↓
  baseline      can raise/lower    can only lower
```

The project's `max_tier` acts as a hard ceiling that cannot be exceeded.

### Project Configuration

Configure default tier in `project.yaml` at the project root:

```yaml
version: "2.0"
name: "Production"
slug: production

access:
  default_tier: 2       # unprivileged (default for all nodes)
  max_tier: 3           # privileged (hard ceiling)
  collector_script: /usr/local/bin/ic-collect.sh
```

### CLI Override

Restrict tier via CLI flag (can only lower, not elevate):

```bash
# Restrict to observability-only diagnostics
ic --tier local_only describe node context vm:web-server

# Restrict to collector script only
ic --tier collector describe node context vm:web-server

# Or via environment variable
IC_TIER=unprivileged ic describe node context vm:web-server
```

### Node Context Output

The `access` section appears in node context:

```yaml
access:
  tier: unprivileged
  tier_level: 2
  capabilities:
    - local_data
    - observability_api
    - collector_script
    - ssh_readonly
```

Claude checks this section before running diagnostics and adjusts its approach accordingly

### Learnings

Learnings are discovered knowledge that accumulates over time. Claude records them during triage, and you can add them manually:

```bash
# Add a learning manually
ic describe node learning vm:web-server \
  "PHP slow log is at /var/log/php8.1-fpm/slow.log (non-standard)" \
  --context "slow request investigation" \
  --source human
```

Learnings appear in node context and help future investigations:

```yaml
learnings:
  - date: "2024-01-15"
    context: "high CPU investigation"
    finding: "PHP-FPM pool was set to static with too many workers"
    source: agent
  - date: "2024-01-20"
    context: "disk space issue"
    finding: "Laravel log rotation not working, check /etc/logrotate.d/laravel"
    source: human
```

### Defining Relationships

Relationships document dependencies:

```bash
# Create a relationship
ic describe relationship create \
  --source vm:web-server \
  --target vm:db-server \
  --type depends_on \
  --description "PostgreSQL connection"

# Interactive wizard
ic describe relationship wizard

# List relationships
ic describe relationship list
```

## Troubleshooting with Claude

### The /ic-triage Skill

Use the `/ic-triage` skill in Claude Code:

```
/ic-triage vm:web-server "high CPU usage"
/ic-triage db-master "slow queries"
/ic-triage myhost  # General health check
```

Claude will:
1. Get full context from `ic describe node context <node-id>`
2. Extract the SSH alias and test connectivity
3. Run USE method diagnostics
4. Check configured services, logs, and custom commands
5. Consider previous learnings
6. Synthesize findings and recommendations
7. Record new learnings if discoveries are valuable

### Getting Node Context

Claude uses this command to understand the node:

```bash
ic describe node context vm:web-server
ic describe node context vm:web-server --format json
ic describe node context vm:web-server --format toon  # Token-efficient for LLMs
```

Output includes:
- SSH connection info (alias or fallback to IP/domain)
- Access tier and capabilities
- Triage hints (services, context)
- Previous learnings
- Dependencies (upstream and downstream)

### Graph Analysis

Analyze infrastructure dependencies:

```bash
# What does this node depend on?
ic triage analyze vm:web-server --upstream

# What depends on this node?
ic triage analyze vm:db-master --downstream

# Find all paths between two nodes
ic triage analyze vm:web-server --paths-to vm:db-master

# Impact analysis: what breaks if this node fails?
ic triage impact vm:db-master

# Find single points of failure
ic triage spof

# Detect circular dependencies
ic triage cycles

# Find orphaned nodes
ic triage orphans
```

## SSH Configuration

### Setting Up SSH Aliases

Configure SSH aliases in `~/.ssh/config`:

```
Host web-prod
    HostName 192.168.1.10
    User admin
    IdentityFile ~/.ssh/infra_key

Host db-prod
    HostName 192.168.1.20
    User admin
    IdentityFile ~/.ssh/infra_key
    # Through a bastion
    ProxyJump bastion

Host bastion
    HostName bastion.example.com
    User admin
    IdentityFile ~/.ssh/bastion_key
```

Then in your node YAML:

```yaml
ssh_alias: "web-prod"
```

Claude will simply run `ssh web-prod` - all connection details are handled by SSH config.

### Why SSH Aliases?

- **Simplicity**: One name instead of user@host:port + options
- **Jump hosts**: ProxyJump handled automatically
- **Key management**: IdentityFile per-host
- **Consistency**: Same alias works everywhere

## Infrastructure Sources

### SSH Config Import

Import nodes from an SSH config file:

```bash
# Auto-discovers path from project hierarchy
# Project acme/production → ~/.ssh/conf.d/acme/production.conf
ic import ssh

# Or specify an explicit path
ic import ssh --path ~/.ssh/config
```

### Proxmox Integration

Sync nodes from a Proxmox VE cluster:

```bash
# Store API credentials in system keychain
ic config credential set proxmox:prod

# Add the source
ic describe source add prod --type proxmox

# Configure (opens editor)
ic describe source configure prod

# Sync nodes
ic describe source sync prod
```

Synced nodes get `managed_by: proxmox-prod`. You can still add `ssh_alias`, `triage` config, and other fields - they won't be overwritten.

### Managing Sources

```bash
# List configured sources
ic describe source list

# Remove a source (does not delete synced nodes)
ic describe source remove prod
```

### Manual Nodes

For infrastructure not covered by plugins:

```bash
ic describe node create --type external_service --name "AWS S3"
ic describe node edit external_service:aws-s3
```

## Best Practices

### 1. Start Simple

Begin with:
- Node name, type, slug
- SSH alias
- One-line description

Add more detail when you need it.

### 2. Document the Non-Obvious

Claude knows standard things. Document what's unique:
- Non-standard log locations
- Custom health endpoints
- Expected high resource usage
- Known issues and workarounds

### 3. Use SSH Aliases

Don't put connection details in YAML. Use SSH config - it's more flexible and works with other tools too.

### 4. Let Learnings Accumulate

After each triage, Claude records what it discovered. Over time, your documentation becomes richer without manual effort.

## Querying Monitoring Sources

Query your monitoring systems directly from the CLI. Useful for quick status checks before SSH diagnostics.

### Supported Sources

| Source | Backend | Query Method |
|--------|---------|--------------|
| Prometheus | HTTP API | HTTP requests |
| Loki | HTTP API | HTTP requests |
| CheckMK | REST API | HTTP requests |
| Monit | HTTP | SSH tunnel or direct HTTP |

### Setup

**1. Configure source endpoints** (per project):

```bash
# Add sources
ic describe source add prometheus --type prometheus
ic describe source add loki --type loki
ic describe source add checkmk --type checkmk

# Configure each source
ic describe source configure prometheus
```

Source config examples:

```yaml
# sources/prometheus.yaml
type: prometheus
addr: http://prometheus.example.com:9090

# sources/loki.yaml
type: loki
addr: http://loki.example.com:3100

# sources/checkmk.yaml
type: checkmk
api_url: https://monitoring.example.com/mysite/check_mk/api/1.0
credential: checkmk:mysite  # keychain account (user:secret format)
```

**2. Configure node observability** (how to find each node in monitoring):

```yaml
# In node YAML
observability:
  - type: prometheus
    instance: web-server:9100           # Prometheus instance label
  - type: loki
    selector: '{service_name="web"}'    # LogQL selector
  - type: checkmk
    host_name: web-server.example.com   # CheckMK host name
  - type: monit
    monit_url: http://web-server:2812   # Direct HTTP (optional)
    # OR use SSH mode (default) - queries via node's ssh_alias
```

### Multiple Sources of Same Type

For multiple Prometheus/Loki instances (e.g., prod vs staging):

```yaml
# sources/prometheus-prod.yaml
type: prometheus
addr: http://prometheus-prod:9090

# sources/prometheus-staging.yaml
type: prometheus
addr: http://prometheus-staging:9090
```

Reference specific source in node config:

```yaml
observability:
  - type: prometheus
    source: prometheus-prod    # Uses prometheus-prod.yaml
    instance: web-server:9100
```

### Query Commands

```bash
# Quick status from all configured sources
ic query status vm:web-server

# Individual sources
ic query prometheus vm:web-server              # Key metrics (CPU, memory, disk)
ic query prometheus vm:web-server -t cpu       # Specific metric
ic query prometheus vm:web-server --promql 'up{instance="web:9100"}'

ic query loki vm:web-server                    # Recent logs
ic query loki vm:web-server --grep error       # Filter for errors
ic query loki vm:web-server --since 2h         # Time range
ic query loki vm:web-server --labels           # List available labels

ic query checkmk vm:web-server                 # Host status
ic query checkmk vm:web-server -t services     # All services
ic query checkmk vm:web-server -t alerts       # Active problems

ic query monit vm:web-server                   # Monit service summary
ic query monit vm:web-server -s nginx          # Specific service
ic query monit vm:web-server --url http://monit.example.com:2812  # Direct HTTP
```

### Monit Modes

Monit runs locally on each server. Two access modes:

**SSH mode** (default): Connects via SSH and queries localhost:2812
```bash
ic query monit vm:web-server  # Uses node's ssh_alias
```

**Direct HTTP mode**: Queries exposed Monit interface
```bash
ic query monit vm:web-server --url http://monit.example.com:2812
```

Or configure in node YAML:
```yaml
observability:
  - type: monit
    monit_url: http://monit.example.com:2812
    credential_hint: monit:web-server  # Optional basic auth (user:pass in keychain)
```

### Storing Credentials

Credentials are stored in the system keychain (macOS Keychain, Linux secret service).

```bash
# Store a credential (prompted for password if -p not provided)
ic config credential set checkmk:mysite -p "automation:mysecret"
ic config credential set monit:web-server

# List stored credentials
ic config credential list

# Check if a credential exists (use --show to reveal)
ic config credential get checkmk:mysite
ic config credential get checkmk:mysite --show

# Delete a credential
ic config credential delete checkmk:mysite
```

## Configuration

View current configuration:

```bash
ic config show
```

## Local Overrides

Team members may have different SSH configs or local paths. Use `.infracontext.local.yaml` for machine-specific settings that shouldn't be committed:

```yaml
# .infracontext.local.yaml (add to .gitignore)
nodes:
  "vm:web-server":
    ssh_alias: my-web-alias        # My SSH config uses different alias
    source_paths:
      - /Users/me/projects/webapp  # My local checkout path
```

Overrides are applied automatically when reading nodes. Only these fields can be overridden:
- `ssh_alias` - SSH connection alias
- `source_paths` - Local source code paths (must be absolute)

## Data Storage

All data is stored in `.infracontext/` within your project repo (git-tracked):

```
.infracontext/
├── config.yaml                  # Environment config (active project)
└── projects/
    ├── homelab/                 # Simple project
    │   ├── nodes/
    │   ├── relationships.yaml
    │   └── sources/
    └── acme/                    # Hierarchical: customer
        └── production/          # Hierarchical: project
            ├── nodes/
            │   ├── vm/
            │   │   ├── web-server.yaml
            │   │   └── db-master.yaml
            │   └── physical_host/
            │       └── pve01.yaml
            ├── relationships.yaml
            └── sources/
                ├── prometheus.yaml
                ├── loki.yaml
                └── checkmk.yaml

.infracontext.local.yaml         # Local overrides (gitignored)
```

### Migration from Legacy Location

If you have data in the old `~/.local/share/` location:

```bash
# Check migration status
ic migrate status

# Preview migration
ic migrate legacy --dry-run

# Migrate all projects
ic migrate legacy

# Migrate specific project
ic migrate legacy -p acme/production
```

YAML files are human-editable and preserve comments when modified by the CLI.

## Data Validation

Validate your infrastructure data for syntax errors, schema compliance, and completeness:

```bash
# Full validation
ic doctor

# JSON output (for CI/CD)
ic doctor --json
```

Doctor checks for:
- **Syntax errors**: Invalid YAML
- **Schema violations**: Fields that don't match Pydantic models
- **Missing info**: Compute nodes without `ssh_alias`, nodes without descriptions
- **Orphaned relationships**: References to non-existent nodes
- **Duplicates**: Redundant relationships

Exit code is 1 if errors are found (warnings/info are non-blocking).

## Claude Code Integration

### Installing Skills and Agents

Symlink the skills and agents to your Claude Code configuration:

```bash
# Triage skill - USE method diagnostics
ln -s /path/to/infracontext/commands/ic-triage.md ~/.claude/commands/ic-triage.md

# Node collector skill - auto-discover and create node YAML
ln -s /path/to/infracontext/commands/ic-collect.md ~/.claude/commands/ic-collect.md

# Diagnostic agents (used by triage skill)
ln -s /path/to/infracontext/agents ~/.claude/agents/infracontext
```

### Using /ic-triage

```
/ic-triage vm:web-server "high CPU"
```

Claude reads the skill instructions, gets node context from `ic`, and performs the investigation.

### Using /ic-collect

```
/ic-collect web-prod
/ic-collect s.myserver --project prod
```

Claude SSHes to the server, auto-discovers system info (OS, services, ports, monitoring agents), then walks you through an interactive conversation to fill in project, triage config, and context. Outputs a complete node YAML.
