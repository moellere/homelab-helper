# homelab-helper — Architecture

## Premise

Reminder of locked decisions, because the architecture is downstream of all of them:

- **OSS core, hobbyist-first**, named **homelab-helper**, with a future HA-Nabu-Casa-style optional subscription tier.
- **NetBox is in-the-loop** as canonical store for physical/L2/L3 inventory. Harness reads and writes via NetBox API.
- **Multi-source coordination, not single SoT**. NetBox owns inventory; K8s/Proxmox/UniFi/ArgoCD/Cloudflare own their slices; kernel probes are ground truth on anything storage- or hardware-related.
- **Plugin-from-day-one probes**.
- **Plugin objects** (`PhysicalPart`, `Placement`, `ConfigurationAssertion`, etc.) live in **harness DB**, not as NetBox plugins.
- **Continuous mode uses an agent** running on managed hosts (helper-agent).
- **L1 is the default and the floor; L2 is the planned final phase** — propose-never-apply ships first (Phases 1–5) and remains the product for anyone who never opts in. Execution (Phase 6) is gated by the **trust gradient**, a deterministic authorization model the operator controls (see "Security & trust"). Manifest schema is L2-ready from Slice 1.
- **Ollama as default local LLM backend**; cloud BYOK as opt-in.
- **No K8s pod-level modeling**; logical service surface kept by K8s adapter.
- **Cables in NetBox**: chokepoints only, not every patch.
- **MVP wedge: inventory & discovery (Phase 1).**

The architecture below is what falls out of those decisions taken together.

## System overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              Interfaces                                 │
│   CLI    │   HTTP API   │   Web UI (P4+)   │   MCP Server (P4+)        │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
┌────────────────────────────────┴────────────────────────────────────────┐
│                          Agent Layer (P4+)                              │
│   LLM Router │ Narrator │ Conv. Discovery │ Planner(P5) │ Triage(P7+)   │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
┌────────────────────────────────┴────────────────────────────────────────┐
│                            Engine Layer                                 │
│   Probe Runner   │   Reconciler   │   Assertion Engine                  │
│   Scheduler      │   Proposal Manager   │   Finding Generator           │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
┌────────────────────────────────┴────────────────────────────────────────┐
│                           Adapter Layer                                 │
│   NetBox  │  Kernel-SSH  │  Proxmox(P3)  │  K8s(P3)                    │
│   UniFi(P3)  │  Cloudflare(P3)  │  Git/ArgoCD(P3)                       │
└────────────────────────────────┬────────────────────────────────────────┘
                                 │
┌────────────────────────────────┴────────────────────────────────────────┐
│                             Data Layer                                  │
│   Harness DB (SQLite default, Postgres optional)                        │
│   NetBox (separate stack, in-the-loop)                                  │
│   Secrets Store (external — file/SOPS/Vault)                            │
│   Observation Store (logical; lives in harness DB Observation table)    │
└─────────────────────────────────────────────────────────────────────────┘

           ↑ helper-agent (per managed host) posts here ↑
```

The flow is top-down for user-driven interactions, bottom-up for observations. Adapters never call up into engine; engine never reaches around adapters to talk directly to NetBox or hosts. The layering is enforced (not just suggested) because the agent boundary is also the trust boundary — see "Security & trust" below.

## Components

### Data layer

- **Harness DB** — SQLAlchemy 2.0 models per `harness-schema-slice1.md`. SQLite by default for low-friction install; Postgres optional for users who want it or who run multi-process workers. Alembic migrations.
- **NetBox** — separate stack the user runs (via netbox-docker, or NetBox Cloud, or any deployment). The harness depends on it being reachable; if it's not, write operations queue and reads fall back to cached state.
- **Secrets store** — never in harness DB. Initial implementation: encrypted file with a key from OS keyring. Later: SOPS, Vault, 1Password CLI, any external secret backend. Harness DB only holds *references* (`credentials_ref` columns).
- **Observation store** — conceptually distinct because it's append-only time-series; physically lives in the harness DB Observation table. May graduate to a dedicated TSDB if scale demands.

### Adapter layer

Every external source the harness talks to gets an adapter implementing a uniform interface:

```python
class Adapter(Protocol):
    name: str
    capabilities: AdapterCapabilities

    def health_check(self) -> HealthStatus: ...
    def query(self, spec: QuerySpec) -> AsyncIterator[Observation]: ...
    def write(self, change: ChangeSpec) -> WriteResult: ...
    def supports(self, capability: str) -> bool: ...
