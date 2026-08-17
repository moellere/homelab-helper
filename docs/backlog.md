# homelab-helper — Backlog

The concrete, actionable task list. `roadmap.md` is the narrative (phases,
rationale, stop-here value); this is the punch list of what's actually left,
grounded in the current state of `src/`. Two scopes are tracked here today:
the remaining **Phase 1** work, and the **Phase 6** trust-gradient build.

Priorities are *within* a phase: **P0** = on the critical path, **P1** =
needed to meet the phase's acceptance criteria, **P2** = strengthens but
doesn't block. Acceptance-criterion references (AC1–AC5, P6-AC1–6) point at
`roadmap.md`.

---

## Current status snapshot

**Phase 1 ≈ 30–40% by effort.** The evidence-collection half is built; the
make-sense-of-it half is not.

Built and green (42 tests passing):

- [x] Slice 1 schema — all 11 models + Alembic initial migration
- [x] Probe plugin SDK (`probes/base.py`, `registry.py`, entry-point registration) — meets AC5
- [x] Warm host probes: `host.identity`, `host.cpu`, `host.memory`, `host.network`, `host.storage`, `host.services`
- [x] `KernelSSHAdapter` (shared SSH session) + `ProbeRunner` (probe → Observation rows)
- [x] `FingerprintGenerator` (exists; not yet consumed)
- [x] CLI: `version`, `db init|status|reset|migrate`, `discover host`, `discover show`, `probes`

The rest of Phase 1, and all of Phase 6, is below.

---

## Phase 1 — remaining work

### P0 — critical path

