# homelab-helper

> An open-source framework for homelab inventory, audit, recommendations, and
> operator-gated execution.

**Status: beta.** The read-only product — discovery, inventory, audit, chat,
MCP tools, placement and rebalancing recommendations — is complete, installs
from PyPI, and has been run against a real multi-site lab. Execution (Phase 6,
widened in Phase 7 to guest migration, Kubernetes workloads, and agent-triggered
runs behind a phone-tap approval) is built and tested but **opt-in and off by
default**: nothing runs until you
raise a trust cell, and you should validate it against your own fleet before
you do. Expect the CLI verbs, MCP tool names, and configuration variables to
stay stable through the 0.1 series; database schema changes ship as Alembic
migrations that `helper db init` applies.

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
- Recommend placement, rebalancing, and reconfiguration, and flag known bottleneck patterns
- Execute a proposed guest power action only after you raise its trust cell — deterministic gate, receipts, snapshots, rollback, elevation windows, kill switch

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
uv tool install --prerelease allow homelab-helper   # from PyPI (beta: installers skip pre-releases unless told)
# or: pipx install homelab-helper==0.1.0b3
helper --install-completion                          # bash / zsh / fish
# bleeding edge: uv tool install git+https://github.com/moellere/homelab-helper
```

Drop `--prerelease allow` / the version pin once a non-beta `0.1.0` is on
PyPI; until then a plain `uv tool install homelab-helper` reports no matching
version.

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
extra is available.

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

```bash
helper trust show                                   # every cell sits at PROPOSE by default
helper trust grant hypervisor restart single-host confirm
helper trust grant hypervisor migrate single-host confirm
helper trust grant containers workload-restart single-service confirm
helper trust grant containers argocd-sync single-service confirm
helper trust grant dns dns-record single-service confirm
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
within 180 days of end of support (dates live in `data/os-eol.yaml`, nothing is
guessed), Kubernetes/Talos version skew, and pending Home Assistant updates. A
source that cannot be reached is reported and its findings are left alone.

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

### After the fact

Every run you were *not* asked about tells you it happened: an AUTONOMOUS
execution, a failed dispatch at any level, or an outcome that moved a cell's
floor (auto-promotion, demotion) posts a plain notification to the same
`notify.<phone>` service the approval channel uses — what ran, the outcome,
the `helper exec rollback <receipt>` one-liner when the receipt holds enough
state to undo it. A confirmed success stays quiet; you just tapped Approve.
The notification goes out after the receipt is written and is best-effort: a
phone that cannot be reached changes nothing about the run or its record.

### Proactive mode (Phase 7)

`helper daemon run` closes the loop without anyone asking: discovery on a
cadence (findings land in the harness DB), then **playbooks** turn the findings
they cover into pending proposals, then the **listener** sends the ones policy
would allow to your phone and executes on a tap. Two playbooks ship:

| Finding | Playbook | Proposal |
|---|---|---|
| `drift-candidate` (Argo CD reports an app out of sync or unhealthy) | `argocd-resync` | `argocd-sync` of that application |
| `workload-unhealthy` (a settled Deployment / StatefulSet / DaemonSet has fewer ready replicas than desired) | `workload-restart` | `workload-restart` of that workload |

Playbooks are a deterministic table, not a model: a finding's own fields pick
the action; a finding must have persisted 15 minutes first (the platform's
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
`plan_rebalance`, `analyze_bottlenecks`, `analyze_surplus`, `network_path`)
that the client's own model narrates. Nothing writes to the lab itself.

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

**Transport and trust.** The server speaks stdio only. It runs as you, in
your shell, and reads the same `.env` the CLI does, so put nothing secret in
the client's config block: the command line above is all a client needs.
There is no network transport. Remote MCP would need the HTTP API (a stub
today) with token auth and TLS and per-token tool allowlists (a remote client
should never see `probe_host`); neither is planned before live-fleet
validation signs Phase 6 off.

## Repo layout

```
.
├── docs/architecture.md            # System design and locked decisions, incl. the trust gradient
├── docs/roadmap.md                 # Phased delivery plan
├── docs/backlog.md                 # What's done and what's left, per phase
├── docs/agent-access-scope.md      # How agents reach each service, and what stays operator-only
├── docs/harness-schema-slice1.md   # DB schema spec + trust-gradient tables
├── docs/releasing.md               # Tag-driven releases to PyPI
├── fixtures/                       # Operator-editable examples: assertion library, topology, example lab
├── src/homelab_helper/
│   ├── adapters/                   # NetBox, Kernel-SSH, Talos, Proxmox, K8s, UniFi, MikroTik, Cloudflare, Argo CD, OMV, Home Assistant
│   ├── probes/                     # Probe plugin SDK + first-party host/network/talos probes
│   ├── engine/                     # Reconciler, assertions, planners, trust gate, executor, rollback
│   ├── llm/                        # LLM router + backends, chat context, narrator/planner/discovery agents
│   ├── db/                         # Models, enums, async session
│   ├── migrations/                 # Alembic env + versions (ship in the wheel)
│   ├── data/                       # Starter workload library (ships in the wheel)
│   ├── cli/                        # `helper` Typer app
│   ├── mcp_server.py               # MCP tools over stdio
│   ├── config.py                   # .env loading, per-user dirs, source status
│   ├── secrets.py                  # Secret references (file/age/sops/keyring) + redaction
│   └── api/                        # HTTP API — a stub; not part of the 0.1 product
├── tests/                          # pytest suite (~880 tests, no live infrastructure)
└── .github/workflows/              # CI gate on every PR; tag-driven release
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
uv sync --all-extras --group dev   # creates .venv with every extra and the dev group
uv run helper --help               # the CLI from the checkout

# The CI gate — run all four before pushing
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest -q
```

Tests never touch live infrastructure: adapters run against `httpx`
`MockTransport`, probes against loopback servers, and the CLI against a
temporary SQLite file. `uv run pre-commit install` wires the same checks into
your commits. Pull requests target `main` and are squash-merged; releases are
cut from tags (see [`releasing.md`](./docs/releasing.md)).

## License

Apache License 2.0 — see [`LICENSE`](./LICENSE).

## Contributing

See `CONTRIBUTING.md`: the four checks, the three invariants a change must not
break, and the shape of a Phase-7 action-kind contribution.
