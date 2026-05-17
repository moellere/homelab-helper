# homelab-helper — Roadmap

## Principles

Three rules that shape every phase below.

**1. Each phase produces a usable system on its own.** Not a milestone in a partially-built product; a thing you'd actually run. If you stopped after Phase 1, what you have is a discovery-and-audit tool that's already valuable on its own. If you stopped after Phase 2, you have a continuous-monitoring tool with assertion verification. And so on. This forces clean abstractions and prevents the framework from being held hostage by future phases.

**2. Acceptance criteria are demoable, not aspirational.** Every phase has at least one criterion that ends with "...against the dorktool.com lab." Real artifacts, real tests, no "we'll know it's done when it feels right."

**3. The L2 executor is post-roadmap.** Five phases get to a fully useful read-only system: discovery, continuous monitoring, multi-source coordination, conversational layer, planning. L2 lift (the framework applying changes) is a separate decision later. The roadmap below does not pretend it knows when L2 happens.

## Dependency graph

```
Phase 1 — Inventory & Discovery
    │
    ├─→ Phase 2 — Continuous Reconciliation
    │       │
    │       ▼
    │   Phase 3 — Multi-Source Coordination (adapters)
    │       │
    │       ▼
    │   Phase 4 — Conversational Layer (agents)
    │       │
    │       ▼
    │   Phase 5 — Planning & Recommendations
    │
    └── Cross-cutting: probe catalog growth, docs, test fixtures
```

Phases 2 and 3 are sequential as written but loosely coupled — could be parallelized once Phase 1 is solid. Phase 4 can begin once Phase 3 produces enough cross-source signal to be worth narrating; can ship narratively before all of Phase 3 is done. Phase 5 needs both inventory depth (P1-2) and external context (P3).

## Effort scale

Estimates assume Python-fluent developer at hobby pace — evenings and weekends, with normal life happening. Multiply by ~2 for realistic completion. Ranges reflect "fast track if you're focused" vs "comfortable pace with detours."

---

## Phase 1 — Inventory & Discovery (MVP)

The foundation. After this phase, the framework can scan a network, deep-probe Linux hosts, populate NetBox, track parts across moves, run configuration assertions on a one-shot basis, and produce a meaningful audit. Everything later builds on this.

### Goals

- Implement the Slice 1 schema (11 tables).
- Build the NetBox adapter with read+write for the modeled object types.
- Build the Kernel-SSH adapter for warm host discovery.
- Build cold and warm discovery probes as proper plugins.
- Build the reconciler with current-state inventory maintenance.
- Build the assertion engine for one-shot verifier runs.
- Ship the CLI as the primary interface.
- Produce the day-one audit against the dorktool lab.

### Deliverables

| Deliverable | Notes |
|---|---|
| Harness DB models + Alembic migrations | Per `harness-schema-slice1.md`. SQLite default. |
| Probe plugin SDK | `Probe` base class, entry-point registration, output validation via Pydantic. Documented enough that external contributors can write probes. |
| Probes: `network.subnet-scan`, `network.fingerprint` | Asyncio-based; no nmap dependency required (nmap optional for deeper modes). |
| Probes: `host.identity`, `host.cpu`, `host.memory.dmidecode`, `host.storage`, `host.network`, `host.pci`, `host.gpu`, `host.services` | The warm probe suite. Each is a separate plugin. |
| NetBoxAdapter | Read+write Devices, Interfaces, IPs, VLANs, Prefixes, Clusters, VMs, Services, InventoryItems, custom fields. |
| KernelSSHAdapter | SSH session management, command execution, output parsing primitives shared across host probes. |
| Reconciler | Observation → inventory; current placement maintenance; finding generation. |
| AssertionEngine (one-shot mode) | Runs verifiers, generates AssertionRun rows and Findings. No scheduling yet. |
| FingerprintGenerator | Deterministic fingerprints for finding dedup. |
| CLI: `helper discover network`, `helper discover host`, `helper audit`, `helper findings`, `helper host show`, `helper config` | Primary interaction surface. |
| NetBox bootstrap routine | Creates the harness's custom fields in NetBox on first run. |
| Test fixtures: dorktool lab as integration test | Replayable; CI-friendly. |

