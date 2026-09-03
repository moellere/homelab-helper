# Agent access to infrastructure services — scope

Scoping for the six work items that came out of the pre-alpha readiness
review (September 2026). Each section says what the item is, the design it
should follow given the locked decisions in `architecture.md`, which files it
touches, how it is tested, and a rough effort at focused hobby pace. The
sequencing table at the end is the recommendation; nothing here is built yet
except where a checkbox says so.

Two blockers from the same review landed ahead of this doc and are not
scoped here: the packaged Alembic config (`db/migrate.py`; `helper db init`
works from a wheel) and the scoped MCP `probe_host` tool
(`HOMELAB_HELPER_MCP_PROBE_ALLOW`).

Current agent surface, for reference — the MCP server over stdio:

| Service | Adapter | MCP reach today | Writes |
|---|---|---|---|
| Proxmox, K8s, OMV, UniFi, Cloudflare, Argo CD | read-only | `run_discovery(<source>)` | none |
| Talos | read-only, `talosctl` | none (CLI only) | none |
| SSH hosts | `KernelSSHAdapter` | `probe_host` (scoped) | none |
| NetBox | read + custom-field/InventoryItem/VM sync | none (CLI only) | CLI `netbox sync-*` |
| Phase 5 planners | n/a | none (CLI only) | none |
| Home Assistant | none | none | none |

---

## 1. Planners as MCP tools (+ Talos in `run_discovery`)

**Why.** Placement, rebalance, bottlenecks, surplus, and network path exist
as `helper plan ...` / `helper bottlenecks` verbs but not as tools, so the
recommendation engine — the most useful thing an agent could reach — is
agent-invisible.

**Design.** One tool per planner, each a thin wrapper over the existing
engine entry point, returning the deterministic report as a dict. The CLI's
`--narrate` path (Planner agent via `LLMRouter`) is *not* mirrored: the MCP
client's own model narrates, and the harness never spends LLM calls on a
tool's behalf.

| Tool | Engine call | Notes |
|---|---|---|
| `list_workloads(category=None)` | `load_workload_library()` | profile summaries |
| `recommend_placement(workload)` | `recommend_placement(session, profile, topology)` | unknown workload → `{"error"}` |
| `plan_rebalance()` | `plan_rebalance(session)` | three candidate plans + tradeoffs |
| `analyze_bottlenecks(persist=False)` | `analyze_bottlenecks` / `persist_bottlenecks` | `persist` writes findings (harness DB only) |
| `analyze_surplus()` | `analyze_surplus(session)` | |
| `network_path(host_a, host_b, workload=None)` | `Topology.path` + `network_verdict` | topology from `HOMELAB_HELPER_NETWORK_TOPOLOGY`; missing → `{"error"}` |
| `run_discovery("talos", node=...)` | the `discover talos` sequence | needs a node name; extend the tool signature with an optional `target` |

Every report dataclass needs an `as_dict()` mirroring `HostProbeResult.as_dict`
(JSON-safe, enums as values). Keep the lookup-miss-returns-error convention.