- [ ] **Reconciler** (`engine/reconciler.py`) — the keystone; everything below leans on it.
  - [x] Plug-rule architecture (`HostProjectionRule` registry) — extensible by appending entries; future probes don't change the reconciler core
  - [x] `host.identity.*` slice → `Host.capabilities` projection + freshness markers (`discovery_last_run`, `last_verified`)
  - [x] Latest-observation-per-key precedence (single-source; multi-source lands with non-SSH probes)
  - [x] Host-field idempotency: re-run is a no-op (returns empty deltas)
  - [x] CLI wiring: `helper discover host` invokes the reconciler after the probe batch
  - [x] Replay-style test pattern (in-process fixtures); migrates to YAML when example fixture lands
  - [x] `transform` hook on `HostProjectionRule` (raw observation value → typed column domain) + `normalize_arch` mapper
  - [x] `host.cpu.*` slice: `host.cpu.architecture` → `Host.arch` (typed); model, vendor, sockets, cores, threads, threads_per_core, freq, cache, flags, interesting_flags → capabilities (`cpu_*` keys)
  - [x] `host.memory.*` slice (totals only): mem/swap/hugepage totals from `/proc/meminfo` → capabilities. DIMM-level keys land with the lineage slice below as first-class rows, not capabilities.
  - [x] DIMM `PhysicalPart` / `Placement` lineage: `host.memory.dimms` observation contract (list of populated-slot dicts) → serial-keyed `PhysicalPart` upsert with field enrichment; append-only `Placement` open/close; cross-host move closes prior placement; DIMMs without serial counted in `parts_skipped_no_identity`; no-DIMM-observation safety (doesn't close existing placements). Future dmidecode-driven probe will emit the observation contract.
  - [x] `host.storage.*` slice: scalar projections (`disk_count`, `disk_names`, `total_disk_bytes` → capabilities) + per-device `PhysicalPart` / `Placement` lineage from `host.storage.devices`. WWN-preferred / serial-fallback identity (USB enclosures forge serials); kind dispatched per-device (NVMe/SSD/HDD/OTHER) with reclassification on better evidence; partitions/LVM/RAID ignored. Kind-filtered close-loop prevents DIMM reconcile from disturbing storage placements (latent bug fix landed in this slice).
  - [x] `host.network.*` slice: scalar projections (`network_interface_count`, `network_interface_names` → capabilities) + NIC `PhysicalPart` / `Placement` lineage from `host.network.interfaces`. MAC-keyed (lowercased into the `serial` column); virtual interfaces filtered by name prefix and counted in `parts_skipped_filtered` (distinct from `parts_skipped_no_identity`); slot is the kernel interface name. Cross-kind regression tests confirm storage reconcile doesn't disturb NIC placements (and vice versa). Future probe version emitting PCI addresses can replace the prefix heuristic with a positive has-PCI-backing test.
  - [x] Multi-source precedence rules: highest-confidence observation per (host, key) wins (VERIFIED > ASSERTED > INFERRED > STALE), recency only breaks ties within a tier — so a management-plane source fills gaps but never clobbers a kernel-verified fact (`_obs_precedence`/`_best_per_key`)
  - [x] Finding generation with deterministic fingerprint dedup: `INVENTORY_GAP` emitted per skipped part (DIMM no serial, storage no WWN+serial, NIC no MAC); fingerprint = `sha256(kind|host|root-cause)[:16]`; idempotent re-run hits the same row and bumps `last_seen`.
  - [x] Finding-level idempotency: re-runs update `last_seen`, don't duplicate (AC4). Plus auto-resolve when a previously-flagged condition clears, scoped per-category so an absent observation never silently auto-resolves findings of that category. Resolved findings reopen with the same fingerprint when the condition recurs.
  - _Unblocks AC2, AC3, AC4. Expect to rewrite parts of it twice (per roadmap risk note); build the replay test fixture alongside it._

### P1 — needed for the acceptance criteria

- [x] **NetBoxAdapter — first slice** (`adapters/netbox.py`): httpx-based async client; Device CRUD (list / get-by-name / update); custom-field CRUD (list / create); pagination; `Authorization: Token` auth; config via `HOMELAB_HELPER_NETBOX_URL` + `HOMELAB_HELPER_NETBOX_TOKEN` env vars; structured `NetBoxAPIError` carrying status + method + path. `sync_host(host)` PATCHes a Device's custom fields by hostname match (won't create Devices — schema doc invariant: NetBox owns canonical inventory facts, harness owns CF values). _Unblocks AC1/AC2's NetBox push (partial — Device CF surface only)._
  - [x] **Clusters + VirtualMachines CRUD** + `sync_cluster_vms` (Phase-3
    virtualization slice; create+update only, never reaps operator VMs)
  - [x] **Harness-row VM sync + id write-back** (`engine/netbox_vm_sync.py` +
    `helper netbox sync-cluster <name>`) — syncs the persisted `VirtualMachine`
    rows into NetBox and writes `netbox_cluster_id`/`netbox_vm_id` back, so
    re-syncs match by id (rename-safe). Completes the Proxmox→harness→NetBox
    round-trip. `--dry-run` previews + rolls back.
  - [ ] Interfaces / IPs / VLANs / Prefixes / Services CRUD
  - [x] **InventoryItem CRUD + reconciler write path**: list / create / update / delete via `/api/dcim/inventory-items/`; `sync_inventory_items(device_id, placements)` diffs against existing `discovered=True` items (slot label = diff key) and applies create/update/delete. `helper netbox sync-host` now runs both passes by default (`--skip-fields` / `--skip-inventory` for either alone). Human-edited InventoryItems are invisible to the sync because the list query passes `discovered=true` — sync never reaps an operator's hand-entered row.
  - [x] `NETBOX_DIVERGENCE` finding on hand-edit conflict (skip write, never
    overwrite) — three-way merge in the VM sync: a per-VM `netbox_baseline`
    (last write) on `VirtualMachine.attributes`; when NetBox's current state
    diverges from it, an operator hand-edited it → skip the write, raise a
    `NETBOX_DIVERGENCE` (MEDIUM) finding (reopen/resolve lifecycle). New
    `FindingKind.NETBOX_DIVERGENCE` (no migration — SQLite SAEnum has no CHECK).
