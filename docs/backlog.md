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
  - [ ] `NETBOX_DIVERGENCE` finding on hand-edit conflict (skip write, never overwrite)
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