**Files.** `mcp_server.py`; `engine/{placement,rebalance,bottlenecks,reconfigure,network_path}.py`
(`as_dict` methods); `tests/test_mcp_server.py` (roster + one seeded call
per tool, reusing `test_cli_plan.py`'s fixtures); README roster.

**Effort.** 1–2 days. No new dependencies, no schema change.

## 2. Home Assistant adapter (read-only)

**Why.** HA is the most common service in the target audience's labs and the
only one on the list with no adapter. It also carries cross-source signal
the harness wants: which integrations HA has configured (a second opinion on
what exists), and device-tracker entities that map hostnames to IPs.

**Design.** Same pattern as UniFi/OMV: httpx client, injected for tests with
`MockTransport`, read-only, `health_check()`, `_owns_client` semantics.

- Config: `HOMELAB_HELPER_HASS_URL`, `HOMELAB_HELPER_HASS_TOKEN` (a long-lived
  access token, secret), `HOMELAB_HELPER_HASS_VERIFY_SSL`. Register in
  `config.SOURCES`.
- Reads, v1 (REST only): `GET /api/` (liveness), `GET /api/config` (version,
  `location_name`, `components`), `GET /api/states` (entity states; summarize
  by domain), `GET /api/services` (available service domains).
- Persisted, v1: a `Service("home-assistant")` with `attributes` = version +
  component list + entity counts by domain, and an internal `ServiceEndpoint`
  for the HA URL. Integrations that name another management plane the harness
  knows (`proxmoxve`, `unifi`, `mqtt`, `esphome`, `zha`, …) land in the
  service's attributes for the stray-config / cross-resolver pass to compare
  against. `DiscoverySource` gains `HOME_ASSISTANT`.
- v2 (deferred, separate PR): the device registry over the websocket API
  (`config/device_registry/list`) and `device_tracker.*` entities with
  `ip` attributes as a client-identity source alongside UniFi clients.
- Writes: none. When Phase 6 reaches HA, service calls are a new trust domain
  (`automation`) rather than `containers`; out of scope here.

`DiscoverySource` is a SQLAlchemy `Enum` column: on SQLite adding a member is
code-only, on Postgres it is an `ALTER TYPE ... ADD VALUE` migration. Ship the
migration either way so the two backends stay aligned.

**Files.** `adapters/homeassistant.py`; `engine/hass_import.py` (persist);
`cli/discover.py` (`discover hass`); `config.py` (`SOURCES`);
`mcp_server.py` (`run_discovery("hass")`); `db/enums.py` + migration;
`tests/test_homeassistant_adapter.py`, `tests/test_cli_hass.py`; README
credential-scope note (a token from a non-admin HA user is sufficient).

**Effort.** 2–3 days for v1; 2 days for v2.

## 3. MCP transport stays stdio — document, don't build

**Why.** Stdio inherits the operator's shell and `.env`, which is the right
trust boundary for the test audience: the MCP client is the same person as
the operator. A network transport would put the harness's SSH key and every
source token behind whatever auth the transport has.

**Design.** No code. A short "Transport and trust" subsection in the README's
MCP section and a line in `architecture.md` under "Trust boundaries" stating:
stdio only; the server process runs as the operator; client config blocks
must not carry secrets (the server reads `.env`); remote MCP (streamable
HTTP + bearer auth + TLS) is gated on the HTTP API, which is a stub, and is
not planned before Phase 6 ships.

What remote MCP *would* need, so the decision is recorded: the FastAPI layer
with token-scoped auth from the architecture doc; the MCP SDK's streamable
HTTP transport mounted on it; per-token tool allowlists (a remote client
should never see `probe_host`); and the secrets store from item 6.

**Effort.** One hour of docs.

## 4. Adapter write surfaces designed against trust cells

**Why.** No adapter has a write method today (the architecture doc's "gated
dispatcher" does not exist in code). When Phase 6's executor lands, writes
must be added per *cell*, not per adapter, so each one is authorized and
snapshotted the way `decide()` expects.

**Design.**

- **Manifest.** `ProposalLog.artifact` carries
  `{"domain", "action_kind", "blast_radius", "target": {...}, "params": {...},
  "rollback": {"verified": bool, "handle": ...}}`; `ActionRequest` is built
  from it. A pydantic model validates it at the boundary.
- **Registry.** `engine/executor.py` holds a dispatch table keyed by
  `(domain, action_kind)` → a write callable. Write callables live in
  `adapters/writes/<adapter>.py`, never on the read adapters, and are imported
  *only* by the executor. A regression test (same shape as the trust module's
  no-LLM import check) asserts nothing under `cli/`, `llm/`, or
  `mcp_server.py` imports `adapters.writes`.
- **Flow.** `helper exec <proposal>` → load `ActionRequest` from the manifest
  → `load_trust_context` → `decide()` → BLOCK/PROPOSE: stop; CONFIRM: prompt;
  AUTONOMOUS: run → snapshot (if the cell declares one) → write → receipt
  written back to `ProposalLog` (`outcome`, `outcome_by`, restore handle).
- **First cells**, per roadmap P6-AC2, and their write callables:

  | Cell | Callable | Snapshot | Rollback verified? |
  |---|---|---|---|
  | `containers/restart/single-host` | SSH `systemctl restart <unit>` / `docker restart <name>` | none | no → caps at CONFIRM |
  | `hypervisor/restart/single-host` | Proxmox `POST /nodes/{n}/{qemu\|lxc}/{vmid}/status/reboot` | Proxmox snapshot API before reboot | yes once snapshot verified |
  | `inventory-metadata/sync/metadata-only` | the existing NetBox `sync_host` | none needed | yes (idempotent re-sync) |

  Everything else stays PROPOSE until a cell is designed the same way.

**Files.** `engine/executor.py`, `engine/manifest.py`, `adapters/writes/`,
`cli/exec.py`, `tests/test_executor.py`, `tests/test_no_write_imports.py`.

**Effort.** Executor skeleton + the SSH restart cell under CONFIRM: 1–2
weeks (PR B in the Phase 6 sequence). Proxmox restart with snapshot: 3–5
days (PR C). Each later cell: 2–3 days.

## 5. `propose_action` for agents; never `exec`

**Why.** The architecture lets an agent *draft* an action manifest and never
authorize one. Today the MCP surface can do neither. Giving agents a
proposal tool closes the loop without touching the gate.

**Design.**

- `propose_action(domain, action_kind, blast_radius, title, target, params, description=None)`
  validates against the item-4 manifest model, writes a `ProposalLog` row
  with `proposed_by="mcp"` and the manifest's `provenance="agent"`, outcome
  PENDING, and returns the row id plus a **preview** of `decide()` (level +
  reasons) so the agent can tell the operator what would happen. It never
  executes.
- `list_proposals(outcome=None)`, `get_proposal(id)`: read-only.
- `trust_status()`: the `helper trust show` data, read-only, so an agent can
  explain the gradient's state.
- Deliberately absent, enforced by a roster test: `exec`, `grant`, `window`,
  `override`, `revoke`. These stay CLI-only and interactive.
- `decide()` already carries `provenance` on `ActionRequest`; the policy
  should clamp `provenance="agent"` one step below the cell level (an
  agent-drafted action never runs AUTONOMOUS on the strength of a cell the
  framework's own reconciler earned). Small change in `engine/trust.py` with
  a reason string.

**Files.** `mcp_server.py`, `engine/trust.py` (provenance clamp),
`tests/test_mcp_server.py`, `tests/test_trust.py`.

**Effort.** 2–3 days once item 4's manifest model exists; the read-only
tools can land earlier.

## 6. Secrets store

**Why.** Every token lives in a plaintext `.env` and `Host.credentials_ref`
is the string `ssh:<user>:<key-path>`. Acceptable for a read-only alpha, not
once any write-capable token exists — which is the moment item 4 merges.

**Design.**

- `secrets/` package with a `SecretsBackend` protocol: `resolve(ref) -> str`.
  `credentials_ref` values and source variables become URIs:
  `env:HOMELAB_HELPER_PROXMOX_TOKEN_SECRET` (default, today's behaviour),
  `file:<path>#<key>` (an age- or sops-encrypted YAML decrypted via the
  operator's `age`/`sops` binary, subprocess, never a vendored key),
  `keyring:<service>/<user>` (OS keyring via the `keyring` package),
  and later `op://` (1Password CLI) and `vault://`.
- `config.py` resolves through the backend; `SourceConfig.secret` variables
  accept either a literal (env backend) or a URI. `helper config` reports the
  backend and set/unset, never values. Redaction: a single `redact()` helper
  applied to every log line and MCP error string that could echo a header.
- Migration path: with no URI anywhere, behaviour is bit-for-bit today's.

**Files.** `secrets/{base,env,file,keyring}.py`, `config.py`, `cli/config.py`,
`adapters/*` (take resolved values, unchanged), `tests/test_secrets.py`.

**Effort.** 3–5 days for env + file + keyring. Required before item 4's PR B
merges.

---

## Sequencing

| Order | Item | Blocks | Effort |
|---|---|---|---|
| 1 | 3. stdio-only note | nothing | 1 h |
| 2 | 1. planner tools + Talos | nothing | 1–2 d |
| 3 | 2. Home Assistant v1 | nothing | 2–3 d |
| 4 | 5. read-only half (`list_proposals`, `get_proposal`, `trust_status`) | nothing | 1 d |
| 5 | 6. secrets store | item 4 merging | 3–5 d |
| 6 | 4. executor + first cell (Phase 6 PR B) | item 6 | 1–2 w |
| 7 | 5. `propose_action` + provenance clamp | item 4's manifest model | 2 d |
| 8 | 2. Home Assistant v2 | v1 | 2 d |

Items 1–4 in this order are all read-only and can ship to the test audience
as they land. Items 5–7 are the Phase 6 track and stay behind the
live-fleet validation gate in `backlog.md`.
