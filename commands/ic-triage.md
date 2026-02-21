---
description: Triage infracontext nodes using the USE method with infrastructure context.
allowed-tools: Bash, Task, Read, Write
---

# Infracontext Triage

You are performing infrastructure triage using the USE method (Utilization, Saturation, Errors) with context from infracontext.

## Agent Locations

Checker agents are in `~/.claude/agents/infracontext/`:

| Agent | Purpose |
|-------|---------|
| `ic-connectivity-checker.md` | Validate SSH, detect OS/privilege |
| `ic-collector-checker.md` | Execute pre-deployed collector script (tier: collector) |
| `ic-cpu-checker.md` | CPU utilization, saturation, errors |
| `ic-memory-checker.md` | Memory utilization, OOM, swap |
| `ic-storage-io-checker.md` | Disk I/O latency, throughput |
| `ic-storage-capacity-checker.md` | Filesystem usage, inodes |
| `ic-service-checker.md` | Check node's configured services |
| `ic-log-checker.md` | Check node's configured log files |

## Input Format

The user provides:
- **Node ID, name, domain, or IP**: Full ID (`vm:web-server`), partial name (`proxy-01`), domain (`timetrack.acme.com`), or IP address
- **Issue description**: What problem they're investigating (optional)

Examples:
- `/ic-triage vm:proxy-01 "502 errors"`
- `/ic-triage app-01 "high memory"`
- `/ic-triage timetrack.acme.com "slow responses"` (resolves domain to node)
- `/ic-triage 10.0.1.10 "network issues"` (resolves IP to node)
- `/ic-triage db-master` (general health check)

---

## Access Tier Enforcement

Check `access.tier` from node context before spawning agents:

| Tier | Allowed Agents |
|------|----------------|
| `local_only` | None (query observability APIs only) |
| `collector` | ic-collector-checker only |
| `unprivileged` | + connectivity, cpu, memory, storage-capacity |
| `privileged` | + storage-io (SMART), full service-checker |
| `remediate` | Same as privileged (fixes happen post-diagnosis) |

### Tier-Specific Behavior

**`local_only` (tier_level: 0)**:
- NO SSH commands allowed
- Run only: `ic query prometheus/loki/checkmk <node-id>`
- Report observability data only

**`collector` (tier_level: 1)**:
- Execute ONLY the collector script specified in `access.collector_script`
- Use `ic-collector-checker` agent
- No other SSH commands allowed

**`unprivileged` (tier_level: 2)**:
- Read-only SSH commands (no sudo)
- Core USE agents: connectivity, cpu, memory, storage-capacity
- No privileged commands (SMART, dmesg, etc.)

**`privileged` (tier_level: 3)**:
- Full SSH access including sudo
- All diagnostic agents available
- Still READ-ONLY - no modifications

**`remediate` (tier_level: 4)**:
- Full diagnostic access (same as privileged)
- After diagnosis complete, MAY suggest or execute fixes
- Requires explicit user confirmation for any changes

---

## Critical Constraints

**Tier-based constraints:**
- `local_only`: NO SSH commands allowed
- `collector`: ONLY execute the collector script, nothing else
- `unprivileged`/`privileged`: READ-ONLY SSH (no restarts, no writes)
- `remediate`: May execute fixes AFTER diagnosis complete, with confirmation

**You must NEVER:**
- Execute SSH commands when tier is `local_only`
- Execute arbitrary commands when tier is `collector`
- Modify system state without `remediate` tier
- Write to disk on remote systems (except with `remediate`)

**After triage, you SHOULD:**
- Record significant learnings: `ic describe node learning <node-id> "finding" --context "investigation"`

---

## Execution Flow

### Step 1: Resolve Node and Get Context

```bash
# Find node by name, domain, or IP
ic describe node find <query>

# Examples:
ic describe node find timetrack.acme.com  # Find by domain
ic describe node find 10.0.1.10           # Find by IP
ic describe node find proxy-01            # Find by partial name
ic describe node find app-01              # Find by slug

# Get full context once node ID is known
ic describe node context <node-id>
```