```

Concrete adapters:

| Adapter | Phase | Reads | Writes |
|---|---|---|---|
| **NetBoxAdapter** | P1 | Devices, IPs, VLANs, Cables, Custom Fields | Devices, Custom Fields, InventoryItems, Services |
| **KernelSSHAdapter** | P1 | Anything `lshw`/`dmidecode`/`smartctl`/etc. produces over SSH | (read-only; never writes) |
| **ProxmoxAdapter** | P3 | Cluster state, VMs, Ceph health, replication | (read-only at L1) |
| **K8sAdapter** | P3 | Nodes, pods, services, labels, events | (read-only at L1) |
| **UniFiAdapter** | P3 | DNS records, DHCP leases, switch port config, network definitions | (read-only at L1) |
| **CloudflareAdapter** | P3 | DNS records, ACME certs, zone state | (read-only at L1) |
| **GitArgoCDAdapter** | P3 | Declared app state from Git, ArgoCD sync status | (read-only) |

L1 means write capability is wired only for harness-owned tables and NetBox. The other adapters' write paths exist as interfaces (so L2 doesn't repaint) but are gated off.

Adapter discovery is dynamic: the framework scans configured adapters on startup, runs each one's `health_check`, and produces a finding if any required adapter is unreachable.

### Engine layer

- **ProbeRunner** — dispatches probe plugins to appropriate adapters (Kernel-SSH for host probes, Proxmox for cluster probes, network primitives for cold scans). Manages timeouts, retries, parallel execution. Channels probe output into Observation rows.
- **Reconciler** — the most important component. Reads recent Observations, applies precedence rules (kernel beats management-plane, recent beats stale, verified beats inferred), updates harness DB inventory and NetBox via the adapter. Generates Findings for things that don't reconcile cleanly.
- **AssertionEngine** — runs ConfigurationAssertion verifiers on schedule, records AssertionRun rows, produces Findings on failure.
- **Scheduler** — cron-like + event-driven. Runs probes by schedule; runs reconciliation after every probe batch; runs assertion verifiers on their declared cadence. Implementation: APScheduler is sufficient for MVP scale.
- **ProposalManager** — accepts proposals from Reconciler, AssertionEngine, and (later) agents; writes ProposalLog rows; surfaces pending proposals via API.
- **FindingGenerator** — small library used by the others. Deterministic fingerprint, dedup, status transitions.

### Probe layer (plugins, from day one)

Each probe is a class implementing the `Probe` base:

```python
class Probe(ABC):
    name: ClassVar[str]
    version: ClassVar[str]
    schema_version: ClassVar[int]
    required_privilege: ClassVar[PrivilegeLevel]
    produces_keys: ClassVar[list[str]]
    target_kinds: ClassVar[list[str]]  # "host", "network", "cluster"

    @abstractmethod
    async def run(self, ctx: ProbeContext) -> ProbeResult: ...
