---
description: Collect node information via SSH and create an infracontext node YAML file.
allowed-tools: Bash, Read, Write, AskUserQuestion
---

# Infracontext Node Collector

You are collecting information about a server to create a new node in infracontext. Your goal is to gather **hard facts** via SSH, then have a **conversation** with the user about context that can't be discovered automatically.

## Input

The user provides:
- **SSH alias**: An alias from `~/.ssh/config` (required)
- **Project**: Target project name (optional, will ask if not provided)

Examples:
- `/ic-collect web-prod`
- `/ic-collect s.myserver --project prod`
- `/ic-collect jump-host`

---

## Phase 1: SSH Discovery (Automatic)

Connect to the host and gather hard facts. Run these commands via SSH:

```bash
SSH_ALIAS="<user-provided-alias>"

# Test connectivity first
ssh -o ConnectTimeout=10 -o BatchMode=yes "$SSH_ALIAS" 'echo OK' 2>&1
```

If connection fails, stop and inform the user to check their SSH config.

### Gather System Info

Run a single SSH command to collect all info efficiently:

```bash
ssh -o ConnectTimeout=30 "$SSH_ALIAS" '
echo "=== HOSTNAME ==="
hostname -f 2>/dev/null || hostname

echo "=== OS ==="
cat /etc/os-release 2>/dev/null | grep -E "^(PRETTY_NAME|ID|VERSION_ID)=" || uname -a

echo "=== IPS ==="
ip -4 addr show scope global 2>/dev/null | grep inet | awk "{print \$2}" | cut -d/ -f1 || ifconfig 2>/dev/null | grep "inet " | awk "{print \$2}"

echo "=== CPU ==="
nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null
lscpu 2>/dev/null | grep -E "^(Model name|Architecture):" || true

echo "=== MEMORY ==="
free -h 2>/dev/null | head -2 || vm_stat 2>/dev/null

echo "=== DISK ==="
df -h / /home /var 2>/dev/null | tail -n +2

echo "=== SERVICES ==="
systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null | head -30 || service --status-all 2>/dev/null | grep "+" | head -30

echo "=== LISTENING_PORTS ==="
ss -tlnp 2>/dev/null | tail -n +2 | head -20 || netstat -tlnp 2>/dev/null | tail -n +3 | head -20

echo "=== DOCKER ==="
docker ps --format "{{.Names}}: {{.Image}}" 2>/dev/null || podman ps --format "{{.Names}}: {{.Image}}" 2>/dev/null || echo "none"

echo "=== MONITORING_AGENTS ==="
pgrep -la node_exporter 2>/dev/null || echo ""
pgrep -la promtail 2>/dev/null || echo ""
pgrep -la alloy 2>/dev/null || echo ""
systemctl is-active monit 2>/dev/null || echo ""

echo "=== VIRTUALIZATION ==="
systemd-detect-virt 2>/dev/null || echo "unknown"
'
```

### Parse the Output

From the SSH output, extract:

| Field | Source |
|-------|--------|
| `hostname` | HOSTNAME section |
| `os_name` | PRETTY_NAME from OS |
| `os_id` | ID from OS (debian, ubuntu, rhel, etc.) |
| `ip_addresses` | IPS section |
| `cpu_cores` | CPU section (nproc) |
| `memory_mb` | MEMORY section (convert to MB) |
| `services` | SERVICES section (service names only) |
| `listening_ports` | LISTENING_PORTS section |
| `containers` | DOCKER section |
| `monitoring_agents` | MONITORING_AGENTS section |
| `virtualization` | VIRTUALIZATION (vm, lxc, docker, none, etc.) |

### Infer Node Type

Based on virtualization detection:
- `none` or `bare-metal` → `physical_host`
- `kvm`, `qemu`, `vmware`, `microsoft`, `xen` → `vm`
- `lxc`, `lxc-libvirt` → `lxc_container`
- `docker`, `podman` → `oci_container`

---

## Phase 2: Present Discovered Facts

Show the user what was discovered:

```
Discovered from <SSH_ALIAS>:

System:
  Hostname: web-prod.example.com
  OS: Ubuntu 22.04 LTS
  Type: vm (detected: kvm)
  CPU: 4 cores
  Memory: 8192 MB

Network:
  IPs: 10.0.1.50, 192.168.1.50

Services (running):
  - nginx
  - php8.2-fpm
  - postgresql@15-main
  - ssh
  - node_exporter

Containers: none

Listening Ports:
  - 22 (ssh)
  - 80 (nginx)
  - 443 (nginx)
  - 5432 (postgres)
  - 9100 (node_exporter)

Monitoring:
  - node_exporter: running
```

---

## Phase 3: User Conversation

Now ask the user questions to fill in what can't be auto-discovered. Use `AskUserQuestion` for each topic.

### Question 1: Project

Check existing projects first:

```bash
ic describe project list
```

Then ask:

```
Which project should this node belong to?

Options:
1. <existing-project-1>
2. <existing-project-2>
3. Create new project
```

### Question 2: Confirm Node Type and Slug