The context provides:
- **SSH alias** - CRITICAL, use for all SSH commands
- **Triage config** - services, logs, commands to check
- **Learnings** - past findings to guide investigation
- **Dependencies** - upstream/downstream relationships
- **Source paths** - local source code for the application
- **Access** - tier, tier_level, capabilities, collector_script

**Check access.tier IMMEDIATELY after getting context.**

**If no SSH alias**: Inform user to add one with `ic describe node edit <node-id>`

### Step 1b: Check Access Tier

Read the `access` section from node context:

```yaml
access:
  tier: unprivileged
  tier_level: 2
  capabilities:
    - local_data
    - observability_api
    - ssh_readonly
```

If `tier` is `local_only`:
1. Skip all SSH-based agents
2. Run only observability queries:
   ```bash
   ic query prometheus <node-id>
   ic query loki <node-id> --grep error
   ic query checkmk <node-id>
   ```
3. Synthesize results from observability data only

If `tier` is `collector`:
1. Only spawn `ic-collector-checker` agent
2. Use the `collector_script` path from context
3. No other SSH commands allowed

### Step 1c: Consult Source Code (if available)

If the node has `source_paths`:

1. **Local paths** (`source_paths`): Read relevant config files, docker-compose, systemd units, or application code to understand:
   - How the service is configured
   - Expected ports, paths, environment variables
   - Health check endpoints
   - Log file locations

This helps correlate infrastructure state with application expectations (e.g., "config says port 8080 but service listening on 3000").

### Step 2: Validate Connectivity

**Skip if tier is `local_only`.**

Spawn the `ic-connectivity-checker` agent with:
- `SSH_ALIAS`: From node context
- `NODE_ID`: The node being triaged

This returns:
- Connection status
- OS family (debian/rhel/centos)
- Privilege level (root/sudo/user)
- CPU count (NPROC)
- Detected subsystems (ZFS, containers, etc.)

### Step 3: Analyze Issue Keywords

Determine additional checks based on issue description:

| Keywords | Additional Checks |
|----------|-------------------|
| network, connection, timeout, DNS | Network checker |
| error, crash, log, exception | Log checker with node's configured logs |
| container, docker, podman | Container checker |
| disk, storage, full, space | Storage capacity (priority) |

Also check detected subsystems from Step 2:
- ZFS detected → ZFS checker
- Containers detected → Container checker

### Step 4: Run Core USE Diagnostics (Parallel)

**For `collector` tier**: Only spawn `ic-collector-checker`.

**For `unprivileged` tier**: Launch these 4 agents in parallel:

1. **ic-cpu-checker**: CPU USE metrics
2. **ic-memory-checker**: Memory USE metrics
3. **ic-storage-capacity-checker**: Filesystem usage
4. **ic-service-checker**: Check services from node's triage config

**For `privileged`/`remediate` tier**: Add:

5. **ic-storage-io-checker**: Disk I/O USE metrics (requires SMART access)

Each agent receives:
- `SSH_ALIAS`: From node context
- `NODE_ID`: Node being triaged
- `PRIVILEGE`: From connectivity check
- `NPROC`: From connectivity check
- `TRIAGE_CONFIG`: Services/logs from node context

**IMPORTANT**: Launch all agents in a single response with parallel tool calls.

### Step 5: Check Node-Specific Config

If the node has triage configuration, also run:

**Services** (from `triage.services`):
```bash
ssh <alias> 'systemctl status <service1> <service2> --no-pager'
```

### Step 6: Apply Learnings

Review node's learnings section. Past findings often repeat:
- If learning says "high memory is normal for this Redis server" → don't flag as warning
- If learning says "check PHP-FPM first for CPU issues" → prioritize that check
- If learning mentions non-standard paths → use those paths

### Step 7: Synthesize Results

```
═══════════════════════════════════════════════════════════════
TRIAGE SUMMARY: {node_name} ({node_id})
Access Tier: {tier} (level {tier_level})
═══════════════════════════════════════════════════════════════

CRITICAL ({count}):
  • {finding requiring immediate action}

WARNING ({count}):
  • {finding to investigate}

OK ({count}):
  • {category}: All checks passed

LIKELY ROOT CAUSE:
{Analysis of most probable cause based on findings}

RECOMMENDED NEXT STEPS:
1. {action}
2. {action}

DEPENDENCIES:
  Upstream: {what this node depends on}
  Downstream: {what depends on this node - impact if this fails}
```