```

Probes register at startup via entry points (`homelab_helper.probes`). The framework's own probes ship via the same mechanism — eating its own dogfood. The plugin SDK is its own design artifact (deferred from this doc; will get its own spec).

Probe categories:
- **Network probes** (cold): subnet scan, fingerprint, port enumeration.
- **Host probes** (warm): CPU, memory, storage, network, PCI, GPU, sensors, services.
- **Cluster probes**: Proxmox cluster state, K8s node state, Ceph health.
- **Service probes**: targeted health checks (HTTP, DB ping, etc.).
- **Assertion probes**: the verifiers behind ConfigurationAssertions.

### Agent layer (Phase 4+)

- **LLMRouter** — single entry point for every LLM call. Inputs: task class, minimum capability tier, privacy constraint. Output: response from the best available backend. Backends: `OllamaBackend` (default), `AnthropicBackend`, `OpenAIBackend`, `OpenAICompatibleBackend` (covers vLLM, llama.cpp server, LM Studio, etc.). Never silently sends to cloud when policy says local-only.
- **Narrator** — turns Findings into prose. Lowest-stakes agent. Tiny/Small tier sufficient.
- **Conversational Discovery Agent** — interviews the user about their lab. Extracts intent, asks about hardware, resolves ambiguity. Small/Mid tier.
- **Skill Inferer** — passive; watches the conversation, updates skill profile. Soft signal only.
- **Planner (P5)** — invokes the constraint solver, narrates results. The solver is deterministic Python (OR-Tools or similar); the LLM is the explainer. Mid/Frontier tier.
- **Incident Triage (P7+)** — ingests alerts + recent changes + logs, proposes fixes. Mid/Frontier tier.
- **Code Generator (P6+)** — produces Ansible roles, docker-compose, K8s manifests. Mid/Frontier tier with verifier loop.

The agent layer is read-only by default. Agents can call adapter `query` methods (mediated by the engine) but never `write`. Writes happen via ProposalManager, which produces ProposalLog rows that humans act on.

### Interfaces

- **CLI** — `helper` command. Built with Click or Typer. Primary interface for MVP. Verbs: `discover`, `audit`, `inventory`, `findings`, `assert`, `propose`, `agent enroll`, `host show`, `config`.
- **HTTP API** — FastAPI. Powers the UI and external consumers (including MCP server). OpenAPI spec generated. Auth: token-based, scoped.
- **Web UI** — deferred to Phase 4 unless something simpler is needed earlier. SvelteKit or Next.js. Reuses the HTTP API.
- **MCP Server** — Phase 4. Exposes engine and adapter capabilities as MCP tools so Claude Code / Cursor / Claude Desktop users drive the harness via natural language without going through the framework's own agent layer.

### helper-agent (continuous-mode daemon)

Runs on each managed host. Tiny Python program packaged as a single binary (PyInstaller or Nuitka, or static-linked Go if the deployment story gets ugly).

- **Footprint**: target <50 MB RAM, near-zero CPU at idle. Must work on a Pi 4B.
- **Schedule**: runs a configurable subset of host probes every N minutes (default 5). Posts observations to helper-server.
- **Auth**: bearer token per host, generated during enrollment (`helper agent enroll <hostname>`).
- **Self-update**: pull-based — agent checks helper-server for new probe versions and updates its local probe bundle. Server-pushed updates are out of scope for MVP (security implications).
- **Subnet scan from inside the VLAN**: optional capability, lets the agent do cold discovery on its local network and post results back. Useful for VLAN-isolated segments.
- **Backpressure**: if helper-server is unreachable, observations buffer locally on disk (capped); when connection returns, drain in order.
- **Power-policy aware**: an agent on a `wol-on-demand` host doesn't try to phone home during its expected-off windows.

The agent is its own small product. Versioning and protocol compatibility with helper-server matter — semver discipline and a clear migration path are mandatory.

## Data flows

### Cold discovery

```
User: `helper discover network 10.250.6.0/23`
  │
  ▼
CLI → HTTP API → Scheduler
  │
  ▼
ProbeRunner picks `network.subnet-scan` probe
  │
  ▼
Network probe (running on helper-server) → asyncio TCP probe + fingerprint
  │
  ▼
