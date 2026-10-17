# homelab-helper

> An open-source framework for homelab inventory, audit, recommendations, and
> operator-gated execution.

**Status: 1.0.2.** Every planned phase is built and
validated against a real two-site lab: discovery and inventory, audit, chat and
MCP tools, placement / rebalancing / rightsizing recommendations, version
currency and backup posture, per-probe cadences, a weekly digest, and a status
endpoint for your dashboard. Execution (Phases 6–8: guest power, migration and
resize, Kubernetes workloads, Argo CD syncs, DNS records, a rolling node update,
agent-triggered runs behind a phone-tap approval) is **opt-in and off by
default**: nothing runs until you raise a trust cell. Phases 10 and 11 added
predictive operations (disk health, capacity trends, baselines, maintenance
runs) and Talos as a managed plane on top of the 1.0 surface. The CLI verbs, MCP
tool names, configuration variables and the probe contract are declared stable
in [stability and deprecation](https://moellere.github.io/homelab-helper/stability/);
schema changes ship as Alembic migrations that `helper db init` applies, on
SQLite and Postgres alike.

`homelab-helper` is for people who run their own infrastructure at home. It
discovers what you have, maintains one coherent inventory across the many
sources of truth a real homelab spans (kernel probes, NetBox, Proxmox,
Kubernetes, Talos, UniFi, MikroTik, Cloudflare, Argo CD, OpenMediaVault, Home
Assistant), surfaces drift and gaps as auditable findings, and proposes
changes. Everything is "propose, never apply" (L1) unless you opt in — and
that read-only product is complete on its own. Execution (L2) sits behind the
**trust gradient**: a deterministic, operator-controlled authorization model
that the framework can never escalate on its own, and that no LLM is ever in
the path of.

See [`architecture.md`](./docs/architecture.md) for the design,
[`roadmap.md`](./docs/roadmap.md) for the phased plan,
[`backlog.md`](./docs/backlog.md) for what's left, and
[`releasing.md`](./docs/releasing.md) for how versions ship.

## What it does today

- Scan a network and fingerprint live hosts
- Deep-probe Linux hosts over SSH (CPU, memory, storage, network, PCI, GPU, services) and Talos nodes over the machine API
- Maintain part-level identity that survives moves (DIMMs, SSDs, NICs)
- Read the management planes — Proxmox, Kubernetes, UniFi, MikroTik, Cloudflare, Argo CD, OpenMediaVault, Home Assistant — and reconcile them against kernel ground truth (DNS split-brain, git-vs-cluster drift, stray config)
- Push inventory into NetBox via its API
- Run configuration assertions and produce reconciliation findings
- Produce a day-one audit against a real homelab
- Answer questions about the lab in chat (local Ollama by default, BYOK cloud opt-in) and expose everything as MCP tools
- Recommend placement, rebalancing, and reconfiguration, and flag known bottleneck patterns — respecting node-local storage and the hosts you mark `no-new-guests`
- Keep usage history from Proxmox's RRDs and recommend cores and memory per guest from it
- Check version currency (OS, Ceph, Kubernetes and Talos end of support, pending updates), backup posture, storage efficiency, and suggest services your hardware could run
- Judge every drive against its own SMART history — failing, reallocating, CRC errors on the rise, running hot, wearing out, simply old — before it is an outage
- Project filesystems, node memory and CPU forward from the usage history and say when each runs out; flag a guest or node that departs from its own baseline
- Run probes and assertions on their own cadences from one daemon, draft remediations from findings through deterministic playbooks, and ask your phone before acting
- Execute a proposed action only after you raise its trust cell — deterministic gate, receipts, snapshots, rollback, elevation windows, kill switch, and a notification after every run you were not asked about
- Run a node's drain → update → reboot → return as one sequence of gated steps that stops at the first failure
- Serve a one-screen status (`helper status`, `GET /status`) for Homepage or Home Assistant, and send a weekly digest

## Running it locally

> Everything is **read-only (L1) — it proposes, never applies** — until you
> raise a trust cell yourself (Phase 6, opt-in). Every write to the lab goes
> through one gate, and every discovery is a read.

**Requirements.** Python 3.12+ on Linux or macOS for the tool itself. Hosts
you deep-probe need SSH with key auth; the SMART and DIMM probes run
`smartctl` and `dmidecode` under `sudo -n`, so give the probe user passwordless
sudo for those two commands or accept that disks and DIMMs report without
identity. Talos nodes need a working `talosctl`; Kubernetes needs `kubectl`
and a kubeconfig. Chat works out of the box against a local
[Ollama](https://ollama.com); cloud models are bring-your-own-key.

**1. Install.** As a tool on your PATH (no checkout needed):

```bash
uv tool install homelab-helper                      # from PyPI (upgrading from an rc installed with --prerelease allow? use --reinstall)
# or: pipx install homelab-helper
helper --install-completion                          # bash / zsh / fish
# bleeding edge: uv tool install git+https://github.com/moellere/homelab-helper
```

Or from a checkout for development (see [Development](#development)), where
every command below is prefixed with `uv run`:

```bash
uv sync --all-extras --group dev
```

**2. Initialize.** State lives in a per-user directory, not the working
directory: the database under `~/.local/share/homelab-helper/` and your
credentials under `~/.config/homelab-helper/.env` (XDG variables are honoured;
`HOMELAB_HELPER_HOME` puts both in one place, e.g. a container volume).
`HOMELAB_HELPER_DATABASE_URL` overrides the database entirely; a `postgres`
extra is available (`postgresql+asyncpg://user:pw@host/db`), and CI runs the
suite against Postgres as well as SQLite.

```bash
helper config init         # writes the commented .env template
helper db init             # alembic upgrade + register entry-point probes
helper db status
helper config              # what the harness will actually talk to
# helper db reset --yes    # DESTRUCTIVE — dev only
```

**3. Configure source credentials.** Uncomment what you use in the `.env`
that `helper config init` wrote. A project `.env` (repo checkout, gitignored)
is loaded first, then the per-user file, then `~/.env`; explicit exports
always win. Each source only needs its variables when you run that `discover`
verb (all are prefixed `HOMELAB_HELPER_`):

| Source | Variables |
|---|---|
| Database | `DATABASE_URL` (default local SQLite) |
| UniFi | `UNIFI_URL`, `UNIFI_API_KEY`, `UNIFI_SITE`, `UNIFI_VERIFY_SSL` |
| Cloudflare | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE` (or `CLOUDFLARE_ZONE_ID`) |
| Argo CD | `ARGOCD_URL`, `ARGOCD_API_TOKEN`, `ARGOCD_VERIFY_SSL` |
| Proxmox | `PROXMOX_URL`, `PROXMOX_TOKEN_ID`, `PROXMOX_TOKEN_SECRET`, `PROXMOX_VERIFY_SSL` |
| Kubernetes | `KUBECONFIG`, `KUBE_CONTEXT` |
| OpenMediaVault | `OMV_URL`, `OMV_USERNAME`, `OMV_PASSWORD`, `OMV_VERIFY_SSL` |
| Home Assistant | `HASS_URL`, `HASS_TOKEN` (a long-lived access token; a non-admin user is enough), `HASS_VERIFY_SSL` |
| Approval channel (Phase 7) | `APPROVAL_NOTIFY_SERVICE` (an HA `notify.*` service that reaches your phone, e.g. `notify.mobile_app_pixel`), `APPROVAL_TIMEOUT_S` (default 300); uses the Home Assistant URL/token above |
| MikroTik | `MIKROTIK_URL`, `MIKROTIK_USERNAME`, `MIKROTIK_PASSWORD` (a read-only user with the `rest-api` policy), `MIKROTIK_VERIFY_SSL`, `MIKROTIK_NAME` |
| Service identity | `SERVICE_ALIASES` — YAML mapping hostnames to service names when the leftmost-label default is wrong (see `fixtures/service-aliases.example.yaml`) |
| NetBox | `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_VERIFY_SSL` |
| LLM (chat) | `LLM_PRIVACY` (`strict-local`/`prefer-local`/`open`), `OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_TIER`; BYOK: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENAI_COMPAT_BASE_URL` |

**Secrets don't have to be plaintext.** Any secret-valued variable accepts a
reference instead of a literal, resolved with your own tooling and keys:

```bash
HOMELAB_HELPER_PROXMOX_TOKEN_SECRET=file:~/.config/homelab-helper/secrets.yaml#proxmox   # plain YAML/JSON
HOMELAB_HELPER_UNIFI_API_KEY=file:~/.config/homelab-helper/secrets.yaml.age#unifi         # age (HOMELAB_HELPER_AGE_IDENTITY)
HOMELAB_HELPER_NETBOX_TOKEN=file:~/secrets.sops.yaml#netbox                               # sops -d
HOMELAB_HELPER_HASS_TOKEN=keyring:homelab-helper/hass                                     # OS keyring: install homelab-helper[keyring]
```

`helper config` shows `set via file` / `keyring` / `env` for a reference and
never prints a value; resolved values are scrubbed from the MCP server's
error strings.

**4. Run.** Discovery is read-only; add `--persist` to write to the DB and
`--dry-run` to preview:

```bash
helper --help

helper discover host <name> --ssh-user <u> --ssh-key <path>
helper discover unifi --persist
helper discover mikrotik --persist   # RouterOS 7: static DNS → endpoints, subnets + leases → stray config
helper discover cloudflare --persist
helper discover argocd
helper discover proxmox --persist
helper discover omv --persist   # OpenMediaVault NAS: filesystems, disks, shares; stray exports → findings
helper discover hass --persist  # Home Assistant: version, integrations, entity summary
helper discover printwarden --persist   # print-warden: printers offline, Klipper down, stale telemetry → findings

helper view service <name>      # internal/external endpoints + DNS split-brain
helper view host <name>         # guests, endpoints, findings
helper diff git-vs-cluster --persist   # Argo CD drift → DRIFT_CANDIDATE findings
helper audit
helper findings list
```

No lab yet? `helper discover replay` loads a bundled three-host lab and
produces thirteen findings with no hardware, credentials or SSH — the
[getting-started guide](https://moellere.github.io/homelab-helper/getting-started/)
walks through what each one means. What you can build on across releases —
verbs, MCP tools, the probe contract, the database — is declared in
[stability and deprecation](https://moellere.github.io/homelab-helper/stability/).

Keep tokens and keys in the per-user `.env` or behind a secret reference —
never in a checkout, never in an MCP client's config block.

### Keeping the inventory honest

Hardware gets reflashed, moved, and renamed; nothing in discovery can prove
that "the drive that used to be in `pi-cp1`" is "the drive now in `pi-cp2`",
so the cleanup verbs are explicit and operator-driven:

```bash
helper host retire pi-cp1 -r "reflashed as pi-cp2"   # records the intent, closes its placements, resolves its findings
helper host intent node0 --no-new-guests -r "runs hot"  # keep what runs there; planners never send it more
helper part show SSD-A                               # a part's identity and placement history
helper part merge 0x5000c500deadbeef --into SSD-A    # same drive under a second identity: fold it in
helper service resolvers                             # every (scope, resolver) endpoint slice
helper service retire-resolver unifi                 # drop the slice a renamed controller left behind
helper service aliases                               # the alias map as loaded
```

A retired host stays in the inventory (history is history) but leaves the
planners, and chat sees it as retired. Renaming a UniFi controller changes
its resolver tag; the next sync warns when the old slice is now a duplicate,
and `retire-resolver` removes it. When two distinct services share a short
name, or one service spans unrelated names, the alias map overrides the
leftmost-label default; re-running discovery re-points existing endpoints.

### Chat with your lab

`helper chat` answers questions from the reconciled inventory — grounded in
facts, never inventing hosts or findings. Runs against local **Ollama by
default** (`localhost:11434`); add a BYOK cloud key to enable fallback, and
control routing with `HOMELAB_HELPER_LLM_PRIVACY` (`strict-local` never sends
anything to a cloud model — the router refuses rather than silently
downgrading or leaking).

```bash
helper chat "what hosts do I have?"     # one-shot
helper chat                             # REPL ('exit' to leave)
helper findings narrate                 # open findings as prose
helper onboard "a new mini-PC"          # conversational host onboarding
```

`helper onboard` interviews you about a new machine, then **validates and asks
for confirmation before writing anything** — the model proposes, deterministic
code decides. It collects at most an SSH username and key *path* (never key
material or passwords); add `--probe` to kick off warm SSH discovery right
after registration.

### Placement recommendations

"If I add Immich, where should it run?" — `helper plan` answers from a
67-service workload library plus your reconciled inventory. Hard constraints
(arch, RAM, GPU) reject with reasons; survivors are ranked on headroom,
GPU optionality, and data gravity. Deterministic first; `--narrate` adds the
Planner agent's prose on top.

Declare your sites and inter-site links (VPNs, wireless hops) in a topology
file — see `fixtures/network-topology.example.yaml` — and placement becomes
network-aware: a path inherits **the worst of its links**, so sync-replicated
workloads (Ceph, etcd) are refused across a VPN, with the reason spelled out.

Two more constraints come from the fleet itself: a guest with a volume on
node-local storage (an installer ISO on `local` counts) is never proposed for
migration, and a host you mark `no-new-guests` is never a destination. Both
are listed in the plan's caveats so it says what it refused to touch.

```bash
helper plan path node0 remote-node0 --workload ceph-osd   # path verdict
helper plan workloads                    # browse the library
helper plan add-workload immich          # ranked hosts + reasons
helper plan add-workload immich --narrate
helper plan rebalance --narrate     # 3 candidate plans with tradeoffs
helper bottlenecks --persist        # known patterns → findings + mitigations
helper plan surplus                 # idle capacity → reconfiguration options
```

As you chat, a **skill profile** builds passively (deterministic keyword
inference, no extra LLM calls): `helper skills` shows it, `helper skills set
storage advanced` pins a domain so inference can't change it. The profile
tunes how much chat explains — and later feeds per-domain trust hints.

Every reply is footed with the backend that served it, e.g.
`[ollama: llama3.2 (small, local)]`.

### Executing proposals (opt-in, Phase 6)

Nothing executes until you raise a trust cell. The gate is `decide()`, a pure
function over the cell's level, the domain ceiling, per-host boundaries, open
elevation windows, and whether a rollback was verified — never an LLM.

Write surfaces today, each with a verified rollback path:

| Domain | Action kinds | Undo |
|---|---|---|
| hypervisor / containers (Proxmox guests) | `start` `stop` `shutdown` `restart`, `migrate` (to a named node), `cpu-type` (QEMU; applies at next stop/start), `resize` (cores and/or memory; QEMU without hotplug at next stop/start, containers live; refused beyond the node's CPUs/memory) | prior power state or snapshot, prior node, prior config |
| containers (Kubernetes) | `workload-restart`, `workload-scale` on a deployment / statefulset / daemonset | rollout undo, prior replicas |
| containers (Argo CD) | `argocd-sync` (optionally pinned to a revision, optionally pruning) | Argo CD's own sync history |
| dns (UniFi static DNS) | `dns-record` (create or update one name + type) | the prior record, or deleting the created one |
| host-os (Proxmox nodes over SSH) | `node-update` (`apt dist-upgrade` on one named node; what runs is fixed in code), `node-reboot` (reboots a drained node and only succeeds once it is online, quorate and reachable again) | **none** — reported as unverified, so these cells can never reach AUTONOMOUS |
| host-os (Talos nodes over `talosctl`) | `talos-upgrade` (one node to a release tag; the installer image is the node's own with the tag swapped, and the receipt only says succeeded once the node is back on the new tag and ready) | `talosctl rollback` — the previous install stays in the other boot partition |
| containers (Kubernetes control plane over `talosctl`) | `k8s-upgrade` (`talosctl upgrade-k8s --to <version>` from one control-plane node; always blast radius `cluster`; refused unless the cluster is healthy, every member runs one Talos release, and the target is newer by at most one minor) | **none** — reported as unverified, so the cell can never reach AUTONOMOUS |

```bash
helper trust show                                   # every cell sits at PROPOSE by default
helper trust grant hypervisor restart single-host confirm
helper trust grant hypervisor migrate single-host confirm
helper trust grant containers workload-restart single-service confirm
helper trust grant containers argocd-sync single-service confirm
helper trust grant dns dns-record single-service confirm
helper trust grant hypervisor resize single-host confirm
helper trust grant host-os node-update single-host confirm   # stays at CONFIRM for good: no inverse
helper trust grant host-os node-reboot single-host confirm
helper trust grant host-os talos-upgrade single-host confirm # has a verified inverse, so it can be promoted
helper trust grant containers k8s-upgrade cluster confirm    # stays at CONFIRM for good: no inverse
helper approvals show                               # channel status, what would ask you, who answered
helper daemon run --once                            # discovery → playbooks → listener, one pass (cron-friendly)
helper daemon run                                   # the same on cadences, until Ctrl-C
helper exec list                                    # pending action proposals
helper exec run <proposal-id>                       # asks at CONFIRM; runs unattended only at AUTONOMOUS
helper exec receipts                                # what ran, at which level, with its rollback state
helper exec rollback <receipt-id>
helper window open --reason "maintenance" --minutes 60 --host node2
helper window kill                                  # revoke every open window now
helper trust history                                # the append-only audit spine
```

Clean confirmed runs promote a reversible, low-blast cell one rung; one bad
outcome demotes it and puts it on probation. See `docs/architecture.md`
("Trust gradient") for the model.

### Version currency (Phase 8.1)

`helper discover versions` (or `run_discovery("versions")` over MCP) checks what
is out of date and records `version-drift` findings: Proxmox nodes with pending
package updates or on mixed `pve-manager` versions, hosts whose OS is past or
within 180 days of end of support, a Ceph release approaching or past its
upstream end of life or a cluster mid-upgrade with daemons on different versions
(all dates live in `data/os-eol.yaml`; nothing is guessed), Kubernetes nodes
on different kubelet or Talos versions, a Kubernetes minor past upstream patch
support or a Talos minor two releases behind, and pending Home Assistant
updates. A source that cannot be reached is reported and its findings are left
alone.

### Backup posture (Phase 8.2)

`helper discover backups` reads the Proxmox backup jobs and every backup
storage and records `backup-gap` findings: guests no enabled job covers,
covered guests whose newest backup is older than twice the job interval (or
that have none), newest backups that failed verification, backups still kept
for guests that no longer exist, and backup storage past 80 / 90%.

### Usage history (Phase 8.3)

`helper discover usage` turns Proxmox's own round-robin data into hourly and
daily rollups per node and guest — about a month of hourly and a year of daily
history on the very first run — and prunes past a horizon so the table stays
bounded. `helper usage [name]` shows CPU and memory p95 and peak against what
each host or guest is allocated; agents read the same through `usage_summary`.


Workload usage (Phase 11.4): when `kubectl` is on the path and the cluster
runs metrics-server, `helper discover usage` also records one hourly sample
per Deployment / StatefulSet / DaemonSet — pod CPU and memory summed per
workload, CPU as a fraction of the workload's declared limits (or requests) —
so `usage_summary` and the Phase 10.3 baselines cover the cluster's workloads
as well as the hypervisor's guests. Run `helper discover usage` hourly (a cron
line; `schedule.yaml` covers probes and assertions, not sources) for a workload
baseline to form.

### Rightsizing (Phase 8.4)

`helper plan rightsize` reads that history and recommends cores and memory per
guest — more cores where the CPU p95 runs hot, fewer where even the peak is low,
less memory where the peak leaves a lot unused, and flags guests idle for the
whole window. Each recommendation states the allocation, the observed p95 and
peak, the window and the proposed value; a guest with under a week of history
gets none. VM memory is only ever shrunk, never grown: Proxmox's figure for a VM
includes the guest's page cache. `helper plan rebalance --basis usage` plans
migrations on observed memory instead of allocations. `--persist` records the
recommendations as findings, and the `rightsize` playbook drafts each cores or
memory change as a `resize` proposal (idle guests are reported, never drafted).
Like every other action it runs only through the trust gate: grant
`hypervisor resize single-host` (VMs) or `containers resize single-host`
(containers) at CONFIRM and the daemon asks your phone; a VM's new size takes
effect at its next stop/start, and `helper exec rollback` restores the prior
values.

### Disk health (Phase 10.1)

Every deep-probed host's drives are judged on each reconcile against the host's
own SMART history: SMART FAILED, reallocated or pending sectors (HIGH when the
count is growing), CRC errors that grow inside the window (a cable, usually — a
static count from years ago raises nothing), temperature over the line for that
kind of drive judged on the window's peak, NVMe wear past a threshold or on a
rate that reaches 100% within six months, and spinning drives past 50 000
hours. They land as `storage-health` findings keyed by serial, so a replaced
drive closes the old finding and starts clean. Thresholds are constants in
`engine/disk_health.py`, with the evidence on every finding.

### Capacity trends (Phase 10.2)

`helper discover capacity` projects the usage history forward: every node root
filesystem and NAS filesystem gets a `filesystem-full` finding when it is past
85 % today and a `filesystem-headroom` one when its daily growth reaches full
within 90 days (HIGH within 30), and a node whose daily mean memory or CPU
trends across 90 % of RAM / 80 % CPU inside the horizon gets `memory-headroom`
/ `cpu-headroom`. A step is not a trend — the fit restarts after the last jump
(a guest moving in, a big copy) — and a node's memory trend only counts where
its guests' allocations could actually carry it over the line. The weekly
digest leads with what runs out first.

### Baselines and anomalies (Phase 10.3)

`helper discover anomalies` compares every guest and node with its *own* last
28 days: percentile bands (p10 / p50 / p95) per metric — CPU, network, disk
I/O, memory — and an `anomaly` finding when the newest three hours all sit
well above the band (a service that started spinning) or, for a subject that
is normally busy, well below it (a backup that silently stopped). The band and
the recent values are on the finding; no model anywhere.

### Maintenance runs (Phase 10.4)

The rolling node procedure, inside the gradient. `helper maintenance plan
<node>` drafts one proposal per step — migrate every running guest to the
emptiest cluster-mate with room (a guest on node-local storage is shut down
instead; a `no-new-guests` host is never chosen, though `--target` may name
one for this run), `node-update`,
`node-reboot`, then bring everything back — and `helper maintenance run <id>`
executes them in order after one consent, relaying it to each step's CONFIRM.
Every step is gated by `decide()` on its own cell and leaves its own receipt;
the first failed or refused step ends the run and the remaining proposals are
rejected. Over MCP the same run asks your phone once. Because `node-reboot`
has no inverse, a run can never execute unattended. The command ignores a
terminal hangup, so a dropped SSH session does not kill a run mid-step (tmux is
still the better habit for a 20-minute procedure). A run that stopped after
its drain is finished by a second one: `helper maintenance plan <node>
--resume <stopped-run>` adds the guests it moved and never returned to the new
run's return leg (a guest that has moved since is left alone).

```bash
helper maintenance plan bmax2            # the steps, as proposals
helper maintenance run 01a1…  --yes      # drain → update → reboot → return
helper maintenance list                  # every run and how far it got
```

### Talos upgrades (Phase 11.1)

The Kubernetes nodes join the gradient through the `talosctl` the harness
already probes them with. A `talos-upgrade` proposal names a node and a release
tag — never an image: at run time the executor reads the node's own
`machine.install.image` (the one field it takes from the machine config), swaps
the tag, checks the node is ready and not already there, issues `talosctl
upgrade`, and waits for the node to leave and come back on the new tag and
ready (a node that never returns is a failed receipt, after 15 minutes). The
undo is Talos's own: the previous install stays in the other boot partition, so
the `prior-image` rollback verifier reads the node's current release before
each run and `helper exec rollback` boots it again with `talosctl rollback`.
That verified inverse is why `talos-upgrade` is the first node action that can
earn AUTONOMOUS. Draft one over MCP with `propose_talos_upgrade`.

Kubernetes itself moves with `k8s-upgrade` (Phase 11.2): `talosctl upgrade-k8s
--to <version>` from one control-plane node, always at blast radius `cluster`
(a proposal claiming a narrower one is refused). Preflight is read-only — the
cluster is healthy, every member runs the same Talos release, the target is
newer and at most one minor ahead — and the receipt only says succeeded once
the kubelet reports the new version and the cluster is healthy again. There is
no inverse, so like `node-update` it is pinned at CONFIRM. Draft one with
`propose_k8s_upgrade`.

The whole cluster moves as one maintenance run (Phase 11.3): `helper
maintenance plan-talos <control-plane> --to v1.14.3 [--kubernetes 1.37.2]`
reads the members and drafts a `talos-upgrade` per node — control planes
first, then workers, nodes already on the target skipped — and Kubernetes
last; `helper maintenance run <id>` walks them after one consent, asking
`talosctl health` before every step so a node that came back wrong stops the
rest. Over MCP: `plan_talos_maintenance_run`, then `execute_maintenance_run`.

### Storage efficiency, service suggestions, the weekly digest (Phase 8.5–8.7)

`helper discover storage` reads every Proxmox storage and guest config and
records `storage-efficiency` findings: pools trending toward full (time-to-full
from the usage history), stale snapshots, orphaned disks and unreferenced
images. `helper discover suggestions` compares what your hardware offers with
what runs on it — an idle GPU, a missing building block the library knows
(a local LLM when the chat router prefers one, say) — as `info` findings.
`helper digest show` renders the week: what executed and how it ended, which
findings opened and resolved, what changed in the trust gradient; `helper
digest send` delivers it through the same phone notification service and moves
the window.

### Status endpoint (Phase 9.7a)

`helper status show` is the one-screen answer to "is anything wrong, and is
anything waiting on me?" — open findings by severity, proposals awaiting
approval, discovery age, trust cells, the day's receipts, and a three-step
`health`. `helper status serve` exposes the same object as `GET /status` (plus
`GET /healthz`) for a Homepage `customapi` tile or a Home Assistant REST sensor;
set `HOMELAB_HELPER_STATUS_TOKEN` when the LAN is not the boundary. Two GET
routes, nothing that changes anything — see
[the docs](https://moellere.github.io/homelab-helper/status-endpoint/).

### After the fact

Every run you were *not* asked about tells you it happened: an AUTONOMOUS
execution, a failed dispatch at any level, or an outcome that moved a cell's
floor (auto-promotion, demotion) posts a plain notification to the same
`notify.<phone>` service the approval channel uses — what ran, the outcome,
the `helper exec rollback <receipt>` one-liner when the receipt holds enough
state to undo it. A confirmed success stays quiet; you just tapped Approve.
The notification goes out after the receipt is written and is best-effort: a
phone that cannot be reached changes nothing about the run or its record.

### Per-probe cadences (Phase 9.6)

One discovery interval for everything was the shortcut; "continuous" means each
probe on its own cadence. Declare them in a YAML file (copy
`fixtures/schedule.example.yaml`, point `HOMELAB_HELPER_SCHEDULE` at it): a RAID
check every 15 minutes, SMART once a day, everything else at a default, and an
interval per assertion. `helper daemon run` then runs whatever is due, and
`helper schedule` shows what runs when. Nothing new is stored — a probe is due
when its last recorded run is older than its interval — so the same file works
from a 15-minute cron tick (`--once`) and from the long-lived daemon alike.

### Proactive mode (Phase 7)

`helper daemon run` closes the loop without anyone asking: discovery on a
cadence (findings land in the harness DB), then **playbooks** turn the findings
they cover into pending proposals, then the **listener** sends the ones policy
would allow to your phone and executes on a tap. The playbooks:

| Finding | Playbook | Proposal |
|---|---|---|
| `drift-candidate` (Argo CD reports an app out of sync or unhealthy) | `argocd-resync` | `argocd-sync` of that application |
| `workload-unhealthy` (a settled Deployment / StatefulSet / DaemonSet has fewer ready replicas than desired) | `workload-restart` | `workload-restart` of that workload |
| `rightsizing` (cores or memory recommendation from usage history) | `rightsize` | `resize` of that guest; never redrafted once applied |
| `version-drift` (a Proxmox node with pending package updates) | `node-update` | `node-update` of that node, one at a time |

Playbooks are a deterministic table, not a model: a finding's own fields pick
the action; four playbooks ship today. A finding must have persisted 15 minutes first (the platform's
own self-heal gets first go); one live proposal per finding; a six-hour
cooldown after any decision so a fix that did not clear the finding is not
retried every pass; and a draft whose finding resolves is withdrawn.
The listener sends every prompt at once — each titled with what it would do
("Resize proxmox-dc: cores 2 → 1"), with one line of why and when it takes
effect, on a high-importance "homelab-helper approvals" channel, sticky until
answered, and cleared from the phone once answered or expired (15 minutes by
default, `HOMELAB_HELPER_APPROVAL_TIMEOUT_S`). Approve or Deny is final; a
prompt that simply expired is asked again two hours later, three times at most,
because a missed notification is not a "no". It never asks about cells still at
PROPOSE and leaves hand-authored proposals alone. With every cell at its
default, the daemon only ever writes rows.

### Using with Claude / MCP

The harness ships a native **MCP server** (the first Phase-4 deliverable) that
exposes its query surface as tools for Claude Desktop / Claude Code / Cursor:
queries (`list_hosts`, `get_host`, `list_findings`, `get_finding`,
`list_services`, `get_service`, `audit_summary`, `config_status`), the
findings lifecycle (`ack_finding`, `resolve_finding`, `suppress_finding` —
harness-DB writes only), `run_discovery` over the management-plane sources
(UniFi, MikroTik, Cloudflare, Argo CD, Proxmox, K8s, OMV, Home Assistant), `probe_host`
(SSH deep discovery; key path or env reference only — no secrets through tool
arguments) and `probe_talos` (the Talos machine API), and the Phase-5 planners
as deterministic reports (`list_workloads`, `recommend_placement`,
`plan_rebalance`, `analyze_bottlenecks`, `analyze_surplus`, `network_path`,
`rightsizing`, `usage_summary`) that the client's own model narrates, and
`retire_host` for the inventory itself. Nothing writes to the lab itself.

```bash
helper mcp tools    # list the tool roster
helper mcp serve    # stdio server (launched by a client)

# Register with Claude Code:
claude mcp add homelab -- helper mcp serve
# from a checkout instead:
# claude mcp add homelab -- uv run --directory /path/to/homelab-helper helper mcp serve
```

For Claude Desktop, add the same command under `mcpServers` in its config.
Nothing the server can do writes to the lab: tools read the harness DB, run
read-only discovery, or draft proposals for you to act on. Source credentials
come from the same `HOMELAB_HELPER_*` env vars as the CLI.

`probe_host` is scoped, because it authenticates with your SSH key: it will
probe a host the harness already knows, at that host's recorded address, and
refuse anything else. To let an MCP client onboard hosts it hasn't seen, set
`HOMELAB_HELPER_MCP_PROBE_ALLOW` to comma-separated hostname/IP globs
(`"*.lan,10.0.1.*"`); an unknown host's name, and its `primary_ip` when given,
must both match. Otherwise add hosts from the CLI (`helper discover host`,
`helper onboard`) and let the agent probe them from there.

**An agent may draft and trigger, never authorize.** `trust_status`,
`list_receipts` and `pending_actions` let a model see the gradient — which
cells are granted, what has executed, what policy would say about each pending
action — and give it no way to change any of it. `propose_action` drafts a
Proxmox guest action (start/stop/shutdown/restart, migrate with a
`target_node`, or a QEMU `cpu_type`), `propose_workload_action` a Kubernetes
one (rollout restart or scale), `propose_argocd_sync` an Argo CD sync, and
`propose_dns_record` a UniFi static-DNS upsert, all as *pending* proposals
validated against the manifest schema and returned with the policy preview. `list_proposals` and `get_proposal` read
them back.

`draft_remediations` runs the playbooks once (harness-DB write, nothing
executes). `execute_proposal` (Phase 7) is the one trigger: it hands a pending proposal
to the same executor `helper exec run` uses, with no override, so the outcome
is still `decide()`'s. AUTONOMOUS runs; CONFIRM sends you a Home Assistant
actionable notification with **Approve** / **Deny** (set
`HOMELAB_HELPER_APPROVAL_NOTIFY_SERVICE`) and waits for your tap, which is
recorded on the audit spine with the channel and device; PROPOSE and BLOCK
execute nothing and return the policy reason plus the CLI command. With every
cell at its default, an agent calling `execute_proposal` changes nothing.

There is no MCP tool that grants a cell, opens an elevation window, overrides
a floor, or rolls back; those are operator gestures at the CLI, and tests
enforce the absence — and that `execute_proposal` can never carry an override
— rather than trusting the convention. Previews are reported pessimistically
(as if reversibility were unverified), because verifying it means probing the
target and a query tool has no business doing that.

**Transport and trust.** The MCP server speaks stdio only. It runs as you, in
your shell, and reads the same `.env` the CLI does, so put nothing secret in
the client's config block: the command line above is all a client needs. The
only network listener the harness has is the read-only status endpoint above.
Remote MCP would need token auth, TLS and per-token tool allowlists (a remote
client should never see `probe_host`), and is not planned for 1.0.

## Repo layout

```
.
├── docs/                           # The mkdocs site (moellere.github.io/homelab-helper): getting started,
│                                   # CLI reference, trust gradient, stability, status endpoint, runbook,
│                                   # architecture, roadmap, backlog, releasing
├── fixtures/                       # Operator-editable examples: assertion library, topology, aliases, schedule
├── src/homelab_helper/
│   ├── adapters/                   # NetBox, Kernel-SSH, Talos, Proxmox, K8s, UniFi, MikroTik, Cloudflare, Argo CD, OMV, Home Assistant
│   ├── probes/                     # Probe plugin SDK + first-party host/network/talos probes
│   ├── engine/                     # Reconciler, assertions, planners, trust gate, executor, rollback
│   ├── llm/                        # LLM router + backends, chat context, narrator/planner/discovery agents
│   ├── db/                         # Models, enums, async session
│   ├── migrations/                 # Alembic env + versions (ship in the wheel)
│   ├── data/                       # Workload library, OS/Ceph/Kubernetes/Talos EOL table, replayable labs (ship in the wheel)
│   ├── cli/                        # `helper` Typer app
│   ├── mcp_server.py               # MCP tools over stdio
│   ├── status_api.py               # GET /status + /healthz (FastAPI)
│   ├── config.py                   # .env loading, per-user dirs, source status
│   ├── secrets.py                  # Secret references (file/age/sops/keyring) + redaction
│   └── api/                        # HTTP API — a stub; not part of the product
├── tests/                          # pytest suite (~1200 tests, no live infrastructure; runs on SQLite and Postgres)
└── .github/workflows/              # CI gate on every PR (3.12, 3.13, Postgres, docs); tag-driven release; docs site
```

## Reporting issues

Open a GitHub issue with the output of `helper version` and `helper config`
(secrets show only as set/unset, never as values) and the command that
misbehaved. For anything that looks like a credential or authorization
problem, see [`SECURITY.md`](./SECURITY.md) instead of filing it publicly.

## Development

This project uses [uv](https://docs.astral.sh/uv/) for environment and dependency
management. Install uv first if you don't have it.

```bash
uv sync --all-extras --group dev --group docs   # creates .venv with every extra, the dev and docs groups
uv run helper --help                            # the CLI from the checkout

# The CI gate — run all five before pushing
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest -q
uv run mkdocs build --strict
# the same suite against Postgres: HELPER_TEST_DATABASE_URL=postgresql+asyncpg://... uv run pytest -q
```

Tests never touch live infrastructure: adapters run against `httpx`
`MockTransport`, probes against loopback servers, and the CLI against a
temporary SQLite file. `uv run pre-commit install` wires the same checks into
your commits. Pull requests target `main` and are squash-merged; releases are
cut from tags (see [`releasing.md`](./docs/releasing.md)).

## License

Apache License 2.0 — see [`LICENSE`](./LICENSE).

## Contributing

See `CONTRIBUTING.md`: the five checks, the invariants a change must not
break, and the shape of an action-kind contribution.