Based on detected type and hostname, propose a slug:

```
Proposed node identity:

  Type: vm
  Slug: web-prod (from hostname)
  ID: vm:web-prod

Accept this, or provide alternatives?

Options:
1. Accept as proposed
2. Change type (if detection was wrong)
3. Change slug (provide custom)
```

### Question 3: Triage Services

From the running services, filter to the meaningful ones (exclude sshd, cron, dbus, etc.):

```
Which services should be monitored during triage?

Detected services that look important:
  [x] nginx
  [x] php8.2-fpm
  [x] postgresql@15-main
  [ ] node_exporter

Add/remove? (Enter to accept, or list services)
```

### Question 4: Triage Context

```
Any troubleshooting hints for this server?

This is free-form context that helps during triage. Examples:
- "High memory is normal for this Redis server (caching)"
- "Check logs at /opt/app/logs/ not /var/log"
- "PHP-FPM slow log useful for CPU issues"

(Enter to skip, or type hints)
```

### Question 5: Description

```
Brief description of this node's purpose?

Example: "Primary web server for customer portal"
```

### Question 6: Relationships (Optional)

```
Does this node have dependencies on other nodes?

Common patterns:
- Web server → Database
- App → Cache
- All VMs → Hypervisor

Enter node IDs this depends on (comma-separated), or skip.
Example: vm:db-master, vm:cache-01
```

---

## Phase 4: Generate Node YAML

Create the node file using `ic describe node create`:

```bash
# First, check if slug exists
ic describe node show <type>:<slug> 2>/dev/null

# If not, create it
ic describe node create --type <type> --name "<name>"
```

Then edit the created file to add all discovered and user-provided information.

### File Location

Nodes are stored at:
```
.infracontext/projects/<project>/nodes/<type>/<slug>.yaml
```

### Generated YAML Structure

```yaml
# <Name> - collected by ic-collect
version: "2.0"
id: "<type>:<slug>"
slug: <slug>
type: <type>
name: "<Name>"

# SSH connection
ssh_alias: "<user-provided-ssh-alias>"

# Network (discovered)
ip_addresses:
  - "<ip1>"
  - "<ip2>"

# Documentation
description: "<user-provided-description>"
notes: |
  ## System Info (collected <date>)
  - OS: <os_name>
  - CPU: <cpu_cores> cores
  - Memory: <memory_mb> MB

  ## Running Services
  <list of running services>

  ## Listening Ports
  <list of ports>

# Observability (discovered)
observability:
  - type: metrics
    name: "Node Exporter"
    url: "http://<ip>:9100/metrics"

# Discovered attributes
attributes:
  collected_at: "<iso-date>"
  os_id: "<os_id>"
  os_version: "<os_version>"
  cpu_cores: <cpu_cores>
  memory_mb: <memory_mb>
  virtualization: "<virt_type>"

# Triage hints
triage:
  services:
    - <service1>
    - <service2>
  context: |
    <user-provided-context>
```

---

## Phase 5: Confirm and Offer Next Steps

After writing the file:

```
Created node: <type>:<slug>
File: .infracontext/projects/<project>/nodes/<type>/<slug>.yaml

Next steps:
1. Review and edit: ic describe node edit <type>:<slug>
2. Add relationships: ic describe relationship wizard
3. Test triage: /ic-triage <type>:<slug>
```

---

## Error Handling

### SSH Connection Failed

```
Could not connect to '<ssh_alias>'.

Check:
1. SSH alias exists in ~/.ssh/config
2. Host is reachable: ssh <alias> 'echo test'
3. Key authentication is configured

Once fixed, try again: /ic-collect <alias>
```

### Node Already Exists

```
Node <type>:<slug> already exists.

Options:
1. View existing: ic describe node show <id>
2. Use different slug
3. Delete and recreate (if you're sure)
```

---

## Example Session

```
User: /ic-collect s.webapp

Claude: Connecting to s.webapp...
[SSH discovery happens]

Discovered from s.webapp:

System:
  Hostname: webapp.prod.example.com
  OS: Debian 12 (Bookworm)
  Type: vm (detected: kvm)
  CPU: 2 cores
  Memory: 4096 MB

Network:
  IPs: 10.8.0.25

Services:
  - nginx
  - gunicorn
  - postgresql@15-main
  - redis-server
  - ssh

Listening Ports:
  - 22, 80, 443, 5432, 6379, 8000

Monitoring:
  - node_exporter: running

---

Which project? [dev / staging / prod / new]
User: prod

Proposed: vm:webapp - Accept? [Y/n/change]
User: Y

Services to monitor: nginx, gunicorn, postgresql@15-main, redis-server
[Accept/modify]
User: accept

Troubleshooting hints?
User: Python app - check gunicorn logs at /var/log/gunicorn/

Description?
User: Main API backend for mobile apps

Dependencies?
User: skip

---

Created: vm:webapp
File: .infracontext/projects/prod/nodes/vm/webapp.yaml

Node is ready for triage: /ic-triage vm:webapp
```

Now execute the collection based on the user's input.