Observations:
  - network.subnet.live_hosts: [...]
  - network.host.<ip>.open_ports: [...]
  - network.host.<ip>.fingerprint: "proxmox" | "k8s-api" | ...
  │
  ▼
Reconciler:
  - For each live host: lookup or create Host row (status=discovered)
  - NetBoxAdapter: create Device with status=staged
  - Generate INVENTORY_GAP finding for unfingerprinted hosts
  │
  ▼
User sees list of discovered hosts; can run warm discovery on each
```

### Warm discovery

```
User: `helper discover host bmax0 --user root --key ~/.ssh/homelab`
  │
  ▼
CLI → HTTP API → ProbeRunner
  │
  ▼
ProbeRunner picks all probes for target_kinds=["host"]
ordered by dependency (identity → CPU → memory → storage → network → ...)
  │
  ▼
Single SSH session (via KernelSSHAdapter) runs probes in sequence
  │
  ▼
Each probe produces Pydantic-validated output → Observation rows
  │
  ▼
Reconciler:
  - Updates Host.capabilities, Host.arch, Host.discovery_last_run
  - For each part observed: lookup PhysicalPart (by serial/wwid), open/close Placements
  - NetBoxAdapter: update custom fields, sync InventoryItems
  - Generate findings for anomalies (e.g. missing e1000e service)
  - Run any ConfigurationAssertions scoped to this host
  │
  ▼
User sees host page with full inventory, capabilities, current findings
```

### Continuous reconciliation (Phase 2+)

```
helper-agent on bmax3 (every 5 min):
  │
  ▼
Runs local probe bundle, gets fresh observations
  │
  ▼
POST /api/v1/observations → helper-server
  │
  ▼
Reconciler diffs against last-known state for each (target, key)
  │
  ▼
Significant deltas → updates inventory + findings
Trivial deltas (e.g. uptime tick) → bumps last_verified only
Confidence decay: any observation older than freshness window → Confidence.STALE
  │
  ▼
Periodic full sweep (every N minutes / hour / day):
  AssertionEngine runs all enabled assertions
  Generates findings for drift
```

### Conversational (Phase 4+)

```
User in UI: "What's wrong with my Ceph cluster?"
  │
  ▼
Web UI → HTTP API → Conversational Discovery Agent (via LLMRouter)
  │
  ▼
LLMRouter selects backend (Ollama by default; cloud if policy permits and capability required)
  │
  ▼
Agent calls tools: query_findings(kind=CEPH_*), query_observations(key=ceph.*),
                   query_inventory(role=proxmox-node)
  │
  ▼
Tools route to Engine (read-only paths)
  │
  ▼
Engine returns synthesized view (NOT raw observations — already-reconciled facts)
  │
  ▼
Agent synthesizes prose answer with citations to specific Findings and Observations
  │
  ▼
User drills into a Finding; can mark acknowledged/resolved or accept a Proposal
```

## Deployment topology

### Single-host development (your laptop, or one of the bmax)

```
┌───────────────────────────────────────────────────┐
│  Dev host                                         │
│  ├─ helper-server (Python, FastAPI, port 8080)    │
│  ├─ harness DB (SQLite, on disk)                  │
│  ├─ Ollama (optional, port 11434)                 │
│  └─ NetBox (Docker compose, port 8000)            │
│                                                   │
│  Probes run from helper-server over SSH to       │
│  target hosts on the LAN.                         │
└───────────────────────────────────────────────────┘
```

Sufficient for Phase 1. Discovery is invoked manually via CLI.

### Production homelab

```
┌──────────────────────────────────┐
│  helper-server (a VM or LXC)     │
│  ├─ helper-server process        │
│  ├─ harness DB (Postgres or      │
│  │   SQLite)                     │
│  ├─ APScheduler                  │
│  └─ HTTP API + Web UI            │
└──────────┬───────────────────────┘
           │
           │ HTTPS (token auth)
           │