### Acceptance criteria

1. **`helper discover network 10.250.0.0/16`** populates the harness DB and NetBox with discovered hosts, fingerprints recognized services (Proxmox, Cockpit, K8s API, web servers), and produces `INVENTORY_GAP` findings for unidentified hosts.
2. **`helper discover host bmax3`** with valid SSH credentials completes the warm probe suite in under 30 seconds, populates `Host.capabilities`, creates `PhysicalPart` and `Placement` rows for every DIMM and storage device, and updates NetBox Device + InventoryItems accordingly.
3. **`helper audit`** against the populated dorktool lab produces the eleven day-one findings from the modeling walkthrough, plus the new ones added in the gap-fill update (CONFIG-DRIFT-CONFIRMED for bmax0 e1000e, STRAY-CONFIG for WAN2, LEGACY-WORKLOAD for the esphome-worker LXCs, UNKNOWN-WORKLOAD for the stopped bmax2 VMs). At least eleven findings; ideally all sixteen.
4. **Re-running discovery against the same lab does not duplicate inventory** — fingerprint dedup works, placements close out cleanly when parts move, findings update `last_seen` rather than creating new rows.
5. **Probe plugin SDK passes the "write a probe in 50 lines" test**: writing a new probe (e.g. `host.systemd-units`) takes a single file under 50 lines of Python plus a Pydantic output schema.

### Effort

**6–10 weeks** of focused evening/weekend work. The DB schema is already designed; the bulk of the time is probes, the NetBox adapter, the reconciler, and integration tests. The reconciler is the most subtle component — expect to rewrite parts of it twice.

### Stop-here value

If you stopped after Phase 1, what you have is **a network discovery + deep host inventory tool with audit output**. That's already valuable — nothing existing combines those well for a homelab. You wouldn't have continuous monitoring, conversational interfaces, or planning, but you'd have something to run quarterly that finds the gaps in your lab.

---

## Phase 2 — Continuous Reconciliation & Assertions

The shift from "discovery is something I trigger" to "the framework knows my lab in near-real-time." Adds the helper-agent and the temporal logic the reconciler needs to handle a continuously-updating world.

### Goals

- Build helper-agent as a deployable daemon for Linux hosts and Pi 4B.
- Add scheduled probe execution (via Scheduler).
- Add temporal awareness to the reconciler: confidence decay, stale-degradation findings.
- Implement the OperationalIntent layer: "ask once, remember forever" pattern fully wired.
- Schedule the AssertionEngine to run verifiers on cadence.
- Build the configuration assertion library with your 16 supplied assertions as the starter corpus.

### Deliverables

| Deliverable | Notes |
|---|---|
| helper-agent daemon | Python, packaged as single binary. Systemd unit. Bearer-token auth. <50 MB RAM target. Works on Pi 4B. |
| `helper agent enroll <hostname>` | Token generation, agent install (via SSH push or pre-built deb/rpm). |
| Observation buffer/replay | Agent buffers locally when server unreachable; drains on reconnect. |
| Scheduler integration | APScheduler-based. Probe schedules per probe definition + per-host overrides. |
| Reconciler temporal logic | Confidence decay (VERIFIED → STALE after freshness window). Stale-degradation finding generation. |
| OperationalIntent reconciliation rules | `expected=off + observed=off = confirmation, not absence`. `STRAY` state suppresses findings forever for the WAN2 case. |
| AssertionEngine scheduling | Cron-style per assertion. Drift findings on transition pass→fail. |
| Seed assertion library | Your 16 ConfigurationAssertions translated into the framework's verifier_spec format. Shipped as a YAML bundle users can adopt. |
| `helper assert run <name>` | Manual trigger for ad-hoc verifier execution. |
| `helper intent set <target> <state>` | Operator command for declaring intent. |

### Acceptance criteria

