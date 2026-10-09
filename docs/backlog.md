# homelab-helper — Backlog

The concrete, actionable task list. `roadmap.md` is the narrative (phases,
rationale, stop-here value); this is the punch list of what's actually left,
grounded in the current state of `src/`. All phases are tracked here:
remaining **Phase 1** items, the landed **Phase 3–5** slices (with their
queued follow-ups), and the **Phase 6** trust-gradient build.

Priorities are *within* a phase: **P0** = on the critical path, **P1** =
needed to meet the phase's acceptance criteria, **P2** = strengthens but
doesn't block. Acceptance-criterion references (AC1–AC5, P6-AC1–6) point at
`roadmap.md`.

---

## Current status snapshot

**Phases 1 (core), 3, 4, 5 and 6 are build-complete, and Phase 7 slice 1
(agent-triggered execution behind a phone-tap approval, guest migrate,
Kubernetes workload actions) has landed; Phase 2 (continuous agent /
time-series) landed as Phase 7 slice 3's daemon.** Full suite: 985+ tests green.
Packaging is release-ready (`uv tool install`, per-user dirs, tag-driven PyPI
release — see `releasing.md`).
Live-fleet validation (runbook: `docs/live-validation.md`): Phases 4–5 swept
10/03/2026 (two n/a on this fleet, onboarding still to run), Phase 6 covered by
the Phase 7 live sessions, Phase 7 signed off end to end (first unattended run 10/05/2026).

Phase-1 foundation, built and green:

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

- [x] **MikroTik RouterOS adapter** — `adapters/mikrotik.py` over the RouterOS 7
  REST API (basic auth, read-only user): identity/resource, interfaces, `/ip/address`
  subnets, DHCP leases, static DNS. Records are shaped like UniFi's, so the same
  reconcilers consume them: static DNS → internal endpoints under resolver
  `mikrotik[:<name>]`, addresses + leases → stray-config. `helper discover
  mikrotik [--persist] [--dry-run]`, `run_discovery("mikrotik")`, `mikrotik`
  source in `helper config`. `DiscoverySource.MIKROTIK` (no migration; SQLite
  precedent). Not yet: routes, firewall address-lists, CAPsMAN/wireless clients.

- [x] **OMV export → stray-export detection** — `engine/stray_export.py`: an NFS
  export or SMB share whose shared folder is gone, or whose folder's backing
  filesystem is not mounted (matched by device, mountpoint uuid, or label),
  becomes a `STRAY_CONFIG` finding keyed per export with the standard
  reopen/resolve lifecycle; absent exports never auto-resolve. `helper discover
  omv` prints the hits, `--persist` records them, `run_discovery("omv")`
  persists. "An export no client mounts" still waits on Phase-2 temporal data.
- [x] **Explicit service alias map** — `engine/service_aliases.py` +
  `HOMELAB_HELPER_SERVICE_ALIASES` (`fixtures/service-aliases.example.yaml`):
  exact hostnames and globs → service name, exceptions only. The DNS reconcile
  names new endpoints through it and re-points existing ones (`moved`), cleaning
  the orphaned service. `helper service aliases` shows the map as loaded.

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
- [x] **Skill Inferer** (`engine/skill_inferer.py` + `skill_profile` table +
  `helper skills`) — passive per-domain proficiency from chat (P4-AC6).
  Deliberately **deterministic**: a curated lexicon maps terms to domains
  (storage / container-orchestration / networking / virtualization /
  linux-admin), basic terms weight 1 and advanced terms 3; levels
  (novice→advanced) are thresholds over accumulated evidence. No LLM in the
  path — the profile feeds the Phase-6 trust gradient's per-domain *hints*, so
  keeping model judgment out of anything trust-adjacent is the point (an
  LLM-assessed refinement can layer later). Every `helper chat` message is
  observed passively; levels only ratchet up; `helper skills set` pins a
  domain (`source=manual`) that inference can never change. The profile is
  injected into the chat system prompt so answers match the operator's depth.
  Migration `e5a2c8f1b9d0`.
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
- [x] **Host retire + part merge** — landed as `engine/retire.py`: `helper host
  retire` (DECOMMISSIONING intent, explicit placement close, findings resolved,
  idempotent; planners skip retired hosts, chat tags them; MCP `retire_host`),
  `helper part show|merge` (operator-driven fold of a duplicate identity, audited
  under `attributes.merged_from`), and `helper service resolvers|retire-resolver`
  for the orphaned-slice case below, with the endpoint reconcile now reporting
  `superseded_resolvers` when a sync duplicates another slice. Original notes:
  the cleanup path that decommissioned and
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

### Phase 5 — planning & recommendations (started)

