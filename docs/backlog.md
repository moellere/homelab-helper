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
  - [x] DIMM `PhysicalPart` / `Placement` lineage: `host.memory.dimms` observation contract (list of populated-slot dicts) → serial-keyed `PhysicalPart` upsert with field enrichment; append-only `Placement` open/close; cross-host move closes prior placement; DIMMs without serial counted in `parts_skipped_no_serial`; no-DIMM-observation safety (doesn't close existing placements). Future dmidecode-driven probe will emit the observation contract.
  - [ ] `host.storage.*` slice + storage device `PhysicalPart` / `Placement` lineage (apply DIMM lineage pattern to SSDs/HDDs/NVMe; key on serial or WWID; slot like `/dev/nvme0n1`)
  - [ ] `host.network.*` slice + NIC `PhysicalPart` / `Placement` lineage (apply DIMM lineage pattern to NICs; key on MAC; slot like `pci@0000:01:00.0`)
  - [ ] Multi-source precedence rules: kernel beats management-plane, verified beats inferred (lands with first non-SSH source)
  - [ ] Finding generation with deterministic fingerprint dedup (wire in `FingerprintGenerator`)
  - [ ] Finding-level idempotency: re-runs update `last_seen`, don't duplicate (AC4)
  - _Unblocks AC2, AC3, AC4. Expect to rewrite parts of it twice (per roadmap risk note); build the replay test fixture alongside it._

### P1 — needed for the acceptance criteria

- [ ] **NetBoxAdapter** (`adapters/netbox.py`) — read+write Devices, Interfaces, IPs, VLANs, Prefixes, Clusters, VMs, Services, InventoryItems, custom fields
  - [ ] Reconciler write path: custom fields + InventoryItems mirror current Placements
  - [ ] `NETBOX_DIVERGENCE` finding on hand-edit conflict (skip write, never overwrite)
  - _Unblocks AC1, AC2._
- [ ] **NetBox bootstrap** (`helper netbox bootstrap`) — create the harness `cf_*` custom fields via the NetBox API on first run. _Open question #4 in the schema doc resolved in favour of bootstrap over manual setup._
- [ ] **AssertionEngine** (`engine/assertions.py`, one-shot mode) — run verifiers, write `AssertionRun` rows, emit findings on failure. _Unblocks AC3._
- [ ] **Network probes** — `network.subnet-scan` and `network.fingerprint` (asyncio TCP, no nmap dependency; fingerprint Proxmox/Cockpit/K8s API/web servers). _Unblocks AC1._
- [ ] **CLI verbs**:
  - [ ] `helper discover network <cidr>` (wire the network probes) — AC1
  - [ ] `helper audit` — AC3
  - [ ] `helper findings [show|ack|resolve|suppress]`
  - [ ] `helper host show <name>`
  - [ ] `helper config`
- [ ] **dorktool integration fixture** — `fixtures/dorktool.yaml` loading the real lab; replayable, CI-friendly; the day-one audit runs as an integration test producing the 11–16 findings. _Unblocks the AC3/AC4 demonstration._

### P2 — strengthens, doesn't block

- [ ] **`host.pci` probe** — PCI device enumeration (currently absent from entry points)
- [ ] **`host.gpu` probe** — GPU detection/capabilities (currently absent)
- [ ] Verify `host.memory` emits per-DIMM identity (`dmidecode` slot topology) sufficient for `PhysicalPart`/`Placement` creation; extend if not (the roadmap names this `host.memory.dmidecode`)
- [ ] Docs site scaffold (mkdocs-material): getting-started, CLI reference, probe SDK guide, NetBox custom-field reference

### Hygiene

- [ ] `engine/__init__.py` docstring lists `Reconciler`/`AssertionEngine`/`Scheduler`/`ProposalManager` as if present — keep as forward-looking, or trim to what's implemented, as those components land
- [ ] **CI workflow** (`.github/workflows/ci.yml`) — currently nothing automated runs on push or PR; the test suite is green locally but unverified in PRs. Run on push + PR against `main`:
  - [ ] `uv sync --all-extras --group dev`
  - [ ] `uv run pytest -q` (42 passing locally today)
  - [ ] `uv run ruff format --check .`
  - [ ] `uv run ruff check .`
  - [ ] `uv run mypy src`
  - [ ] `uv run pre-commit run --all-files` (covers anything contributors didn't install hooks for; `.pre-commit-config.yaml` already exists)
  - [ ] Cache the uv environment between runs (`actions/cache` on `~/.cache/uv` keyed by `uv.lock`)
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