### Step 8: Record Learnings

If you discovered something valuable for future investigations:

```bash
ic describe node learning <node-id> "Brief finding" --context "What was investigated"
```

**Good learnings:**
- Non-standard paths discovered
- Normal baselines ("80% memory is expected - Redis cache")
- Service-specific quirks
- Troubleshooting shortcuts that worked

**Don't record:**
- Transient issues
- Findings already in triage config
- Generic troubleshooting steps

---

## SSH Command Pattern

Always use the SSH alias from node context:

```bash
ssh -o ConnectTimeout=10 -o BatchMode=yes <SSH_ALIAS> '<commands>'
```

For privileged commands (if not root, and tier is `privileged` or `remediate`):
```bash
ssh <SSH_ALIAS> 'sudo <command>'
```

---

## Graph Analysis

For complex issues, analyze dependencies:

```bash
# What does this node depend on?
ic triage analyze <node-id> --upstream

# What depends on this node?
ic triage analyze <node-id> --downstream

# Full impact analysis
ic triage impact <node-id>
```

---

## Example Session

```
User: /ic-triage proxy-01 "502 errors"

Claude: Resolving node...
$ ic describe node find proxy-01
vm:proxy-01  (slug contains 'proxy-01')

$ ic describe node context vm:proxy-01

# Node: proxy-01
ID: vm:proxy-01
Type: vm

## SSH Connection
SSH Alias: proxy-prod

## Access
Tier: privileged (level 3)
Capabilities: local_data, observability_api, ssh_readonly, ssh_privileged

## Triage Configuration
Services: nginx, apache2
Context: Primary reverse proxy. nginx on 80/443, apache on 8081 for WebDAV.

## Learnings
[2026-01-28] (agent)
  Context: initial discovery
  Finding: GeoIP blocking enabled. Configs in /etc/nginx/conf.d/{customer}/*.conf

## Dependencies
Routes to: vm:app-01 (endpoints: timetrack, wiki, dashboard, ...)

---

Access tier is `privileged` - full diagnostics available.

Validating connectivity...
[ic-connectivity-checker] ✓ Connected
  OS: Debian 13, Privilege: root, Cores: 6

Running USE diagnostics in parallel...

[ic-cpu-checker] ✓ Complete
  OK: Load 0.5/6 cores, utilization 8%

[ic-memory-checker] ✓ Complete
  OK: Memory 45% used, no swap

[ic-storage-io-checker] ✓ Complete
  OK: I/O wait < 5%

[ic-storage-capacity-checker] ✓ Complete
  OK: / at 34%, inodes normal

[ic-service-checker] ✓ Complete
  OK: nginx active
  OK: apache2 active

Checking nginx error log (from triage config)...
$ ssh proxy-prod 'tail -100 /var/log/nginx/error.log | grep -iE "error|upstream"'
  WARNING: 23 "upstream timed out" errors (last hour)

Checking upstream dependency...
$ ic triage analyze vm:proxy-01 --downstream
  Routes to: vm:app-01

═══════════════════════════════════════════════════════════════
TRIAGE SUMMARY: proxy-01 (vm:proxy-01)
Access Tier: privileged (level 3)
═══════════════════════════════════════════════════════════════

CRITICAL (0):
  (none)

WARNING (1):
  • nginx: 23 upstream timeout errors in last hour

OK (6):
  • CPU: Load normal (0.5/6)
  • Memory: 45% used
  • Storage I/O: Normal
  • Storage Capacity: Normal
  • Services: nginx, apache2 running

LIKELY ROOT CAUSE:
Upstream server (vm:app-01) is slow or unresponsive,
causing nginx to timeout waiting for backend responses.

RECOMMENDED NEXT STEPS:
1. Triage upstream: /ic-triage vm:app-01 "slow responses"
2. Check which specific endpoints are timing out in nginx access log

DEPENDENCIES:
  Upstream: physical_host:pve01, block_storage:local-zfs
  Downstream: vm:app-01 (12 endpoints routed through this proxy)
```

Now execute the triage based on the user's input.
