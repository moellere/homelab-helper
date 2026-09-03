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

- [x] **Landed.** `list_workloads`, `recommend_placement`, `plan_rebalance`,
  `analyze_bottlenecks`, `analyze_surplus`, `network_path`, and `probe_talos`.
  One deviation from the table below: Talos is its own `probe_talos` tool
  rather than a `run_discovery` target, because it names a host and so gets
  the same allow-list scoping as `probe_host` (the `discover talos` CLI verb
  now shares `engine/talos_probe.py` with it).

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

- [x] **v1 landed.** `adapters/homeassistant.py`, `engine/hass_import.py`,
  `helper discover hass [--persist]`, `run_discovery("hass")`, the `hass`
  source in `helper config`. `DiscoverySource.HOME_ASSISTANT` shipped without
  a migration, following the repo precedent for enum members (SQLite's
  `SAEnum` has no CHECK constraint); a Postgres deployment needs the
  `ALTER TYPE` when that backend is first exercised. v2 (device registry,
  `device_tracker` identity) is still open.

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

- [x] **Landed.** README "Transport and trust" paragraph under the MCP
  section; `architecture.md` trust-boundaries row for the MCP client.

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

- [x] **Re-baselined and landed in reduced form.** Between the original
  scoping and this work, Phase 6 PRs B–F merged: `engine/executor.py`
  (Proxmox guest power via `vm_power`, snapshot capture + rollback,
  receipts), auto-escalation, elevation windows, the kill switch, and the
  per-action override. What was still missing and now exists:
  `engine/manifest.py` (the pydantic authoring schema; the executor's own
  `parse_manifest` stays as the untrusted-input validator, and a test holds
  the two in agreement) and `tests/test_write_isolation.py` (no module
  outside the executor/rollback pair may name a Proxmox write method — the
  block-comment contract is now mechanical).
- **Deliberately not built:** the `(domain, action_kind) → callable`
  registry (one write adapter does not justify it; add it with the second),
  and the SSH `containers/restart/single-host` cell (it needs a rollback
  strategy the orchestrator doesn't have yet, and every new write path is
  behind the live-fleet validation gate). NetBox custom-field/VM sync keeps
  its own diff/confirm path, as the backlog already records.

**Why.** No adapter had a write method when this was scoped; the executor
now exists, so the work is keeping every *future* write path on the same
rails: a typed manifest at the authoring boundary, one enforcement point,
and a mechanical guard.

**Files (landed).** `engine/manifest.py`, `engine/executor.py` (imports the
shared `VM_KIND_DOMAIN`/`ManifestError`), `tests/test_manifest.py`,
`tests/test_write_isolation.py`.

**Effort (remaining).** Registry + SSH restart cell with a rollback
strategy: 1–2 weeks, after live-fleet validation.

## 5. `propose_action` for agents; never `exec`

- [x] **Landed.** MCP `propose_action(action_kind, node, vmid, vm_kind,
  title, description?, blast_radius="single-host", hostnames?)` validates
  through `engine/manifest.py`, writes a PENDING `ProposalLog` with
  `proposed_by="agent:mcp"`, and returns the pessimistic `decide()` preview
  plus the `helper exec run <id>` next step. `list_proposals(outcome?,
  limit?)` and `get_proposal(id | prefix)` are read-only. `trust_status`,
  `list_receipts`, and `pending_actions` had already landed with PR F. The
  roster test pins the tool set and the forbidden-substring test keeps
  `exec`/`grant`/`window`/`override`/`rollback` out of the MCP surface.
- **Dropped: the provenance clamp.** The original proposal would have
  capped agent-drafted actions at CONFIRM. The executor's tests, landed
  with PR B, deliberately run `agent:planner` proposals at AUTONOMOUS once
  a cell has earned it through clean confirmed runs — the gate is *cell
  trust*, not who drafted the manifest, and auto-escalation is the
  mechanism that makes an agent-drafted action trustworthy over time. A
  string-prefix clamp would contradict that design, so `provenance` stays
  informational (recorded on every receipt) and `decide()` is unchanged.

## 6. Secrets store

- [x] **Landed as secret references** (`homelab_helper/secrets.py`). Every
  secret-valued variable accepts a literal (unchanged behaviour) or a
  reference: `env:VAR`, `file:<path>#<key>` (plain YAML/JSON, `.age` via the
  operator's `age` binary and `HOMELAB_HELPER_AGE_IDENTITY`, `*.sops.*` or a
  `sops` marker via `sops -d`), or `keyring:<service>/<user>` (optional
  `[keyring]` extra). Adapters and LLM backends read through
  `secret_from_env`; `helper config` shows `set via file|keyring|env`;
  `helper config init` documents the forms. `redact()` scrubs every
  resolved value from the MCP surface's error strings.
- **Not built:** `Host.credentials_ref` is still the literal
  `ssh:<user>:<key-path>` string (nothing reads it); moving SSH key paths
  onto references is a separate, small change. `op://` and `vault://` stay
  future schemes. Redaction covers MCP error returns; CLI output prints
  adapter errors unredacted (they rarely echo a header, but it is a gap).

**Why.** Every token lived in a plaintext `.env`. With execution now
possible, write-capable tokens exist, and a plaintext file must be a choice.

**Files (landed).** `secrets.py`, `config.py` (`reference` in
`variable_status`), `cli/config.py`, seven adapters, `llm/backends.py`,
`mcp_server.py` (redaction), `tests/test_secrets.py`, `pyproject.toml`
(`keyring` extra).

## Sequencing

| Order | Item | Blocks | Effort |
|---|---|---|---|
| 1 | 3. stdio-only note — **done** | nothing | 1 h |
| 2 | 1. planner tools + Talos — **done** | nothing | 1–2 d |
| 3 | 2. Home Assistant v1 — **done** | nothing | 2–3 d |
| 4 | 5. read-only half (`list_proposals`, `get_proposal`, `trust_status`) — **done** | nothing | 1 d |
| 5 | 6. secrets store — **done** (references; `credentials_ref` migration open) | item 4 merging | 3–5 d |
| 6 | 4. executor + first cell — **done via Phase 6 PRs B–F**; manifest schema + write-isolation test added; registry + SSH cell open | item 6 | 1–2 w |
| 7 | 5. `propose_action` — **done**; provenance clamp dropped (see §5) | item 4's manifest model | 2 d |
| 8 | 2. Home Assistant v2 | v1 | 2 d |

Items 1–4 in this order are all read-only and can ship to the test audience
as they land. Items 5–7 are the Phase 6 track and stay behind the
live-fleet validation gate in `backlog.md`.