- [x] **NetBox bootstrap** — `bootstrap_custom_fields()` adapter method + `helper netbox bootstrap [--dry-run]` CLI verb. Idempotent: lists existing CFs first, creates only what's missing; re-running after upstream NetBox upgrades is safe. Ships 10 Device CFs (power policy/state, discovery source/last-run, last-verified, capabilities, arch, hypervisor type, idle/max power draw). Schema doc open-question #4 resolved in favour of bootstrap.
- [x] **AssertionEngine** (`engine/assertions.py`, one-shot mode) — verifier dispatch (`OBSERVATION_PREDICATE` fully wired; `SSH_COMMAND`/`HTTP_CHECK`/`API_QUERY`/`FILE_HASH` SKIP with a clear reason until their adapter slices land), `AssertionRun` rows persisted, `CONFIG_DRIFT` finding lifecycle on FAIL (deterministic fingerprint dedup, reopen-on-recurrence) and auto-resolve on PASS. CLI: `helper assert list|show|run` with `--name` / `--all` / `--include-disabled`, non-zero exit on FAIL/ERROR for CI gating.
- [x] **Seed assertion library** (`engine/assertion_library.py` + `fixtures/assertion-library-starter.yaml`) — YAML schema (v1) + idempotent upsert-by-name loader; `helper assert load <path> [--dry-run]` verb. Starter pack ships 14 generic assertions across four categories (probe coverage, sane baselines, architecture/capability, network basics) that exercise the OBSERVATION_PREDICATE verifier against keys the existing probes produce. Hostname references resolve to Host UUIDs at load time; missing hosts skip with a clear reason rather than erroring (tolerant by design — land the library, discover hosts, re-load). _Combined with the reconciler's INVENTORY_GAP findings, `helper audit` now reports the day-one finding corpus once a fleet is seeded — partial AC3._
- [x] **Network probes** — `network.subnet-scan` (asyncio TCP connect-scan against a configurable port panel, bounded concurrency, no nmap/no raw sockets) and `network.fingerprint` (SSH banner + HTTP `Server` header + port heuristic; identifies Proxmox VE, Cockpit, K8s API). Probes wired through the existing SDK + entry-points. _Unblocks AC1. NetBox push still pending the adapter slice._
- [ ] **CLI verbs**:
  - [x] `helper discover network <cidr> [--ports a,b,c] [--timeout SECS] [--concurrency N] [--no-fingerprint]` — runs subnet-scan, fingerprints each discovered host on its observed open ports, prints the result table. Persists observations through the existing ProbeRunner.
  - [x] `helper audit` — high-level roll-up (inventory counts, severity × status crosstab, top-N open findings) — AC3 read-side
  - [x] `helper findings list|show|ack|resolve|suppress` — fingerprint-prefix matching everywhere; status/severity/kind/host filters on list
  - [x] `helper host show <name>` — identity + capabilities (grouped by prefix) + current placements + open findings table
  - [x] `helper config` (`cli/config.py`) — read-only view of effective config
    (database URL + state, NetBox URL/token presence, verify-ssl, SSH key) and
    each setting's source (env/default/unset); tokens reported set/unset, never
    printed.
- [x] **Replayable lab fixture + loader** (`engine/lab_replay.py` +
  `fixtures/example-lab.yaml` + `helper discover replay`) — seeds Host rows +
  Observations from a committed synthetic fixture, reconciles each host, and
  loads+runs a bundled assertion library — no live SSH. The fixture yields 13
  day-one findings (inventory-gap + storage-provenance + config-drift, with an
  arch-scoped SKIP). Integration test `tests/test_lab_replay.py` asserts ≥11
  findings (AC3) and idempotent re-runs (AC4). The committed generic assertion
  library is `fixtures/assertion-library-starter.yaml`; operators bind it to
  their own hostnames in a local (un-committed) copy.

### Discovery sources & probes (landed)

- [x] **UniFi adapter** (`adapters/unifi.py` + `helper discover unifi`) — third
  management-plane source: read-only UniFi Network REST client (`X-API-KEY`
  auth, injectable for tests), reads internal DNS records, known clients
  (hostname↔IP), and network/VLAN definitions. Pure per-row parse functions;
  self-signed cert default. Read-only at L1. The DNS-record source that feeds
  internal `ServiceEndpoint` reconciliation.
- [x] **ServiceEndpoint reconciliation** (`db/models/service.py` +
  `engine/dns_reconcile.py` + migration `5372e3c99f7e`) — `Service` +
  `ServiceEndpoint` models capturing DNS split-brain (a hostname resolving to
  different IPs internally vs externally). `reconcile_internal_endpoints`
  upserts internal endpoints from a DNS source's A/AAAA records, keyed by
  `(service, scope, resolver, hostname)`; scope-disciplined so it never touches
  external/other-resolver endpoints, and cleans up orphaned Services on
  removal. `helper discover unifi --persist [--dry-run]` wires it. New
  `ResolutionScope` enum (internal/external).
- [x] **Cross-source view builder** (`cli/view.py` + `helper view
  service|host`) — the Phase-3 query surface. `view service <name>` synthesizes
  a service across its internal/external endpoints, makes the DNS split-brain
  explicit, and cross-references the VM/Host carrying the name (AC#2, minus the
  Cloudflare/ArgoCD columns which land with those adapters). `view host <name>`
  synthesizes the guests a host runs, endpoints resolving to it, and its open
  findings.
- [x] **Cloudflare adapter** (`adapters/cloudflare.py` + `helper discover
  cloudflare`) — the external half of the DNS split-brain: read-only Cloudflare
  v4 API client (scoped Bearer token, `Zone.DNS:Read`; zone-name→id resolution
  cached, or explicit zone id; paginated `list_dns_records`; success-envelope
  unwrapping; injectable for tests). `reconcile_external_endpoints`
  (`engine/dns_reconcile.py`, refactored to a scope-parametrized
  `reconcile_endpoints` core with internal/external wrappers) upserts
  `(scope=external, resolver=cloudflare)` endpoints; reciprocal scope discipline
  means an external sync never touches internal (UniFi) rows and vice versa, so a
  hostname carried by both resolvers pairs on one `Service` — ready for `view
  service` to flag. `helper discover cloudflare --persist [--dry-run]` wires it.