┌──────────┴───────────────────────┐         ┌──────────────────────┐
│  helper-agent (per host)         │ ◀──────▶│  NetBox (separate)   │
│  ├─ systemd unit                 │         └──────────────────────┘
│  ├─ Local probe runner           │
│  └─ Buffered observation queue   │         ┌──────────────────────┐
└──────────────────────────────────┘ ◀──────▶│  Ollama (local or    │
           ↑                                  │   shared GPU host)   │
           │                                  └──────────────────────┘
           │                                  ┌──────────────────────┐
           └──────────────────────────────────│  Proxmox / K8s /     │
                                              │   UniFi APIs (P3)   │
                                              └──────────────────────┘
```

Phase 2+ topology. Agent runs on each managed host. helper-server is the single coordination point.

### Multi-site (the Wyola case)

helper-server stays at the primary site (Covington). Agents at Wyola post observations over the site-to-site VPN. Adapters at the primary site that need to reach Wyola (none in MVP) would also traverse the VPN. NetBox Sites + Locations model the topology; the agent doesn't care about geography, just network reachability.

## Security & trust

### Credentials

- **Never in harness DB.** Only `credentials_ref` columns pointing to the secrets store.
- **Secrets store options** (in order of friction): encrypted file with OS-keyring-stored key, SOPS, age, HashiCorp Vault, 1Password CLI, Bitwarden CLI.
- **SSH credentials**: helper-server holds the SSH key(s) for probe access; helper-agent doesn't need SSH (it runs locally).
- **API tokens** (NetBox, Proxmox, K8s, UniFi, Cloudflare): scoped read+limited-write per L1 policy. NetBox token has write scope to Devices, Custom Fields, InventoryItems, Services. Everything else: read-only.
- **Agent enrollment**: `helper agent enroll <hostname>` generates a per-host bearer token. Token gives the agent permission to post observations *for that specific host only*; can't post observations claiming to be from another host.

### Trust boundaries

| Component | Trust posture |
|---|---|
| helper-server | Fully trusted; holds keys |
| helper-agent | Trusted within its host's scope |
| NetBox | Trusted as far as its API token allows |
| Proxmox / K8s / UniFi / Cloudflare | Trusted as far as their API tokens allow |
| Local LLM (Ollama) | Untrusted; never gets raw credentials |
| Cloud LLM (Anthropic/OpenAI) | Untrusted; never gets credentials or PII the user hasn't authorized |
| User chat input | Untrusted; treat as data, not commands |

The LLM never sees raw secrets, raw SSH output, or anything that hasn't been through the reconciler. Agents read *synthesized inventory views* — facts with confidence scores and provenance, not arbitrary tool output. This protects against prompt injection via probed content.

### L1 enforcement

- Engine produces Proposals (ProposalLog rows). No code path applies Proposals automatically.
- The only writes the engine performs are:
  - Harness DB tables (inventory, findings, proposals — harness's own state)
  - NetBox custom fields and InventoryItems (per the NetBox sync invariants in the schema doc)
- Adapters for Proxmox/K8s/UniFi/Cloudflare have their write methods *implemented* (for L2-ready interfaces) but their dispatcher gates them behind a policy check that always returns False at L1.
- Future L2 lift is the trust gradient (below) plus an Executor that consumes pending proposals — not a re-architecture. At L1, the gradient is present but every cell is pinned to `PROPOSE`.

### Trust gradient (L2 authorization model)

The trust gradient is the deterministic policy that decides whether a proposed change may execute. It is the *only* gate in front of every adapter write path once L2 lands. Two trust concepts live in this system and must not be conflated:

- **Trust boundaries** (the table above) are an *information-flow* concept — who gets secrets, who is an attack surface.
- **The trust gradient** is an *action-authority* concept — how much the framework may change without asking.

They intersect at one invariant, the most important rule in the whole L2 design:

> **The LLM is on the untrusted side of the boundary, so an LLM is never in the path that authorizes execution.** An agent may *draft* an action manifest; a deterministic policy function decides whether it runs. This is the reconciler/narrator split extended to actions: deterministic core, LLM explains.

#### The decision function

```
decide(action, context) → BLOCK | PROPOSE | CONFIRM | AUTONOMOUS
```

| Level | Meaning |
|---|---|
| `BLOCK` | Forbidden by policy. |
| `PROPOSE` | Log it; a human applies it by hand. **This is all of L1.** |
| `CONFIRM` | System can execute, but each action needs explicit per-action approval. Proves the execution + rollback machinery under a human gate. |
| `AUTONOMOUS` | Auto-executes, emits a receipt, notifies, can auto-rollback. |

L1 is this function with every cell pinned to `PROPOSE`. L2 is raising that floor deliberately, per cell, opt-in. A user who never opts in keeps exactly the propose-only product. `decide()` is pure, deterministic Python — it never calls an LLM.

Inputs (five axes; four already live on `ProposalLog`): **domain · user-trust-in-this-cell · blast radius · reversibility · provenance.**

#### The promotable unit is a narrow cell

```
cell = (domain × action-kind × blast-radius)
```

Trust accrues per cell, not per domain, so a clean track record on `containers / restart / single-host` never authorizes `containers / recreate / single-host` — a different cell that earns its trust separately. Two ways a cell's floor rises:

- **Auto-escalation** — applies *only* to reversible + low-blast cells (`blast ∈ {metadata-only, single-host}`): after N clean approvals the cell's default level rises one step. **One bad outcome instantly demotes** the cell to `PROPOSE` and flags it on probation. Trust accrues slowly, evaporates instantly.
- **Explicit grant** — every other cell requires a durable owner grant to leave `PROPOSE`, and never auto-promotes.

#### Three tiers of floor hardness

This is the spine of the safety model:

| Tier | What | How it is crossed |
|---|---|---|
| **Cell default** | the level auto-escalation and grants move | rises slowly, falls instantly |
| **Soft-hard floors** | verified-rollback requirement; non-absolute per-host ceiling | per-action owner **override**, or an open **elevation window** |
| **Absolute floors** | `secrets`, and any owner-marked window-proof host/domain | **only** by editing the policy config out-of-band — no runtime gesture, ever |

Without the absolute tier, the only backstop is the reactive kill switch; with it, `secrets` and "my production NAS, never under any circumstances" are preventively unreachable by any window or override.

#### Override and elevation window

Both cross *soft-hard* floors only — never an absolute floor — and both are **owner-only, interactive, high-friction, and logged**. Neither is ever available to an agent, and neither is ever self-invoked by autonomous execution.

- **Override** — a single action, past a soft-hard floor, with an explicit logged "I accept this." Recorded as a distinct `TrustHistory` event.
- **Elevation window** — a time-boxed lift of soft-hard floors, **scoped** to specific cells/hosts (never blanket), with **hard expiry and no auto-renew**, a **live kill switch** (halts in-flight autonomous actions at the next checkpoint), and a **best-effort snapshot captured before each action** even though rollback isn't required. Every action under a window is tagged with the window id for post-hoc audit. Outside any open window, autonomous-meets-floor **degrades to `CONFIRM`** — the system asks rather than crossing the floor on its own.

Schema carriers (`Domain`, `CellTrust`, `TrustBoundary`, `ElevationWindow`, `TrustHistory`) and their semantics are specified in `harness-schema-slice1.md` under "Trust gradient tables."

## Failure modes

### Unreachable host during scheduled probe

DiscoveryRun records `success=false`. Stale Observations decay to `Confidence.STALE` per freshness window. After threshold, generates `INVENTORY_GAP` or `HOST_UNREACHABLE` finding. **Power-policy-aware**: WoL hosts with `expected_power_state=off` don't trigger this — their unreachability is *confirmation*.

### NetBox unavailable

NetBoxAdapter circuit-breaks. Writes queue locally (in-memory; consider durable queue if outages get common). Reads fall back to harness-DB cache of last-known NetBox state where available. After threshold (e.g. 1 hour), produces a finding. Operator action: restore NetBox or accept the gap.

### Reconciliation conflict (NetBox hand-edited mid-flight)

Reconciler detects: harness expected to write value X based on observation, but NetBox currently has value Y, and Y wasn't written by the harness. Logs `finding.kind=NETBOX_DIVERGENCE`. **Does not overwrite.** Operator resolves: accept harness view, accept NetBox view, or manual merge.

### Probe failures

Per-probe error logged in DiscoveryRun. Other probes in the same run continue. Repeated failures across runs produce `finding.kind=PROBE_FAILING` against the affected host/probe pair. Probe failure rate is itself a health metric for the harness.

### helper-agent loses connection to server

Local probe runs continue. Observations buffer to disk (capped — oldest dropped first if cap exceeded). On reconnect, agent drains buffer in order, marked with `recorded_at` from when the probe ran (not when the post happened). Reconciler tolerates out-of-order observations gracefully.

### LLM router has no available backend matching policy

User asked for `strict-local` but the requested task needs Frontier capability and no local model qualifies. Router refuses cleanly with a message: "this task requires Mid+ capability; your privacy policy is strict-local; available local: Ollama with llama3.2:8b (Tiny). Options: lower the task, change policy, install a better local model." Never silently downgrades quality without telling the user.

## Mapping decisions to architecture

| Decision | Architectural consequence |
|---|---|
| NetBox in-the-loop | NetBoxAdapter is the only adapter with both read+write at L1. Reconciler is authoritative for harness-owned fields; NetBox is authoritative for native fields; custom fields are harness-authoritative writes. |
| Agent for continuous mode | helper-agent is a distinct component with its own protocol versioning, deployment, enrollment. Observation table accepts bulk POSTs; backpressure handled at agent. |
| Plugin-from-day-one probes | Probe base class + entry-point registration in Phase 1. Framework's own probes use the same path — no internal/external distinction. |
| Harness DB for plugin objects | No NetBox plugins required. NetBox stays vanilla; harness ships a Postgres/SQLite schema. Trade-off accepted: harness DB needs its own migration discipline. |
| No K8s pod-level modeling | K8sAdapter keeps `local_context_data` updated on logical service VMs; doesn't sync pods. Scheduler-driven workload placement reasoning happens in the planner (P5), not in inventory. |
| Cables chokepoints only | NetBox cable model lightly used; topology queries traverse only modeled cables. NetworkPath abstraction (P5) consumes this graph. |
| Ollama default | LLMRouter ships with OllamaBackend pre-configured. First-run experience auto-detects local Ollama or prompts to install. |
| L1 default, L2 gated | ProposalManager exists from day one; the Executor lands in Phase 6 behind the trust gradient (`decide()`). Action manifest schema is part of ProposalLog from day one, so L2 is a wire-up. Phases 1–5 keep every cell pinned to `PROPOSE`. |
| Inventory MVP | Phase 1 boundary is "discovery + inventory + day-one audit produces the 11 findings against dorktool." Everything else is Phase 2+. |

## What's deliberately not in this architecture document

- **Schema specifics** — see `harness-schema-slice1.md`.
- **NetBox custom field definitions** — see the NetBox modeling walkthrough.
- **Probe plugin SDK** — its own design artifact, deferred.
- **LLM router policy engine details** — deferred to Phase 4 design.
- **Workload profile library schema** — Phase 5 design.
- **Solver design (OR-Tools or otherwise)** — Phase 5 design.
- **L2 executor design** — out of roadmap scope.

Each of those is a separate design artifact. The architecture above is stable across them; they're details that fill in specific layers.
