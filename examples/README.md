# Example: ACME Corp Production

This example shows a typical web application stack with 6 nodes:

| Node | Type | Description |
|------|------|-------------|
| `physical_host:pve01` | Proxmox hypervisor | Single host running all VMs |
| `vm:web-01` | Web/App server | nginx + PHP-FPM + Laravel |
| `vm:db-01` | Database | PostgreSQL 15 |
| `vm:cache-01` | Cache/Queue | Redis 7 |
| `external_service:cloudflare-cdn` | CDN | Cloudflare for traffic |
| `external_service:aws-s3-backups` | Storage | S3 for backups |

## Architecture

```
                    ┌─────────────┐
                    │  Cloudflare │
                    │     CDN     │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │   web-01    │
                    │ nginx + PHP │
                    └──┬──────┬───┘
                       │      │
              ┌────────▼──┐ ┌─▼────────┐
              │   db-01   │ │ cache-01 │
              │ PostgreSQL│ │  Redis   │
              └─────┬─────┘ └──────────┘
                    │
              ┌─────▼─────┐
              │  AWS S3   │
              │  Backups  │
              └───────────┘

All VMs run on: pve01 (Proxmox)
```

## Using This Example

### Option 1: Copy to your data directory

```bash
# Create the project
ic describe project create acme-corp/production

# Copy the example files
cp -r examples/acme-corp/production/* \
  .infracontext/projects/acme-corp/production/

# Verify
ic describe node list
```

### Option 2: Import from SSH config

If you have an SSH config at `~/.ssh/conf.d/acme-corp/production.conf`:

```bash
# Create project (path matches SSH config location)
ic describe project create acme-corp/production
ic describe project switch acme-corp/production

# Import hosts from SSH config (auto-discovers path from project)
ic import ssh

# Or specify explicit path
ic import ssh --path ~/.ssh/config
```

### Option 3: Use as reference

Browse the YAML files to understand:
- How to structure nodes
- What fields to include for different node types
- How to write triage configurations
- How to define relationships

## Key Patterns Demonstrated

### 1. SSH Aliases

Every compute node has `ssh_alias` at the top level:

```yaml
ssh_alias: "acme-web01"  # Matches ~/.ssh/config entry
```

### 2. Triage Configuration

Minimal hints - the agent discovers logs and commands itself:

```yaml
triage:
  services: [nginx, php8.2-fpm]
  context: |
    If CPU high, check PHP-FPM slow log first.
```

### 3. Learnings

Past discoveries for future investigations:

```yaml
learnings:
  - date: "2024-02-05"
    context: "memory leak investigation"
    finding: "Laravel telescope was enabled in production"
    source: agent
```

### 4. External Services

Non-SSH services with context:

```yaml
type: external_service
triage:
  context: |
    External service - cannot SSH.
    Check via Cloudflare dashboard.
```

### 5. Relationships

Dependency graph:

```yaml
relationships:
  - source: "vm:web-01"
    target: "vm:db-01"
    type: depends_on
    description: "PostgreSQL database connection"
```

### 6. Source Configuration

Import nodes from infrastructure sources:

```yaml
# sources/ssh-config.yaml
version: "2.0"
name: ssh-config
type: ssh_config
status: configured
config_path: null  # Auto-derived from project hierarchy
default_node_type: vm
type_patterns:
  physical_host: ["^pve-"]
  lxc_container: ["^ct-"]
```

Sync with `ic describe source sync ssh-config` or use the one-liner `ic import ssh`.

## Customizing for Your Infrastructure

1. **SSH aliases**: Update to match your `~/.ssh/config`
2. **IP addresses**: Change to your actual IPs
3. **Paths**: Adjust log paths for your distro/setup
4. **Commands**: Modify health checks for your apps
5. **Services**: Update systemd service names

## Running Triage

After setup, use the Claude Code skill:

```
/ic-triage vm:web-01 "slow responses"
```

Or analyze dependencies:

```bash
ic triage analyze vm:web-01 --downstream
ic triage impact vm:db-01
ic triage spof
```
