---
description: Trace a request or flow across infrastructure nodes using relationships, source code, and observability.
allowed-tools: Bash, Task, Read, Write
---

# Infracontext Trace

You are tracing a request or data flow across infrastructure nodes. Unlike `/ic-triage` (reactive USE-method diagnostics), tracing follows a path through the stack to understand how components interact, where latency accumulates, or where a request breaks down.

## Input Format

The user provides:
- **Starting node or endpoint**: A node ID, domain, URL, or service name
- **What to trace**: A request path, error, or flow description

Examples:
- `/ic-trace timetrack.acme.com "POST /api/timesheet slow"`
- `/ic-trace vm:proxy-01 "requests to /dashboard returning 502"`
- `/ic-trace vm:app-01 "websocket connections dropping"`
- `/ic-trace app-01 "how does authentication flow work"`

---

## Execution Flow

### Step 1: Resolve Entry Point and Load Context

```bash
# Find the starting node. -A searches the local root + every external
# (federated) root, so a request that crosses repos can be traced.
ic describe node find <query> -A

# Get full context. Accepts plain 'type:slug' or qualified '@alias:type:slug'
# for nodes that live in an external root.
ic describe node context <node-id>
ic describe node context @fleet:physical_host:pve-01
```

From the context, extract:
- **Relationships** — downstream nodes this routes to, upstream dependencies
- **Services** — what's running on this node
- **Source paths** — local application code for the service
- **Observability** — Loki/Prometheus/CheckMK config for log and metric queries

**Federation note:** Traces can cross root boundaries. A relationship in
the local repo can target `@fleet:physical_host:pve-01`; graph traversals
(`ic graph analyze`) follow these edges automatically and emit qualified
IDs. Pass those IDs unchanged to `node context` and `node learning`.

### Step 2: Map the Request Path

Use the graph to trace the full path:

```bash
# What does this node route to?
ic graph analyze <node-id> --downstream

# What does the destination depend on?
ic graph analyze <downstream-id> --downstream

# Find all paths between two nodes
ic graph analyze <start> --paths-to <end>
```

Build a hop-by-hop map: `client → proxy → app → database`

### Step 3: Collect Evidence at Each Hop

For each node in the path, gather relevant data. Adapt to the tier (check `access.tier` from node context).

**Logs** (most valuable for tracing):
```bash
# Query Loki if configured
ic query loki <node-id> --grep "<request identifier>"

# Or SSH for direct log access (if tier allows)
ssh <alias> 'grep -i "<pattern>" /var/log/<service>/access.log | tail -50'
```

**Metrics** (for latency/throughput):
```bash
ic query prometheus <node-id>
```

**Service status**:
```bash
ic query monit <node-id>
# or
ssh <alias> 'systemctl status <service> --no-pager'
```

**Source code** (if source_paths available):
- Read config files to understand routing rules, middleware, timeouts
- Check application code for the specific endpoint/handler
- Look for error handling, retry logic, connection pool settings

### Step 4: Consult Source Code

If nodes have `source_paths`, read relevant application code to understand:
- **Routing**: How does the request get dispatched?
- **Middleware**: Authentication, rate limiting, logging
- **Connections**: How does this service talk to the next hop? (connection pools, timeouts, retries)
- **Error handling**: What happens when a downstream call fails?

This is the key differentiator from triage — you're reading application code, not just checking system metrics.

### Step 5: Correlate Across Hops

Look for:
- **Timing gaps**: Where does latency accumulate?
- **Error propagation**: Where does the error originate vs where it surfaces?
- **Config mismatches**: Timeouts, ports, hostnames that don't align between hops
- **Missing links**: Nodes in the path that aren't documented in infracontext

### Step 6: Synthesize

```
═══════════════════════════════════════════════════════════════
TRACE: <description>
Path: <node-1> → <node-2> → <node-3>
═══════════════════════════════════════════════════════════════

HOP 1: <node-1> (<role>)
  Observation: <what was found>
  Latency contribution: <if measurable>

HOP 2: <node-2> (<role>)
  Observation: <what was found>
  Latency contribution: <if measurable>

...

FINDING:
<Where the issue is, or how the flow works>

RECOMMENDED NEXT STEPS:
1. <action>
2. <action>
```

### Step 7: Record Learnings

If you discovered something valuable:

```bash
ic describe node learning <node-id> "Brief finding" --context "trace: <description>"
```

Good learnings for traces:
- Discovered request paths not documented in relationships
- Timeout settings that don't match between hops
- Non-obvious service dependencies (e.g., "app-01 calls auth service on app-02 port 9090")
- Config locations for routing rules

---

## Differences from /ic-triage

| Aspect | /ic-triage | /ic-trace |
|--------|-----------|-----------|
| Goal | Is this node healthy? | How does this request flow? |
| Method | USE method (CPU, mem, disk, I/O) | Follow request across nodes |
| Scope | Single node | Multiple nodes (the path) |
| Key data | System metrics, service status | Logs, source code, config |
| Audience | Ops/infra | Devs and ops |
| Agents | USE checker agents | None (direct investigation) |

---

## SSH Command Pattern

Always use the SSH alias from node context:

```bash
ssh -o ConnectTimeout=10 -o BatchMode=yes <SSH_ALIAS> '<commands>'
```

Respect access tiers — check `access.tier` before running SSH commands on each node in the path.

---

Now execute the trace based on the user's input.