1. **helper-agent installs on bmax3 and three Pis** without manual surgery, runs for **7 days unattended**, posts observations every 5 minutes, never crashes.
2. **Deliberately break one ConfigurationAssertion** (e.g. disable the e1000e service on bmax1) — within **15 minutes**, a `CONFIG_DRIFT` finding appears with the correct fingerprint and evidence.
3. **`operational_intent`-tagged stopped VMs produce no finding.** Stop `claude-server` on bmax2 with intent `stopped-by-design`; no `UNKNOWN_WORKLOAD` finding. Stop a different VM without intent; finding appears.
4. **Power-policy-aware probing**: bmax4 and bmax5 (WoL workers) probed during their expected-off windows produces no finding for unreachability. Probe runs are skipped or rerouted as designed.
5. **The bmax0 e1000e drift gets confirmed automatically** when the verifier runs against the gap-filled inventory — `CONFIG_DRIFT_CONFIRMED` finding generated without manual intervention.
6. **Re-running Phase 1's `helper audit` after Phase 2's agents are deployed** — same set of findings, but now with `last_seen` updating continuously and `confidence` reflecting real freshness.

### Effort

**4–6 weeks**. The agent itself is small (1–2 weeks); the reconciler temporal logic is subtle (2 weeks); assertion verifier formats and the YAML corpus are non-trivial (1–2 weeks).

### Stop-here value

With Phase 2 you have **a continuous lab-monitoring tool with drift detection and intent-aware suppression**. This is already more than most existing tools do — you'd be running it routinely and getting value daily. No conversational interface yet, no multi-source coordination, no recommendations, but the audit you ran quarterly is now alive.

---

## Phase 3 — Multi-Source Coordination

The architecture's premise (multi-source SoT, not single SoT) only matters once you actually have multiple sources. This phase adds the adapters that make the dorktool lab's full complexity visible.

### Goals

- Ship the ProxmoxAdapter, K8sAdapter, UniFiAdapter, CloudflareAdapter, GitArgoCDAdapter.
- Wire each adapter into the engine so observations from each source flow through the same reconciler.
- Implement synthesized views: service-to-host-to-pod resolution, DNS split-brain visibility, ArgoCD-vs-K8s drift, stray-config detection.
- Add a query API that lets agents and the UI ask cross-source questions.

### Deliverables

| Deliverable | Notes |
|---|---|
| ProxmoxAdapter | Read cluster state, VMs, LXCs, Ceph health, OSD weights, replication status. |
| K8sAdapter | Read nodes, pods, services, labels, taints, events. Populates `local_context_data` on logical service VMs. |
| UniFiAdapter | Read DNS records, DHCP leases, switch port config, network/VLAN definitions, AP topology. |
| CloudflareAdapter | Read DNS records, ACME cert state, zones. |
| GitArgoCDAdapter | Read declared app state from Git; read ArgoCD application sync status; compare. |
| Cross-source view builder | "Show me service X" returns the synthesized record across NetBox + K8s + UniFi + Cloudflare. |
| Stray-config detection | UniFi-configured networks/VLANs with no observed traffic in N days → `STRAY_CONFIG` finding. |
| Service endpoint reconciliation | ServiceEndpoint table populated; internal vs external endpoints visible. |
| New CLI verbs: `helper view service`, `helper view host`, `helper diff git-vs-cluster` | The query surface. |

### Acceptance criteria