- [x] **Argo CD adapter** (`adapters/argocd.py` + `helper discover argocd`) — the
  git-desired-state source: read-only Argo CD API client (Bearer token,
  self-signed default, injectable for tests). `list_applications` pulls each
  Application's git source (repo/path/target revision) alongside Argo CD's own
  `sync`/`health` verdict and the individual out-of-sync resources;
  `application_is_drifted` flags `OutOfSync`/non-`Healthy` apps. `helper discover
  argocd` lists applications and highlights drift (read-only; drift→findings
  persistence is queued below).
- [x] **Kubernetes adapter** (`adapters/kubernetes.py` + `helper discover k8s`) —
  second management-plane source: read-only `kubectl`-subprocess client
  (injectable runner; kubeconfig/context config). `discover_k8s_nodes`
  (`engine/k8s_import.py`) records each node's facts as **INFERRED** Observations
  on its Host (matched by name/internal-ip) and reconciles — so K8s-only facts
  (kubelet/runtime versions, roles → capabilities `k8s_*`) enrich the host while
  the kernel-VERIFIED facts (kernel/arch/cpu/memory) hold by precedence.
  Live-validated against a real cluster (6 nodes). The first source that
  genuinely exercises multi-source precedence on shared hosts.
- [x] **Proxmox adapter** (`adapters/proxmox.py` + `helper discover proxmox`) —
  first management-plane source (Phase 3): read-only Proxmox VE REST client
  (API-token auth, injectable for tests), reads cluster status + VM/LXC + node +
  storage. Read-only at L1 (no mutate methods). Live-validated against a real
  4-node cluster over HTTPS with a PVEAuditor token.