- [x] **WorkloadProfile schema + starter library** (`engine/workloads.py` +
  `fixtures/workload-library.yaml`) — the planner's unit of reasoning:
  baselines (cores/RAM/footprint), scaling shape, arch support, gpu
  none/optional/required (+purpose), dependencies, **data gravity** (the large
  dataset a workload wants to live near — distinct from its own footprint),
  deployment artifacts. 57 starter entries across media/automation/network/
  files/monitoring/nvr/dev/database/downloads/misc (P5-AC1's ≥50); baselines
  are starter estimates. `HOMELAB_HELPER_WORKLOAD_LIBRARY` layers an operator
  file on top, same-named entries winning — the community-contribution path.
- [x] **Placement recommender** (`engine/placement.py` + `helper plan
  add-workload`, P5-AC2) — deterministic and explainable: hard constraints
  reject with a stated reason (arch, RAM vs baseline + 1 GiB OS reserve, GPU
  required but absent, decommissioning intent — the first IntentState
  consumer); survivors scored on RAM headroom, CPU threads, optional-GPU
  bonus, data-gravity affinity to bulk storage, minus a running-guest load
  penalty — every score component carries a reason. Unknown facts are caveats,
  not rejections ("we don't know" ≠ "it doesn't fit"). Reconciler now projects
  `host.gpu.count/vendors` → `gpu_count`/`gpu_vendors` capabilities.
- [x] **Planner agent** (`llm/planner.py`) — narrates the finished report at
  `TaskClass.PLANNING` (Mid+); it cannot reorder, add, or drop candidates.
  `--narrate` refusals degrade to the already-printed deterministic table.
- [x] **NetworkPath abstraction** (`engine/network_path.py` + `helper plan
  path`, P5-AC6) — typed site/link graph, **operator-declared YAML** (no probe
  can see that the inter-site route rides a VPN; see
  `fixtures/network-topology.example.yaml`, pointed at by
  `HOMELAB_HELPER_NETWORK_TOPOLOGY`). Path characteristics inherit the only
  honest way: bandwidth = min, latency = sum, **reliability = worst link** —
  one VPN hop makes the whole path VPN-grade. Dijkstra by latency; no
  topology declared = single-site assumption, nothing degrades. Workload
  profiles gained `network_class` (any / lan-preferred / lan-required;
  ceph-osd + etcd added as lan-required, media servers marked lan-preferred)
  and placement now folds path verdicts in: **lan-required across a
  non-LAN-grade path to the data-gravity anchor is refused** with the
  worst-link explanation (AC6's Wyola↔Covington case, verified through the
  real CLI), lan-preferred takes a penalty + caveat. `helper plan path A B
  [--workload X]` shows hops, inheritance, and the verdict.
- [x] **Rebalance solver** (`engine/rebalance.py` + `helper plan rebalance
  [--narrate]`, P5-AC3) — fleet load model (per-host RAM capacity vs running
  VMs' committed memory, from the reconciled DB) plus **three candidate plans
  spanning three cost classes**: current-hardware (VM migrations only),
  one-dimm-move (smallest spare DIMM from the emptiest donor with ≥2 DIMMs →
  the constrained host, then fewer migrations), one-part-purchase (smallest
  standard DIMM that relieves the constrained host). Every plan carries typed
  steps, tradeoffs, and the resulting per-host load. Deterministic bounded
  greedy — each move must strictly improve the pairwise max ratio (the
  oscillation guard) — not OR-Tools; the plan/step shapes are solver-agnostic
  if constraint interactions ever outgrow greedy. Migrations respect cluster
  membership and **never cross a non-LAN-grade path** (NetworkPath). Unknown-
  RAM hosts are excluded from the math and listed, never treated as empty.
  Balanced fleets produce no plans. Narration via `narrate_rebalance` at
  planning tier.
- [x] **Bottleneck analyzer** (`engine/bottlenecks.py` + `helper bottlenecks
  [--persist] [--narrate]`, P5-AC4) — deterministic pattern library over the
  reconciled fleet; the *patterns* are curated, the *output* is constructed
  from detected facts (hosts, speeds, VMs — never canned text). Patterns:
  **cluster-link-asymmetry** → `CEPH_BOTTLENECK` (nodes of one cluster with
  differing fastest-NIC speeds; generates the four classic mitigations from
  the detected facts — CRUSH-reweight away from the slow node, upgrade its
  link to the observed fleet speed, relocate OSDs to a named full-speed node,
  accept as a tier — AC4's four, verified through the real CLI);
  **memory-pressure** → `CHOKEPOINT` (>90% committed; migrate the named
  largest VM to the named idle host / add RAM / accept);
  **storage-single-uplink** → `CHOKEPOINT` (bulk-storage host on a single
  ≤1 GbE NIC serving a multi-consumer fleet). `--persist` uses the standard
  fingerprint + reopen lifecycle; because every pattern runs every call, a
  previously open analyzer finding whose condition cleared resolves (category
  observed — not absence-auto-resolve), and findings from other generators
  sharing these kinds are never touched (evidence-tag scoped). Mitigations
  land as `proposed_actions` so chat/narrator can cite them (the P4-AC2 Ceph
  narration path).
- [x] **Reconfiguration reasoner** (`engine/reconfigure.py` + `helper plan
  surplus [--narrate]`, P5-AC5) — the mirror image: hosts with low commitment
  AND reconfigurable slack (stopped VMs, spare DIMMs beyond a kept minimum).
  Options generated from facts: spin the named stopped VMs back up; move the
  named spare DIMMs to the actually-loaded host (pointing at `helper plan
  rebalance`); accept — declare the reserve deliberate via OperationalIntent /
  stopped-by-design so it stops resurfacing. AC5's node2 case (24 GB RAM, two
  stopped VMs, named CPU) verified through the real CLI. Low commitment alone
  is not a hit — there must be something to reconfigure.

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

- [ ] **Planners have no "is a hypervisor" notion** (P5-AC5, 10/03/2026): `plan surplus`
  calls covomv's RAM surplus although it is a NAS running Docker; `plan rebalance`
  had the mirror defect (fixed in #51 via cluster membership). Surplus should skip
  hosts that are nodes of no cluster, or carry a role.
- [ ] **Runbook text assumes the day-one 1 GbE / 2.5 GbE asymmetry** (P4-AC2, P5-AC4):
  the fleet is symmetric now; the criteria are marked n/a with a note.

- [x] **P5-AC3 on the live fleet (10/03/2026)** produced no migrations-only plan, then
  — after the first fix — plans that migrated Proxmox guests onto a NAS, arm64 Pis and
  Talos workers, with one VM ping-ponging. Three defects in `engine/rebalance.py`:
  the greedy mover tried only the single emptiest host as destination; an empty
  host counted as "joinable" to any cluster; nothing stopped a VM moving twice.
  Fixed: destinations are searched emptiest-first past illegal ones, a target
  must be a node of the guest's cluster (from the cluster's persisted `nodes`
  list, else the guests it already runs), and a VM moves at most once per plan.
  `virt_reconcile` now records `Cluster.attributes["nodes"]`.
- [x] **P4-AC1 on the live fleet (10/03/2026)**: Ollama was not running, the router
  answered from Anthropic under `prefer-local`, and the footer said "cloud" but
  not *why* local was passed over — `RouterResult` dropped the exclusion
  reasons on success. Now `RouterResult.skipped` carries them and `helper chat`
  prints a `skipped:` line under a cloud footer.
- [x] **Cloudflare account-owned tokens were rejected (10/04/2026)**: the adapter's
  health check called `/user/tokens/verify`, which answers only for user-owned
  tokens and returns `1000 Invalid API Token` for a valid `cfat_…` account token.
  The check is now a zone read — the permission the adapter actually needs.

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

- [x] Enums `AutonomyLevel`, `TrustDomain`
- [x] Tables + migration `f7b3d9a2c4e1`: `Domain`, `CellTrust`, `TrustBoundary`,
  `ElevationWindow`, `TrustHistory` — per the forward spec; domains seeded
  idempotently by `helper db init` (every default PROPOSE; SECRETS absolute
  with a PROPOSE ceiling)
- [x] **`decide(action, context) → BLOCK|PROPOSE|CONFIRM|AUTONOMOUS`**
  (`engine/trust.py`) — pure function over frozen dataclasses
  (`load_trust_context` does the DB reads, `decide` judges); walks the three
  floor tiers (cell level → domain max + boundary ceilings + verified-rollback
  → absolute floors), windows lift only the soft-hard tier, every clamp
  carries a human-readable reason. **No LLM in the authorization path** is a
  regression test (subprocess import check), not a comment. Probation caps at
  CONFIRM. _P6-AC1 groundwork: with every cell at `PROPOSE`, decide() returns
  PROPOSE and no executor exists; Phases 1–5 behaviour unchanged._
- [x] CLI first slice: `helper trust show|grant` — grants are
  operator-attributed (`HOMELAB_HELPER_OPERATOR`, OS-user fallback), refused
  above the domain ceiling, recorded in `TrustHistory`

### Execution

- [x] **Executor** (`engine/executor.py`, migration `a9c4e7f2d8b3`) — consumes
  pending `ProposalLog` action manifests (`{"kind": "action", ...}`; manifest
  is untrusted input: validated, and the declared domain must match the guest
  kind so a manifest can't shop for a softer cell); gates through
  `load_trust_context` + `decide()`; BLOCK/PROPOSE never dispatch and never
  write a receipt; CONFIRM needs the operator callback; rollback state (prior
  power state) captured before dispatch; exactly one `ExecutionReceipt` per
  dispatch (success or failure — failure stays PENDING/retryable, success
  closes the proposal USER_ACCEPTED). _P6-AC2._
- [x] **First write path** — Proxmox guest power (`vm_power`:
  start|stop|shutdown|restart→reboot, qemu+lxc; `vm_current_status` for
  rollback capture); the only write surface in any adapter, executor-only by
  block-comment contract. AC2's canonical cell is
  `containers/restart/single-host`. MockTransport-only tests; live execution
  waits on the user's fleet-validation gate.
  - [ ] Remaining write-gate wiring as more adapter writes land (NetBox
    `cf_*` sync already has its own diff/confirm path; anything new routes
    through the executor)
- [x] **Snapshot/rollback orchestrator** (`engine/rollback.py`, migration
  `c1d8f4a6e2b7`) — **reversibility is verified, not claimed.** Until PR D
  `rollback_verified` came off the manifest, so the one input to `decide()`
  that an untrusted (possibly LLM-drafted) artifact could set in its own
  favour turned the AUTONOMOUS→CONFIRM floor into an honour system. Now the
  orchestrator probes the target: prior-power-state (is a restorable status
  readable?) or snapshot (does this guest's storage support one?), and the
  manifest's claim is recorded beside the finding so a false one stays
  visible. Three phases around the gate — `verify_rollback` (read-only),
  `capture_rollback` (post-authorization, may snapshot), `restore`. The gate
  runs twice so a refused action never touches the target even to probe it:
  pessimistically first (assuming no rollback, which can only under-state the
  outcome), then again with the finding. `helper exec rollback <receipt>`
  undoes an execution from its captured state, writes its own receipt, and
  links the original (`rolled_back_at` + `rollback_receipt_id`; receipts are
  never edited). Rollback is deliberately **not** decide()-gated — a safety
  valve the policy could lock shut is not a safety valve. _P6-AC4._
  - [ ] ZFS/LVM snapshot + config-backup strategies as those write paths land
    (the strategy seam is in place; today's are prior-power-state + Proxmox
    guest snapshot)

### Trust dynamics

- [x] **Auto-escalation engine** (`engine/escalation.py`) — per-cell
  clean-streak tracking; **N = 5** clean approvals buy **one rung**
  (PROPOSE → CONFIRM → AUTONOMOUS) for eligible cells only (reversible action
  kind *and* `blast ∈ {metadata-only, single-host}`); one bad outcome drops
  the cell straight to PROPOSE with probation, and only an explicit
  `trust grant` clears it. A blocked cell banks nothing — on probation the
  streak stays at zero, and any other block (domain ceiling, top of ladder,
  ineligible) holds it at N, so lifting a block never backdates credit.
  Promotions/demotions are `TrustHistory` events (`auto-promote` / `demote`
  with cause); `granted_by` is NULL on an auto-promotion so grants stay
  distinguishable. Fed by the executor (success/failure) and by
  `helper exec accept|reject` (hand-applied proposals — the PROPOSE rung's
  evidence). Rejection resets the streak but never demotes. _P6-AC3._
- [x] **Override** (`OverrideGrant` + `helper exec run --override`) —
  per-action, owner-only, interactive, high-friction (you retype the cell key;
  a reason is required), logged as a distinct `TrustHistory` event **only when
  it changed the decided level**, so the spine records authority changes
  rather than gestures. Crosses the soft-hard floors and nothing else: it
  never lifts a BLOCK/PROPOSE cell, never crosses an absolute domain or an
  absolute host boundary, never forges a window id, and covers exactly one
  action. Never agent-invokable — the grant is constructed only by the
  interactive CLI, never loaded from or derived from DB state.
- [x] **Elevation window** (`engine/trust.py` + `helper window`) — scoped to
  domains/hosts/cells and **never blanket** (a scopeless window is refused),
  hard expiry capped at `MAX_WINDOW_MINUTES` (8h) with no auto-renew,
  best-effort snapshot captured under elevation even though rollback isn't
  required, receipts tagged with the window id; outside any window,
  autonomous-meets-floor still degrades to `CONFIRM`. Open/revoke are
  `TrustHistory` events. _P6-AC5._
- [x] **Kill switch** (`helper window kill`) — revokes every open window in one
  gesture. In-flight work halts at the executor's **pre-dispatch checkpoint**:
  a decision that leaned on a window re-tests it immediately before dispatch,
  so a run authorized under a window that has since been killed refuses
  instead of executing. _P6-AC5._
- [x] **Absolute floors** — `Domain.is_absolute` (`secrets` ships true) and
  `TrustBoundary.absolute`; `open_window` refuses to name an absolute domain
  at all, and an absolute host boundary clamps even with a window open.
  `helper trust boundary <host> <ceiling> [--absolute]` sets the per-host
  ceiling. Config-edit only to change the absolute ones. _P6-AC6._

### Surface & audit

- [x] CLI: the authorization surface is complete — `trust show|grant` (PR A),
  `exec list|run|receipts` (PR B), `exec accept|reject` + `trust history`
  (PR C), `exec rollback` (PR D), `window open|list|revoke|kill` +
  `trust boundary` (PR E)
- [x] Receipts + audit spine: `ExecutionReceipt` (decision level + full
  reason trace + window id + rollback state + outcome/error/duration) with
  write-back to `ProposalLog`; `TrustHistory` append-only for grants (the
  auto-promote / demote / override / window events land with their features)
- [x] MCP trust surface (`trust_status`, `list_receipts`, `pending_actions`)
  — **read-only by construction**: a model can see the gradient, the receipts,
  and what policy would say about each pending action (computed
  pessimistically, since verifying reversibility means probing the target and
  a query tool has no business doing that). There is no MCP tool to grant,
  elevate, override, roll back, or execute; two mechanical tests enforce the
  absence, one on the tool roster and one on the module's imports and call
  sites.
- [ ] Decide the `AuditLog`/receipt detail beyond `ProposalLog` + `TrustHistory`

---

## Agent access (scoped, not started)

Six items from the pre-alpha readiness review — planner MCP tools, a Home
Assistant adapter, the stdio-only transport decision, cell-keyed adapter
write surfaces, `propose_action`, and a secrets store — are scoped with
designs, files, and sequencing in `agent-access-scope.md`. The two blockers
from the same review are done:

- [x] Packaged Alembic config (`db/migrate.py`; migrations ship in the wheel, `helper db init` works from an install)
- [x] Scoped MCP `probe_host` (known hosts at their recorded address; `HOMELAB_HELPER_MCP_PROBE_ALLOW` globs for anything else)
- [x] Item 1 — planners as MCP tools (`list_workloads`, `recommend_placement`, `plan_rebalance`, `analyze_bottlenecks`, `analyze_surplus`, `network_path`) + `probe_talos` (scoped like `probe_host`; `discover talos` shares `engine/talos_probe.py`)
- [x] Item 2 — Home Assistant adapter v1 (`adapters/homeassistant.py`, `engine/hass_import.py`, `discover hass --persist`, `run_discovery("hass")`); v2 device registry + `device_tracker` identity still open
- [x] Item 3 — stdio-only MCP transport documented (README + architecture trust table)
- [x] Item 4 — `engine/manifest.py` (pydantic authoring schema, agreement test with `parse_manifest`) + `tests/test_write_isolation.py` (Proxmox writes reachable only via executor/rollback). Dispatch registry and the SSH restart cell stay open until a second write adapter / a rollback strategy exists
- [x] Item 5 — MCP `propose_action` / `list_proposals` / `get_proposal` (draft-only; provenance clamp dropped — the gate is cell trust, per the executor's tests)
- [x] Item 6 — secret references (`secrets.py`: `env:` / `file:` incl. age + sops / `keyring:`), `helper config` reports the scheme, MCP error strings redacted
- [ ] Item 6 follow-up — move `Host.credentials_ref` (`ssh:<user>:<path>`) onto references; `op://` / `vault://` schemes; redact CLI-printed adapter errors

---

## Phase 7 — Agentic Operations

Specified in `roadmap.md` ("Phase 7") and `architecture.md` ("Agent-triggered
execution"). The invariant does not move: an LLM never authorizes. What changes
is who may *trigger*, and how much of the lab has an executor-gated write path.

### Slice 1 — trigger + approval channel + two surfaces (landed)

- [x] `engine/approval.py` — `ApprovalChannel` protocol, `ApprovalResult`, and the
  Home Assistant channel (actionable notification with Approve/Deny; the tap
  read back over HA's websocket event bus; timeout = no). Config:
  `HOMELAB_HELPER_APPROVAL_NOTIFY_SERVICE`, `HOMELAB_HELPER_APPROVAL_TIMEOUT_S`.
- [x] Executor: a channel's answer is written to `TrustHistory` as an `approval`
  event (channel, responder, approved); `confirm_cb` may return a bool (CLI) or
  an `ApprovalResult` (channel).
- [x] MCP `execute_proposal` — runs the executor with `override=None`; AUTONOMOUS
  runs, CONFIRM waits on the channel, PROPOSE/BLOCK refused with the reason and
  the `helper exec run` command. Mechanical tests: the tool can never carry an
  override; every cell at PROPOSE executes nothing through it. _P7-AC1, AC5._
- [x] Proxmox `migrate` (live/offline, named target node) with the `prior-node`
  rollback strategy (verified: both nodes online; restore: migrate back).
- [x] Kubernetes `workload-restart` / `workload-scale` on deployment, statefulset,
  daemonset; new `single-service` blast radius; K8s adapter writes
  `rollout_restart`, `scale_workload`, `rollout_undo` (executor-only);
  strategies `rollout-undo` (verified by rollout history; undo to the
  pre-restart revision) and `prior-replicas`.
- [x] Manifest: two target shapes (`ActionTarget`, `WorkloadTarget`), the
  authoring helper `build_workload_artifact`, MCP `propose_workload_action`,
  `propose_action(target_node=, online=)` for migrate.
- [x] `REVERSIBLE_ACTION_KINDS` grew by the three new kinds; `LOW_BLAST_RADII`
  by `single-service`.
- [x] **Live validation** (`live-validation.md`, Part 3) — 10/03/2026 on the
  Covington lab: migrate + rollback, workload restart + undo, Approve and Deny,
  all through `execute_proposal` with a phone tap. _P7-AC2, AC3._

### Slice 2 — surfaces (landed)

- [x] Guest `cpu-type` (QEMU; `set_vm_config`, applied at next stop/start;
  rollback `prior-config` restores the previous type or removes the key)
- [x] Argo CD `argocd-sync` (optional pinned revision, prune) — adapter gains
  `get_application` (deployed revision + sync history + `auto_sync`),
  `sync_application`, `rollback_application`; rollback `argocd-history` returns
  to the current history entry **unless the app has automated sync**, where
  Argo CD refuses the rollback API and the verifier reports unverifiable (found
  live 10/03/2026)
- [x] UniFi `dns-record` (upsert one name + type on a named controller) —
  adapter gains `find_dns_record`, `create/update/delete_dns_record`, keeps
  `_id`; rollback `prior-dns-record` restores the prior value or deletes the
  created record
- [x] `ExecutionReceipt.approval` (migration `b4e7c2a9d1f3`): channel, responder,
  approved — the same facts as the `TrustHistory` event, denormalized for
  `list_receipts`
- [x] `helper approvals show` — channel config, what each pending proposal
  would get if triggered now, recent answers
- [x] MCP `propose_argocd_sync`, `propose_dns_record`; `propose_action(cpu_type=)`;
  `execute_proposal` resolves the Argo CD / UniFi adapter a manifest needs
- [ ] `helper exec listen` — deferred: it needs a marker for *which* pending
  proposals an operator-side daemon should ask about (a denied proposal must
  not be re-asked every loop). Design it with the playbooks in slice 3.
- [ ] LXC lifecycle beyond power; NetBox / OMV writes — each behind the
  executor with a verified inverse, when a use case asks for them

### Slice 3 — proactive (landed)

- [x] `WORKLOAD_UNHEALTHY` findings (`engine/k8s_workloads.py`): the K8s adapter
  lists every Deployment / StatefulSet / DaemonSet; a settled workload with
  fewer ready than desired opens a finding (HIGH at zero ready); healthy again
  resolves it; absent is untouched. Wired into `discover k8s` and `run_discovery("k8s")`.
- [x] Playbooks (`engine/playbooks.py`): deterministic registry —
  `argocd-resync` (DRIFT_CANDIDATE → argocd-sync), `workload-restart`
  (WORKLOAD_UNHEALTHY → workload-restart). One live proposal per finding, 6 h
  cooldown after a decision, `finding.proposed_actions` records the draft.
  MCP `draft_remediations`. _P7-AC6 first half._
- [x] Listener (`engine/listener.py`): asks about pending `playbook:*` / `agent:*`
  proposals that policy would allow, through the executor with the approval
  channel; never re-asks an answered one; skips PROPOSE/BLOCK cells and
  hand-authored proposals. This is the `helper exec listen` the slice-2 notes
  deferred, with the marker question answered by "an approval event exists".
- [x] `helper daemon run` (`cli/daemon.py`): discovery → playbooks → listener on
  APScheduler cadences; `--once` for cron; `--no-ask` / `--no-playbooks` /
  `--sources ''` to disable jobs. _P7-AC6 second half._
- [x] Live validation of the loop (runbook Part 3 step 7, 10/03/2026): app-wirestudio
  resync asked → approved → executed via the listener. Two refinements fell out of it:
  `argocd-resync` only for OutOfSync apps (Synced/Degraded = a failed job, a sync cannot
  help); a 15-min `min_age` debounce + withdrawal of pending drafts whose finding resolved
  (app-of-apps blipped OutOfSync and Argo's own automated sync fixed it inside 3 min —
  the phone should never have been asked).
- [ ] More playbooks as findings grow identities: guest expected-on but stopped
  (needs an expected-state model for VMs), stray DNS → `dns-record`.
- [ ] Probe-level schedules and assertion cadences inside the daemon (the rest
  of the Phase-2 spec).

### Slice 4 — tell me afterwards (landed)

- [x] `engine/notify.py`: `should_notify` (AUTONOMOUS, any failure, auto-promote /
  demote), `render`, `HomeAssistantNotifier` on the approval channel's config,
  `notifier_from_env`, best-effort `notify_after_run`. Executor calls it after
  the receipt + escalation flush; `ExecutionResult.notification` says what
  happened; wired from `helper exec run`, MCP `execute_proposal`, the listener
  and the daemon. _P7-AC4 "with a receipt and a notification"._
- [x] Live validation (10/05/2026, runbook step 8): `workload-restart` granted
  AUTONOMOUS, an agent-drafted homepage restart ran unattended from the daemon
  and the phone got the ✓ notice after the receipt. The demotion notice was not
  forced live (needs a failing write behind a succeeding read); covered by tests.
- [ ] Daily digest (one notification summarising the day's receipts) — only if
  the per-run notices turn out noisy.

### Test hygiene (found during slice 1)

- [x] `tests/test_mcp_server.py` flakes (one random failure or error per run):
  the unclosed event loop was pytest-asyncio remembering an "old" loop that
  Python 3.12's default policy had just conjured because `asyncio.run` (any
  CliRunner test) leaves the main thread loop-less; `conftest.py` now installs
  a policy that refuses to create loops implicitly. The UniFi/OMV tests reading
  the operator's exported `HOMELAB_HELPER_*` credentials is fixed separately
  by scrubbing them in `conftest.py`.

---

## Phase 8 — Operate & Optimize

See `roadmap.md` Phase 8. Slices land in this order.

- [x] 8.1 Version currency — `engine/versions.py`, `helper discover versions`, `run_discovery("versions")`: `version-drift` findings for Proxmox package lag (node's cached apt list, Debian/Proxmox split, pve-manager called out) and mixed cluster versions, OS end of life from `data/os-eol.yaml`, kubelet/Talos skew, Home Assistant `update.*` pending (platform MEDIUM, devices LOW). Categories resolve only when observed (invariant 1). First live run 10/05/2026: bmax1–3 on 9.2.11 with 120–123 pending, bmax0 on 9.2.20, 7 HA updates.
  - [ ] Follow-ups: Argo CD image tags vs upstream releases; Debian security-update count (needs the node's apt sources, not exposed by the API); UniFi/OMV firmware; probe the X1/X4 Pis so their Bullseye/Buster EOL shows up (they have no `os_*` facts yet).
- [x] 8.2 Backup posture — `engine/backups.py`, `helper discover backups`, `run_discovery("backups")`: `backup-gap` findings for guests no enabled job selects, selected guests whose newest backup is older than twice the job interval (or missing), newest backup failing verification, backups retained for vmids that no longer exist, backup storage ≥ 80/90%. Shared reconcile extracted to `engine/category_findings.py` (8.1 uses it too). First live run 10/05/2026: all 23 live guests covered, fresh, verified; 1.3 TiB logical retained for deleted guests 102/107/110 (prune keeps a group's newest backups forever).
  - [ ] Follow-ups: offsite freshness (offsite-push publishes status to the Wyola HA's MQTT — read it once HA instances are multi-site in the adapter); restore-drill results; state outside Proxmox (HA config, covomv data) as operator-declared backup targets.
- [x] 8.3 Usage history — `usage_sample` table (migration `a7d3e9c2b5f1`), `engine/usage.py`, `helper discover usage`, `helper usage [subject] [--days]`, MCP `usage_summary`. Hourly rollups from the RRD `month` timeframe (30-min points), daily from `year` (6-h points): mean of AVERAGE, peak of MAX. First run backfills ~30 days hourly and up to a year daily; reruns upsert; `prune_usage` keeps 45 days hourly / 730 days daily (`HOMELAB_HELPER_USAGE_HOURLY_DAYS` / `_DAILY_DAYS`). First live run 10/05/2026: 17,504 rollups for 26 subjects in 7 s.
  - [ ] Follow-ups: K8s pod usage (metrics-server), OMV/covomv disks, non-Proxmox hosts via probes.
- [x] 8.4 Rightsizing & real-usage placement — `engine/rightsizing.py`, `helper plan rightsize [--days] [--persist]`, MCP `rightsizing`; `plan rebalance --basis usage` / `plan_rebalance(basis="usage")`. `rightsizing` findings (cpu-grow, cpu-shrink, mem-shrink, mem-grow for containers only, idle), each naming allocation, p95, peak, window and proposed value; < 7 days of hourly history → no verdict and no resolution (`observed_targets` added to the shared reconcile). VM memory is never grown: Proxmox's figure includes guest page cache (measured: ubuntu-dev reported 14.6 GiB, 4 GiB used in-guest). First live run 10/06/2026: Home Assistant CPU-bound 2 → 3 cores; ESPHome builders 4 GiB → 1–2 GiB; proxmox-dc 2 → 1 core, 4 → 1.5 GiB; esphome-lxc idle.
  - [x] `resize` action kind (10/06/2026): cores and/or `memory_mib` on a guest, through the executor via `set_vm_config`; refused at dispatch beyond the node's CPUs/physical memory (no write); a QEMU balloon floor above the new memory is lowered with it; QEMU without hotplug reports *pending until next stop/start*, containers apply live. Rollback: `prior-config` generalised to the keys an action changes (cpu; cores/memory/balloon). In `REVERSIBLE_ACTION_KINDS`. `rightsize` playbook drafts one resize per cpu-*/mem-* rightsizing finding (never for `idle`); rightsizing evidence now carries node/vmid/kind. MCP `propose_action(cores=, memory_mib=)`.
  - [ ] Follow-ups: in-guest memory via the QEMU guest agent so VM memory can grow too; K8s requests vs usage; an optional restart-after-resize for VMs (today the operator restarts, or a `restart` proposal).
- [x] 8.5 Storage efficiency — `engine/storage.py`, `helper discover storage`,
  `run_discovery("storage")`, `FindingKind.STORAGE_EFFICIENCY` (no migration —
  SQLite SAEnum has no CHECK). Five categories through
  `engine/category_findings.py`:
  - `storage-headroom` — least-squares slope on a pool's daily usage history →
    "fills in about N days", HIGH inside 30 days, MEDIUM inside 90. Needs
    history, so **8.3 now records storage pools too** (`subject_type="storage"`,
    `STORAGE_FIELDS`, `ProxmoxAdapter.storage_rrd`, one reader per pool since a
    shared pool is reported by every node — no migration, `subject_type` is a
    free string). A flat, shrinking or sub-10 MiB/day pool gets no projection,
    and fewer than 7 samples is not a trend. Deliberately linear: a straight
    line is explainable in a finding ("68 GiB/day"). 8.2's `backup-capacity`
    still owns the point-in-time ratio on backup storages — this answers the
    different question of *when*, for every pool, so one pool can raise both.
  - `storage-snapshot-stale` — snapshots older than 30 days, which pin blocks
    the guest has since overwritten. The harness's own `helper-` rollback
    captures are named separately and raised to MEDIUM: that is its own litter.
  - `storage-detached-disk` — `unusedN` entries: on the pool, attached to
    nothing, invisible in the guest's own usage.
  - `storage-template-clutter` — ISOs and container templates no guest config
    references (disks, `unusedN` slots and `ostemplate` all count as a
    reference), grouped per storage with the bytes.
  - `storage-released-pv` — Kubernetes PVs in `Released`: claim gone, volume
    retained, nothing can bind it again.
  - [ ] Follow-ups: backup retention cost (8.2's `backup-orphans` already names
    the groups kept for deleted guests; a per-tier cost breakdown is reporting
    rather than a finding); OMV/covomv filesystem headroom via the OMV adapter;
    ZFS snapshot space accounting (`written`/`refer`) so a stale snapshot's real
    cost is a number rather than an explanation.
- [x] 8.6 Weekly digest — `engine/digest.py`, `db/models/digest.py` (migration
  `b4e1f7a9c3d2`), `helper digest show|send|history`, daemon `--digest` job.
  Contents are chosen from rows only (findings, proposals, receipts,
  `TrustHistory`) and a subprocess test asserts the module never imports
  `homelab_helper.llm` — P8-AC6's "an LLM may narrate it but does not choose
  its contents". Three sections: what was done (receipts, rollbacks marked),
  authority changes, what changed (opened/resolved, worst-first), what is
  recommended (open findings + pending proposals).
  - Windows **tile**: each digest records `window_start`/`window_end`, so the
    next one starts where the last stopped — no change reported twice, none
    lost in a gap. `--days` overrides; `show` deliberately does not record, or
    reading the page would eat a week of changes.
  - A **quiet window is recorded but not sent** (`--quiet-ok` to override),
    and standing open findings do not make a window busy — otherwise one known
    LOW finding buzzes the phone every week, which is the stream this slice
    replaces.
  - The daemon job is gated on `MIN_DIGEST_DAYS` rather than its interval, so a
    restart or a 15-minute cron tick cannot turn the weekly summary into a
    stream.
  - [ ] Follow-ups: LLM narration of the page (the contents are already fixed,
    so this is presentation only); a digest-scoped `--since <receipt>` for
    re-reading an old window; HTML output if the Markdown page wants a browser.
- [x] 8.7 Service suggestions — `engine/suggestions.py`,
  `helper discover suggestions`, `run_discovery("suggestions")`,
  `FindingKind.SERVICE_SUGGESTION`. Two categories, both INFO:
  - `capability-idle-gpu` — a host reporting display/compute adapters
    (`gpu_count`/`gpu_vendors`, from the `host.gpu` probe) while nothing the
    library knows as GPU-capable appears to run. The suggestion names each
    candidate's `gpu_purpose`, so it says what the silicon would be *for*.
  - `building-block-missing` — the three the roadmap names (metrics, alerting,
    and the local model the LLM router prefers), each a set of library entries;
    missing when none is present. Grafana counts for metrics and alerting both.
  - **Presence is decided by name**, because the harness tracks guests,
    services and endpoints rather than installed software: a guest called
    `media` running Plex is invisible here. Every finding says it matched on
    names only, and both categories are INFO rather than problems. Containment
    is word-level, so `plexiglass-inventory` is not Plex.
  - The pass reads stored facts only (no adapter calls), so it cannot fail for
    a source being down and both categories are always observed.
  - [ ] Follow-ups: match on the `host.services` probe's unit names and on
    container image names (K8s + Proxmox LXC) so presence stops depending on
    what a guest was called; `depends_on` chains (suggest `mosquitto` when
    `zigbee2mqtt` is present without it); accelerator kinds beyond PCI
    display-class (Coral TPU on USB).
- [ ] Update orchestration (first Phase 8 write path, at PROPOSE) — after 8.1 has run a while.

---

## Not tracked here

Phase 2 (continuous agent / time-series) is specified in `roadmap.md` but
deliberately deferred — Phase 7 slice 2 picks it up as the scheduler. Post-roadmap
(Phase 8+) items are deliberately not tracked.