1. **Each adapter passes its smoke test against a real instance** — `helper adapter test proxmox` succeeds against the dorktool Proxmox cluster, returns expected observations.
2. **`helper view service home-assistant`** returns a complete record: NetBox Service entry, hosting VM (bmax0:105), K8s state (n/a — it's a Proxmox VM), UniFi internal DNS record, Cloudflare external DNS record, ArgoCD app status (n/a), recent observations.
3. **DNS split-brain visible**: querying any service produces both internal and external endpoint rows with the correct resolvers.
4. **Git-vs-cluster drift produces findings**: introduce a manual change to a K8s resource that ArgoCD manages, finding appears identifying the divergence.
5. **Ceph health surfaced**: `helper view storage ceph` shows OSD weights, near-full warnings, replication lag, and (still) the bmax0 1 GbE bottleneck finding now with confirming Ceph-side evidence.
6. **The bmax0 Ceph bottleneck finding gets richer**: instead of "bmax0 has 1 GbE and the others have 2.5 GbE," it's "bmax0 has 1 GbE and the others have 2.5 GbE *and Ceph backend traffic is currently averaging X MB/s with peaks Y, capped near bmax0's link saturation*."

### Effort

**5–7 weeks**. Each adapter is roughly a week of careful work plus integration tests. Cross-source view builder is its own ~1 week of design. Stray-config detection requires temporal data so leans on Phase 2.

### Stop-here value

With Phase 3 you have **a unified view across all the systems that compose your lab**. Findings get sharper, the audit gets more complete, and you have the data needed to drive the conversational and planning layers. Still no chat, still no recommendations, but the underlying data layer is now genuinely cross-cutting.

---

## Phase 4 — Conversational Layer

The agent layer. Adds the LLM router, narration, conversational discovery, and the MCP server. The framework gains a voice; users can chat about their lab and get sensible answers.

### Goals

- Build the LLM router with Ollama default and BYOK cloud backends.
- Wire the agent layer's read-only access to engine query methods.
- Build the Narrator agent: findings → prose.
- Build the Conversational Discovery Agent: interview-style host onboarding.
- Build the Skill Inferer: passive skill profile updates from chat.
- Ship the MCP server exposing engine and adapter capabilities.
- (Maybe) ship a minimal Web UI for chat + finding browsing.

### Deliverables

| Deliverable | Notes |
|---|---|
| LLMRouter | Task-class → privacy → capability tier → backend selection. Refuses cleanly when policy can't be satisfied. |
| Ollama backend | First-run auto-detect; install prompt if missing. |
| Anthropic / OpenAI / OpenAI-compatible backends | BYOK; not active unless user configures keys. |
| Narrator agent | Converts findings into prose. Small/Mid tier. |
| Conversational Discovery Agent | Interviews user to fill inventory gaps. Bidirectional: agent asks, user answers, agent updates harness DB. |
| Skill Inferer | Passive signal; updates per-domain trust hints. |
| MCP server | Exposes engine capabilities as MCP tools. Compatible with Claude Desktop / Claude Code / Cursor. |
| `helper chat` CLI verb | Basic CLI chat for users without a UI. |
| Minimal Web UI (optional) | If shipped: SvelteKit, chat + findings browser. Can defer to Phase 4.5 if scope is tight. |

### Acceptance criteria

1. **`helper chat` with default config invokes Ollama** and answers a basic question ("what hosts do I have?") correctly.
2. **"What's wrong with my Ceph cluster?"** produces a narrated answer citing the CEPH_BOTTLENECK finding, explaining the 1 GbE/2.5 GbE asymmetry, and listing the four candidate mitigations from the day-one report.
3. **Conversational Discovery Agent successfully populates a new host** via chat: user says "I just added a new mini-PC," agent walks through identity + IP + role + credentials, populates harness DB, kicks off warm discovery.
4. **MCP server: Claude Code can drive a discovery run** by saying "discover the host at 10.250.6.27" — calls the right tools, results land in NetBox.
5. **Strict-local privacy mode**: configure `privacy=strict-local`, ask a question that needs Frontier capability — router refuses cleanly with a clear message.
6. **Skill profile demonstrably updates**: after chatting about ZFS and Ceph and Kubernetes, the skill profile reflects "storage: intermediate; container-orchestration: advanced" without the user setting these manually.

### Effort

**5–7 weeks**. LLM router is small but tricky to get right. Each agent is 1–2 weeks. MCP server is 1 week if you've built one before, 2 if not. Web UI deferrable.

### Stop-here value

With Phase 4 you have **a conversational lab assistant that knows your specific infrastructure and surfaces real findings with context**. This is the "demo to a friend" moment — the framework is genuinely impressive at this point even though it still hasn't recommended anything or fixed anything. Power users with Claude Code or Cursor get a fully MCP-driven workflow.

---

## Phase 5 — Planning & Recommendations

The framework starts producing recommendations, not just observations. The planner takes inventory + workload catalog + user goals and emits multi-option plans with tradeoffs.

### Goals

- Build the WorkloadProfile schema and seed a starter library.
- Build the NetworkPath abstraction (typed link graph the planner queries).
- Build the solver (OR-Tools or similar) for placement reasoning.
- Build the Planner agent: solver output + LLM narration.
- Ship hardware reconfiguration recommendations: "if you move this DIMM..."
- Ship the bottleneck analyzer for known patterns (Ceph, network, storage, scheduler).

### Deliverables

| Deliverable | Notes |
|---|---|
| WorkloadProfile schema | Fields: baseline resources, scaling shape, arch support, dependencies, data-gravity, common deployment artifacts. |
| Seed workload library | ~50 common homelab services: Plex, Jellyfin, Home Assistant, Nextcloud, Immich, Pi-hole, Frigate, MinIO, Grafana, etc. Community contribution path. |
| NetworkPath abstraction | Typed link graph: cables, tunnels, wireless. Planner queries paths between hosts with characteristic inheritance. |
| Solver (OR-Tools-based) | Constraint problem: workloads × hosts × constraints (CPU, RAM, GPU, arch, storage, network, intent). Output: candidate plans. |
| Planner agent | Calls solver; LLM narrates. |
| Reconfiguration reasoner | "If you swap these DIMMs you can do X" — uses Placement history + current capability gaps. |
| Bottleneck analyzer | Pattern library: Ceph link asymmetry, K8s scheduler pressure, NFS saturation, etc. |
| New CLI: `helper plan add-workload <name>`, `helper plan rebalance`, `helper bottlenecks` | The recommender surface. |

### Acceptance criteria

1. **WorkloadProfile library has ≥50 entries** covering top homelab services.
2. **"If I add Immich, where should it run?"** returns a placement recommendation with reasoning — including arch constraints (amd64 only), RAM headroom, ML-tagging GPU optionality, storage proximity to media.
3. **`helper plan rebalance`** against the dorktool lab returns at least three candidate plans with tradeoffs narrated by the planner agent, e.g. "rebalance Proxmox storage with current hardware," "rebalance with one DIMM move," "rebalance with one part purchase."
4. **The Ceph bottleneck recommendation** produces the four candidate mitigations from the day-one report (CRUSH reweight, USB 2.5GbE add, OSD relocate, accept) — *generated by the framework, not hardcoded*.
5. **Reconfiguration reasoner flags the bmax2 surplus**: 24 GB of RAM, two stopped VMs, an i5-8500T — would propose either spinning the VMs back up, moving the DIMMs to a more-loaded host, or accepting capacity reserve as the intent.
6. **NetworkPath correctly characterizes the Wyola↔Covington case**: planner refuses to place a Ceph-replicated workload across the VPN, explains why (latency + reliability inheritance from worst link in path).

### Effort

**8–12 weeks**. The workload library is the biggest sink — accurate profiles take measurement effort, and 50 of them isn't trivial. The solver is the next biggest — constraint modeling is finicky, cost function tuning is real work. Planner agent itself is small.

### Stop-here value

With Phase 5 you have **a homelab framework that recommends, not just reports**. This is the original vision realized. From here, the obvious next step is L2 (the framework actually applies the recommended changes), but Phase 5 alone is a complete product — recommendations a human applies by hand.

---

## Post-roadmap (Phase 6+)

These are real future phases, deliberately not committed in this roadmap:

- **L2 executor + trust gradient**. Action manifests become executable; the trust gradient gates execution per domain per user trust level. Significant safety engineering — rollback infrastructure, snapshot orchestration, kill switch, audit trail integrity. Probably 3+ months of focused work.
- **Operate & maintain**. Incident triage agent, update orchestration, predictive failure analysis using observation history, anomaly detection.
- **Multi-tenant + hosted service**. The Nabu-Casa-style subscription tier — managed LLM access, off-site backup, remote access, mobile push, community template marketplace.
- **Heterogeneous architecture support beyond Linux**. FreeBSD probes (TrueNAS Core, pfSense/OPNsense), macOS probes (Mac mini servers), Windows probes (Windows hosts in mixed labs).

Each of those is its own roadmap-scale effort. Not promising any of them; just naming them so they don't accidentally creep into Phase 5.

---

## Cross-cutting tracks

Things that happen alongside every phase, never owned by a single one.

### Probe catalog growth

Probes are plugins from day one (Phase 1 decision). The catalog grows continuously:

- Phase 1: ~10 core probes covering Linux hosts.
- Phase 2: agent-side probes for continuous mode.
- Phase 3: API-based probes per adapter (Proxmox, K8s, UniFi, etc.).
- Phase 4+: workload-specific probes (Ceph internals, K8s detailed health, etc.).

A community contribution path matters by Phase 3 — by then you have non-toy users who want to add their own.

### Documentation

- Phase 1: getting-started, CLI reference, probe SDK guide, NetBox custom-field reference.
- Phase 2: agent deployment, assertion authoring.
- Phase 3: adapter authoring guide (so users add adapters for tools you don't support).
- Phase 4: chat interaction patterns, MCP integration recipes.
- Phase 5: workload profile contribution, solver tuning.

A docs site (mkdocs-material or similar) starts in Phase 1 and grows with each phase. Don't defer it.

### Test fixtures

- Phase 1: dorktool lab as integration test fixture. Replayable, CI-friendly.
- Phase 2: synthetic continuous-mode scenarios (drift injection, intent transitions).
- Phase 3: mock external APIs (Proxmox, K8s) for adapter testing without a real homelab.
- Phase 5: workload-placement scenarios with known-correct answers.

Replay-based testing for the reconciler is non-negotiable — without it, regressions in reconciliation logic are devastating.

---

## Risks worth tracking

**Reconciler bugs are catastrophic.** Bad reconciliation logic produces wrong findings or, worse, corrupts NetBox. Mitigation: heavy unit testing, replay tests, dry-run mode for all NetBox writes during development, "trust but verify" comparison runs early on.

**Probe brittleness.** Linux distros vary; kernel versions vary; tools have different output formats across releases. Mitigation: defensive parsing, version-aware probes, graceful degradation, observation `confidence` field used aggressively.

**LLM hallucination at the wrong level.** Even with the reconciler/narrator split, the LLM could narrate a finding incorrectly or invent a recommendation. Mitigation: every LLM output is citation-bound to specific findings/observations; UI surfaces "this came from finding X" explicitly; no LLM output is presented as fact without a source.

**Scope creep into L2.** The pull to "while we're at it, let's just fix this" is constant. Mitigation: L1-only stance is explicit in the roadmap; every PR reviewer checks "does this enable a write path that wasn't agreed to?"

**Workload library accuracy decay.** Service profiles drift as software changes. Mitigation: schema includes version/last-verified per profile; community contribution lowers the maintenance burden on the project owner.

**NetBox upstream changes.** NetBox occasionally makes breaking schema changes. Mitigation: pin a tested NetBox version range; track NetBox releases; integration tests against new NetBox releases before bumping the supported range.

**Burnout.** Hobby pace + ambitious roadmap = real risk. Mitigation: stop-here markers at every phase mean every phase ships something complete. Easier to step away from a complete Phase 2 than a half-built Phase 3.

---

## Total roadmap effort

Adding the phase ranges: **28–42 weeks** for Phases 1–5, at the optimistic "focused hobby pace" estimate. Realistic (2× factor) is **56–84 weeks** — roughly a year to two of evening/weekend work. That's a lot. The stop-here markers exist precisely because *every* point along that road produces a working system; you decide phase-by-phase whether to continue.