- [x] **Virtualization schema + persistence** — `Cluster` + `VirtualMachine`
  models + Alembic migration `b2f1a9c7d3e4`; `engine/virt_reconcile.py` upserts
  them from Proxmox discovery (idempotent, keyed by name / (cluster, vmid);
  resolves a guest's node to its `Host`). `helper discover proxmox --persist`
  writes the cluster + guests to the harness DB; `--netbox-sync` proposes them
  into an existing NetBox cluster via `sync_cluster_vms`; `helper audit` now
  reports cluster/VM counts. Live-validated: cluster + 20 guests persisted,
  re-run idempotent.
- [x] **Talos adapter** (`adapters/talos.py` + `probes/talos/host.py` +
  `helper discover talos`) — `talosctl`-subprocess adapter (injectable runner
  for tests) + a `talos.host` probe that pulls COSI resources (`nodename`,
  `systeminformation`, `disks`, `links`, `addresses`) plus `/proc/cpuinfo` &
  `/proc/meminfo` reads and `version`, projecting them onto the canonical
  `host.*` keys so the reconciler/assertions/audit consume Talos nodes with no
  downstream change. Physical-NIC filtering is positive (link `kind` empty +
  real `busPath`), avoiding the SSH path's name-prefix heuristic. First non-SSH
  source — also unblocks reconciler **multi-source precedence**.
- [x] **`host.smart` probe** (`probes/host/smart.py`) — per-drive S.M.A.R.T. via
  `smartctl -a -j` over kernel-ssh (root; `sudo -n` when not root). Emits
  `host.smart.devices` (health, power-on hours, temperature, reallocated/pending
  sectors, CRC errors, NVMe wear), `host.smart.health_all_passed`,
  `host.smart.unhealthy_devices`; WWN in lsblk `0x…` form to cross-reference
  storage parts. Graceful-skips when smartctl is absent, so it's safe in the
  default host-probe set. Chosen over an OMV JSON-RPC adapter (vendor-specific,
  needs API creds) and an MCP path (can't run in the headless probe pipeline).
- [x] **Network-scan importer** (`engine/scan_import.py` + `helper discover
  import <csv> [--dry-run]`) — ingest an external host-scan CSV
  (`ip,hostname,os_class,os_detail,ssh_banner,ttl,mac,nic_vendor`) into Host
  rows; classify by scan signature (full OpenSSH → deep-probeable;
  embedded/dropbear/IoT/no-banner → agentless; Windows → agentless-for-now) and
  raise a `DISCOVERY_AGENTLESS_NEEDED` (INFO) finding per unprobeable host.
  Existing hosts match by primary_ip or short hostname and are enriched, not
  clobbered. Idempotent.
- [x] **Deep-probe records coverage** — `helper discover host`/`talos` set
  `Host.discovery_source = KERNEL_PROBE` on a successful run; the importer treats
  probed hosts (incl. Talos nodes with no SSH banner) as covered and
  auto-resolves stale agentless findings.
- [x] **`_resolve_host` matches by primary_ip too** — a probe run passing a short
  name no longer duplicates a row another source created under an FQDN at the
  same IP. Regression test `tests/test_cli_resolve_host.py`.
- [x] **`host.memory` DIMM lineage** — the memory probe now emits
  `host.memory.dimms` (slot/size/serial/vendor/part/type) from `dmidecode -t
  memory` (root; `sudo -n` when not root, graceful-skip otherwise), the exact
  shape the reconciler's DIMM lineage consumes — so `PhysicalPart`/`Placement`
  rows populate per DIMM (closes the AC2 DIMM gap).
- [x] **`host.pci` + `host.gpu` probes** — PCI enumeration via `lspci -vmmnn`
  (shared parser) and GPU detection from PCI class `03xx`. Graceful-skip with no
  PCI bus. The last two P1 warm-probe deliverables.

### Discovery sources & probes (landed, cont.)

- [x] **OpenMediaVault adapter** (`adapters/openmediavault.py` + `helper discover
  omv`) — read-only NAS management-plane source over OMV's JSON-RPC API
  (`/rpc.php`, lazy `session.login` with the cookie carried by the client;
  RPC method names centralized as constants; injectable client for tests).
  Reads mounted filesystems/pools, per-disk S.M.A.R.T. identity, shared folders,
  and service states — the storage facts a headless probe can't reach without
  vendor creds (this is why the `host.smart` probe was chosen *over* an OMV
  adapter for the SSH path; OMV now lands as its own management-plane source,
  same pattern as UniFi/Proxmox). Read-only at L1. Also enumerates NFS/SMB
  exports (`NFS`/`SMB` `getShareList`) so the exported shares join the
  shared-folder inventory; `discover omv` renders an exports table.
- [x] **Argo CD drift → findings** (`engine/argocd_drift.py` + `helper diff
  git-vs-cluster`) — persists `application_is_drifted` results as
  `DRIFT_CANDIDATE` findings via the deterministic fingerprint (`argocd-app` /
  `<name>` / `argocd-drift`) + reopen-on-recurrence machinery, mirroring the
  AssertionEngine lifecycle. `Degraded`/`Missing` → HIGH, `OutOfSync`+healthy →
  MEDIUM. Invariant #1 respected: only apps Argo CD *reports* as healthy resolve;
  a vanished app's finding is left open. `helper diff git-vs-cluster` prints the
  drift table and, with `--persist [--dry-run]`, records the findings so they
  surface in `helper findings` / `helper audit`.
- [x] **Stray-config detection** (`engine/stray_config.py`, wired into `helper
  discover unifi --persist`) — cross-references UniFi network/VLAN definitions
  against known clients by **subnet membership** (IP-based, deterministic — no
  reliance on UniFi's internal id linkage). An enabled network with a routable
  subnet and zero clients in it opens a `STRAY_CONFIG` (LOW) finding, with the
  fingerprint + reopen-on-recurrence lifecycle; a network that gains a client
  resolves it. Invariant #1 respected (a vanished network isn't auto-resolved);
  networks without a subnet are skipped. Structural L1 signal — the roadmap's
  "no traffic in N days" refinement still leans on the Phase-2 time-series.
- [x] **Cross-resolver service identity** (`dns_reconcile.service_key` +
  service-level split-brain in `view`) — the DNS reconcile now keys a `Service`
  by the leftmost DNS label (canonical short name), so internal `ha.lan` and
  external `ha.example.com` attach to one `Service` (`ha`) while each endpoint
  keeps its full FQDN. `helper view service` detects split-brain at the service
  level (internal IPs vs external IPs), not per exact hostname — so differently-
  suffixed internal/external names now surface as split-brain. Colliding short
  names merge (acceptable for a homelab); an explicit alias map can override
  later.

### Discovery sources & probes (next up)

- [ ] **OMV export → stray-export detection** — with NFS/SMB exports now
  inventoried, flag an exported share whose backing shared-folder/filesystem is
  gone (or an export no client mounts, once temporal data lands) as a
  `STRAY_CONFIG` on the storage side. Complements the UniFi network stray-config.
- [ ] **Explicit service alias map** — an operator-editable fixture that overrides
  the leftmost-label heuristic when two distinct services share a short name (or
  one service spans unrelated short names). Refines cross-resolver identity.

### Phase 4 — conversational layer (started)

- [x] **MCP server** (`mcp_server.py` + `helper mcp serve|tools`) — the query
  surface as Model Context Protocol tools over stdio (official `mcp` SDK):
  `list_hosts`, `get_host` (identity + guests + endpoints + open findings),
  `list_findings`/`get_finding` (stable fingerprints as identities),
  `list_services`/`get_service` (split-brain surfaced as data), `audit_summary`,
  and `run_discovery` for the six management-plane sources (reads live source,
  persists to harness DB — never writes to the lab). Lookup misses and adapter
  errors return `{"error": ...}` payloads so LLM callers can react instead of
  hitting protocol faults. Registers with Claude Code via
  `claude mcp add homelab -- uv run --directory <repo> helper mcp serve`.
- [x] **LLMRouter** (`llm/router.py` + `llm/backends.py`) — the single entry
  point for every LLM call: task class → minimum capability tier
  (Tiny/Small/Mid/Frontier) → privacy policy → backend. Backends: Ollama
  (default, always present at `localhost:11434`, availability-probed, model
  pulled-check), BYOK Anthropic + OpenAI (only built when a key is set), and
  OpenAI-compatible for local vLLM/llama.cpp/LM Studio (counts as local).
  Policies: `strict-local` (cloud is not a candidate at all), `prefer-local`
  (default; capable local before capable cloud), `open` (most capable first,
  local wins ties) via `HOMELAB_HELPER_LLM_PRIVACY`. Failover on
  unavailable/erroring backends; **never a silent downgrade** — no capable
  backend → `RouterRefusal` naming the task, needed tier, policy, what each
  backend offered, and the operator's options (P4-AC5). Local tiers are
  operator-declared (`*_TIER` env vars). `helper config` shows the roster.
- [x] **`helper chat`** (`cli/chat.py`) — one-shot and REPL chat grounded in
  `llm/context.py`'s bounded fact sheet (hosts, guests, services + split-brain,
  open findings with fingerprints) — the synthesized-view trust boundary: no
  raw probe output, no secrets. Multi-turn history in the REPL; every reply
  prints a `[backend: model (tier, local/cloud)]` footer; refusals exit 2 with
  the router's message (P4-AC1).
- [x] **Narrator agent** (`llm/narrator.py` + `helper findings narrate`) —
  findings → prose at `TaskClass.NARRATION` (Tiny+). Renders reconciled finding
  rows (fingerprint, severity, description, proposed actions, affected) into
  the prompt; narration cites fingerprints so prose is presentation, never the
  source of truth. `narrate` takes fingerprint prefixes or a status filter
  (P4-AC2's machinery; the Ceph demo needs the populated fleet).
- [x] **Conversational Discovery Agent** (`llm/discovery.py` + `helper
  onboard`) — interview-style host onboarding (P4-AC3). The first agent that
  leads to a harness-DB write, so the trust split is the design: the **LLM
  interviews and proposes; deterministic Python validates; the operator
  confirms; only then is anything written.** Protocol: one JSON object per
  model turn (`{"say", "proposal", "done"}`), tolerantly parsed (fences,
  prose) with corrective retries and a bail-out after 3 consecutive protocol
  failures. `validate_proposal` enforces what the model cannot waive: hostname
  syntax, IP syntax, arch enum, duplicate host/IP detection, and **rejection of
  anything that looks like key material** — the agent collects an SSH username
  and key *path* only (stored as `Host.credentials_ref`), never secrets.
  Confirmed hosts land as `discovery_source=manual` with role in capabilities;
  `--probe` then runs the shared `engine/host_probe.py` warm-discovery path,
  otherwise the exact `helper discover host` command is printed. Top-level verb
  (`helper onboard`, not a chat subcommand) because a click group callback with
  a positional argument is ambiguous with subcommand resolution.
- [ ] **Skill Inferer** — passive per-domain trust hints from chat (P4-AC6).
- [x] **MCP host-discovery tool** (`probe_host`) — SSH deep-probe exposed over
  MCP, so kernel-level facts are reachable conversationally and not just from
  the CLI (`run_discovery` covers management planes only). Orchestration moved
  out of the Typer command into `engine/host_probe.py` (`probe_host`,
  `resolve_host`, `select_host_probes`) so both surfaces run the identical
  sequence; the CLI passes an `on_probe` callback to stream progress. Credential
  story: **key path or `HOMELAB_HELPER_SSH_KEY` only** — passwords are not
  accepted through tool args at all, which is the "no raw secrets through MCP"
  constraint this item was waiting on.
- [x] **Config surface + `.env` loading** (`config.py` + rewritten `helper
  config` + `config_status` MCP tool) — `python-dotenv` was a declared
  dependency that nothing imported, so `.env` files were silently ignored.
  Now loaded at both entry points (CLI callback and MCP module import) with
  `override=False`, from a project `.env` (walk bounded at the `.git` root so a
  stray ancestor file is never pulled into a process that talks to live infra)
  then `~/.env`. `SOURCES` is the single declaration of each source's required/
  optional/secret variables, rendered by both surfaces; secrets report as
  set/unset and their values are never returned.
- [x] **Findings lifecycle over MCP** — `ack_finding`, `resolve_finding`,
  `suppress_finding`, sharing the CLI's fingerprint-prefix matching (ambiguity
  reported, never guessed). Harness-DB writes only.
- [ ] **Host retire + part merge** — the cleanup path that decommissioned and
  repurposed hardware needs, and the reason stale rows currently accumulate.
  `IntentState.DECOMMISSIONING` exists in the enum with **zero consumers**;
  needs the OperationalIntent write path, reconciler consumption, a CLI verb,
  and an MCP tool. Two cautions: it is really **Phase 2 intent work surfacing
  late**, not native Phase 4; and it touches load-bearing invariant #1 — a
  retire must close placements *explicitly* without weakening the rule that an
  absent observation never auto-resolves. Motivating case: three Pi control-plane
  nodes (`pi-cp1/2/3`) were reflashed and moved to the Wyola site as
  `wyhome`/`wynode2`/`wynode3`. NIC lineage tracked the move by MAC, but the
  disk placements stayed open on the retired hostnames because the USB
  enclosures forge a shared WWN and Talos vs. Ubuntu report different serial
  fields for the same drive — so no identity links the two eras. Part merge is
  therefore operator-driven, not inferable.

  **Second case — renaming a UniFi controller strands its resolver slice.**
  `ServiceEndpoint` is keyed by `(service, scope, resolver, hostname)` and a
  sync reaps only the `(scope, resolver)` slice it was called for — the scope
  discipline that lets two gateways coexist without deleting each other's rows.
  The cost is that changing a controller's name changes its resolver tag, and
  the previous slice is left behind with nothing to ever reap it. Observed live:
  a single-controller lab adopting the multi-controller form renamed `default` →
  `covington`, so 107 endpoints under resolver `unifi` were silently superseded
  by 107 identical rows under `unifi:covington`. Nothing errored, nothing
  reported it, and the duplicates are invisible until someone groups endpoints
  by resolver. Cleaned up by hand this time.

  Same shape as the hardware case above: **an identity change strands rows that
  no reconcile pass owns.** Whatever retire/merge verb lands should cover
  resolver slices too, not just hosts and parts. Cheap partial mitigation
  worth doing first — have the endpoint reconcile warn when it creates a slice
  whose rows duplicate an existing slice's `(hostname, ip)` set, which turns a
  silent orphan into a visible one without needing the full verb.

### Reconciler / assertions (landed)

- [x] **Forged-WWN collision guard** — `Reconciler._resolve_storage_identity` +
  `_guard_forged_wwn`: when a storage WWN is already owned by a part with a
  *different* serial, the WWN is forged (some USB/SATA enclosures report a
  constant WWN) — re-key the device by its unique serial (recovered from the
  by-id symlink) and raise a `STORAGE_PROVENANCE_DELTA` (MEDIUM) finding instead
  of merging distinct drives into one "moving" part. First-created part wins WWN
  ownership (deterministic via uuid7/`created_at`); re-runs stable. Test
  `test_forged_wwn_collision_keys_by_serial_and_flags`.
- [x] **SMART-health assertion pattern** — a `<host>.smart_all_healthy` (HIGH)
  check using `host.smart.health_all_passed eq true` turns a failed SMART status
  into a finding via the existing assertion engine.

### Known gaps (found during validation)

- [x] **NIC virtual-interface filter** — added Proxmox firewall prefixes
  (`fwbr`/`fwln`/`fwpr`) to the host.network reconciler heuristic so they no
  longer leak through as spurious NIC parts. (A positive PCI-backing test is
  still the eventual ideal.)
- [x] **DIMM no-identity** — now emits an `INVENTORY_GAP` for DIMMs the probe
  reports without a serial, same as storage, since the dmidecode probe populates
  `host.memory.dimms`.
- [x] **Per-assertion arch filter** — capability assertions take an optional
  `arch:` scope (`verifier_spec.applies_to_arch`); off-arch hosts SKIP rather
  than FAIL, so the library binds cleanly to mixed amd64/arm fleets.
- [ ] **`host.raid` / `host.shares` probes** — mdraid composition
  (`/proc/mdstat` + `mdadm --detail`) so the reconciler models an array as a
  volume over its member parts; NFS/SMB share enumeration.
- [ ] **`talos.host` CPU/DIMM depth** — SMBIOS `processors`/`memorymodules` can
  be sparse (cores/flags come from `/proc/cpuinfo`); DIMM lineage isn't
  populated without a per-module serial.


### P2 — strengthens, doesn't block

- [ ] **`host.pci` probe** — PCI device enumeration (currently absent from entry points)
- [ ] **`host.gpu` probe** — GPU detection/capabilities (currently absent)
- [ ] Verify `host.memory` emits per-DIMM identity (`dmidecode` slot topology) sufficient for `PhysicalPart`/`Placement` creation; extend if not (the roadmap names this `host.memory.dmidecode`)
- [ ] Docs site scaffold (mkdocs-material): getting-started, CLI reference, probe SDK guide, NetBox custom-field reference

### Hygiene

- [ ] `engine/__init__.py` docstring lists `Reconciler`/`AssertionEngine`/`Scheduler`/`ProposalManager` as if present — keep as forward-looking, or trim to what's implemented, as those components land
- [x] **CI workflow** (`.github/workflows/ci.yml`) — runs on push + PR against `main`, with concurrency cancel-in-progress on the same branch:
  - [x] `uv sync --all-extras --group dev` (uv cache keyed by `uv.lock` via `astral-sh/setup-uv@v6 enable-cache: true`)
  - [x] `uv run ruff check src tests`
  - [x] `uv run ruff format --check src tests`
  - [x] `uv run mypy src` — fixed the two pre-existing `_resolve_host` errors as part of this slice so the gate is genuine, not vacuous
  - [x] `uv run pytest -q`
  - [ ] `uv run pre-commit run --all-files` (deferred — explicit ruff+mypy+pytest steps overlap, add only if a contributor lands the hooks-locally workflow)
- [ ] Decide on Python version matrix for CI — at minimum the `.python-version` pin (3.12); consider also testing on 3.13 once it stabilises in upstream deps

---

## Phase 6 — L2 Execution & Trust Gradient

Depends on Phases 1–5 (the proposal stream, blast-radius/rollback metadata,
and planner output it executes). Specified in `architecture.md` ("Trust
gradient") and `harness-schema-slice1.md` ("Trust gradient tables"). The
**safety machinery is the bulk of the effort**, not the executor itself.

### Schema & policy core

- [ ] Enums `AutonomyLevel`, `TrustDomain`
- [ ] Tables + migration: `Domain`, `CellTrust`, `TrustBoundary`, `ElevationWindow`, `TrustHistory`
- [ ] **`decide(action, context) → BLOCK|PROPOSE|CONFIRM|AUTONOMOUS`** — pure, deterministic; **no LLM in the authorization path**. _P6-AC1: with every cell at `PROPOSE`, nothing executes; Phases 1–5 behaviour unchanged._

### Execution

- [ ] **Executor** — consume approved `ProposalLog` artifacts → adapter `write()` paths; transactional where supported; emit a receipt per action. _P6-AC2._
- [ ] **Adapter write-gate wiring** — route *every* adapter write through `decide()`; flip the L1 gate that currently always returns `False`
- [ ] **Snapshot/rollback orchestrator** — per-blast-radius capture (ZFS/LVM/VM snapshot, config backup) before execution; the verified-rollback gate that caps autonomy. _P6-AC4._

### Trust dynamics

- [ ] **Auto-escalation engine** — per-cell clean-streak tracking; promote reversible + low-blast cells after N clean approvals; instant demotion + probation on one bad outcome. _P6-AC3._
- [ ] **Override** — per-action, owner-only, interactive, high-friction, logged as a `TrustHistory` event; never agent-invokable, never self-invoked by autonomous execution
- [ ] **Elevation window** — scoped (cells/hosts), hard expiry, no auto-renew, best-effort snapshot under elevation, receipts tagged with window id; outside any window, autonomous-meets-floor degrades to `CONFIRM`. _P6-AC5._
- [ ] **Kill switch** — revoke all open windows instantly; halt in-flight autonomous actions at the next checkpoint. _P6-AC5._
- [ ] **Absolute floors** — `Domain.is_absolute` (`secrets` ships true) and `TrustBoundary.absolute`; no override or window crosses them; config-edit only. _P6-AC6._

### Surface & audit

- [ ] CLI: `helper trust grant|show`, `helper window open|revoke`, `helper exec`
- [ ] Receipts + audit spine: write-back to `ProposalLog` (outcome, restore handle); `TrustHistory` append-only for every grant / auto-promote / demote / override / window open+revoke
- [ ] Decide the `AuditLog`/receipt detail beyond `ProposalLog` + `TrustHistory`

---

## Not tracked here

Phases 2–5 are in `roadmap.md` but not yet broken into backlog items — they
get exploded into tasks as Phase 1 closes out. Post-roadmap (Phase 7+) items
are deliberately not tracked.
