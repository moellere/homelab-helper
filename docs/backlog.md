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
  - [x] Replay-style test pattern (in-process fixtures); migrates to YAML when dorktool fixture lands
  - [x] `transform` hook on `HostProjectionRule` (raw observation value → typed column domain) + `normalize_arch` mapper
  - [x] `host.cpu.*` slice: `host.cpu.architecture` → `Host.arch` (typed); model, vendor, sockets, cores, threads, threads_per_core, freq, cache, flags, interesting_flags → capabilities (`cpu_*` keys)
  - [x] `host.memory.*` slice (totals only): mem/swap/hugepage totals from `/proc/meminfo` → capabilities. DIMM-level keys land with the lineage slice below as first-class rows, not capabilities.
  - [x] DIMM `PhysicalPart` / `Placement` lineage: `host.memory.dimms` observation contract (list of populated-slot dicts) → serial-keyed `PhysicalPart` upsert with field enrichment; append-only `Placement` open/close; cross-host move closes prior placement; DIMMs without serial counted in `parts_skipped_no_identity`; no-DIMM-observation safety (doesn't close existing placements). Future dmidecode-driven probe will emit the observation contract.
  - [x] `host.storage.*` slice: scalar projections (`disk_count`, `disk_names`, `total_disk_bytes` → capabilities) + per-device `PhysicalPart` / `Placement` lineage from `host.storage.devices`. WWN-preferred / serial-fallback identity (USB enclosures forge serials); kind dispatched per-device (NVMe/SSD/HDD/OTHER) with reclassification on better evidence; partitions/LVM/RAID ignored. Kind-filtered close-loop prevents DIMM reconcile from disturbing storage placements (latent bug fix landed in this slice).
  - [x] `host.network.*` slice: scalar projections (`network_interface_count`, `network_interface_names` → capabilities) + NIC `PhysicalPart` / `Placement` lineage from `host.network.interfaces`. MAC-keyed (lowercased into the `serial` column); virtual interfaces filtered by name prefix and counted in `parts_skipped_filtered` (distinct from `parts_skipped_no_identity`); slot is the kernel interface name. Cross-kind regression tests confirm storage reconcile doesn't disturb NIC placements (and vice versa). Future probe version emitting PCI addresses can replace the prefix heuristic with a positive has-PCI-backing test.
  - [ ] Multi-source precedence rules: kernel beats management-plane, verified beats inferred (lands with first non-SSH source)
  - [x] Finding generation with deterministic fingerprint dedup: `INVENTORY_GAP` emitted per skipped part (DIMM no serial, storage no WWN+serial, NIC no MAC); fingerprint = `sha256(kind|host|root-cause)[:16]`; idempotent re-run hits the same row and bumps `last_seen`.
  - [x] Finding-level idempotency: re-runs update `last_seen`, don't duplicate (AC4). Plus auto-resolve when a previously-flagged condition clears, scoped per-category so an absent observation never silently auto-resolves findings of that category. Resolved findings reopen with the same fingerprint when the condition recurs.
  - _Unblocks AC2, AC3, AC4. Expect to rewrite parts of it twice (per roadmap risk note); build the replay test fixture alongside it._

### P1 — needed for the acceptance criteria

- [x] **NetBoxAdapter — first slice** (`adapters/netbox.py`): httpx-based async client; Device CRUD (list / get-by-name / update); custom-field CRUD (list / create); pagination; `Authorization: Token` auth; config via `HOMELAB_HELPER_NETBOX_URL` + `HOMELAB_HELPER_NETBOX_TOKEN` env vars; structured `NetBoxAPIError` carrying status + method + path. `sync_host(host)` PATCHes a Device's custom fields by hostname match (won't create Devices — schema doc invariant: NetBox owns canonical inventory facts, harness owns CF values). _Unblocks AC1/AC2's NetBox push (partial — Device CF surface only)._
  - [ ] Interfaces / IPs / VLANs / Prefixes / Clusters / VMs / Services / InventoryItems CRUD
  - [ ] Reconciler write path: InventoryItems mirror current Placements
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
  - [ ] `helper config`
- [ ] **dorktool integration fixture** — `fixtures/dorktool.yaml` loading the real lab; replayable, CI-friendly; the day-one audit runs as an integration test producing the 11–16 findings. _Unblocks the AC3/AC4 demonstration._

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
