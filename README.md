# homelab-helper

> An OSS framework for homelab planning, inventory, audit, and (eventually) recommendations.

**Status:** Pre-alpha, run-from-source. The read-only product (Phases 1–5:
inventory, audit, chat, placement/rebalancing recommendations) is built and
under live-fleet validation; Phase 6 (gated execution behind the trust
gradient) is in design. APIs and schemas are still unstable.

`homelab-helper` is a framework for people who run their own infrastructure at home.
It discovers what you have, maintains a coherent inventory across the many sources
of truth a real homelab spans (NetBox, Proxmox, Kubernetes, UniFi, Cloudflare,
kernel probes), surfaces drift and gaps as auditable findings, and proposes
changes. Phases 1–5 are strictly "propose, never apply" (L1) — and remain a
complete product for anyone who never opts in. Phase 6 (L2) adds execution,
but only behind the **trust gradient**: a deterministic, operator-controlled
authorization model that the framework can never escalate on its own.

See [`architecture.md`](./docs/architecture.md) for the architecture document,
[`roadmap.md`](./docs/roadmap.md) for the phased delivery plan, and
[`backlog.md`](./docs/backlog.md) for the concrete task list of what's left.

## What it does today

- Scan a network and fingerprint live hosts
- Deep-probe Linux hosts over SSH (CPU, memory, storage, network, PCI, GPU, services)
- Maintain part-level identity that survives moves (DIMMs, SSDs, NICs)
- Push inventory into NetBox via its API
- Run configuration assertions and produce reconciliation findings
- Produce a day-one audit against a real homelab

## Running it locally

> Pre-alpha, run-from-source. There's no packaged release or server to deploy
> yet (the HTTP API and agent layers are later phases). "Running it locally"
> means installing from source, initializing a local SQLite database, and
> driving the `helper` CLI. Everything is **read-only (L1) — it proposes, never
> applies.**

**1. Install** (see [Development](#development) for the full toolchain):

```bash
uv sync --all-extras --group dev
```

**2. Initialize the database** — a SQLite file `./homelab.db` by default
(override with `HOMELAB_HELPER_DATABASE_URL`; a `postgres` extra is available
for Postgres):

```bash
uv run helper db init      # alembic upgrade + register entry-point probes
uv run helper db status
# uv run helper db reset --yes   # DESTRUCTIVE — dev only
```

**3. Configure source credentials.** A `.env` at the repo root (gitignored)
is auto-loaded, then `~/.env`; explicit exports always win. Each source only
needs its variables when you run that `discover` verb (all are prefixed
`HOMELAB_HELPER_`):

| Source | Variables |
|---|---|
| Database | `DATABASE_URL` (default local SQLite) |
| UniFi | `UNIFI_URL`, `UNIFI_API_KEY`, `UNIFI_SITE`, `UNIFI_VERIFY_SSL` |
| Cloudflare | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE` (or `CLOUDFLARE_ZONE_ID`) |
| Argo CD | `ARGOCD_URL`, `ARGOCD_API_TOKEN`, `ARGOCD_VERIFY_SSL` |
| Proxmox | `PROXMOX_URL`, `PROXMOX_TOKEN_ID`, `PROXMOX_TOKEN_SECRET`, `PROXMOX_VERIFY_SSL` |
| Kubernetes | `KUBECONFIG`, `KUBE_CONTEXT` |
| OpenMediaVault | `OMV_URL`, `OMV_USERNAME`, `OMV_PASSWORD`, `OMV_VERIFY_SSL` |
| NetBox | `NETBOX_URL`, `NETBOX_TOKEN`, `NETBOX_VERIFY_SSL` |
| LLM (chat) | `LLM_PRIVACY` (`strict-local`/`prefer-local`/`open`), `OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_TIER`; BYOK: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENAI_COMPAT_BASE_URL` |

Print the effective configuration (secrets shown only as set/unset, never
printed):

```bash
uv run helper config
```

**4. Run.** Discovery is read-only; add `--persist` to write to the DB and
`--dry-run` to preview:

```bash
uv run helper --help

uv run helper discover host <name> --ssh-user <u> --ssh-key <path>
uv run helper discover unifi --persist
uv run helper discover cloudflare --persist
uv run helper discover argocd
uv run helper discover proxmox --persist
uv run helper discover omv             # OpenMediaVault NAS: filesystems, disks, shares, services

uv run helper view service <name>      # internal/external endpoints + DNS split-brain
uv run helper view host <name>         # guests, endpoints, findings
uv run helper diff git-vs-cluster --persist   # Argo CD drift → DRIFT_CANDIDATE findings
uv run helper audit
uv run helper findings list
```

Keep tokens and keys in the environment — never commit them.

### Chat with your lab

`helper chat` answers questions from the reconciled inventory — grounded in
facts, never inventing hosts or findings. Runs against local **Ollama by
default** (`localhost:11434`); add a BYOK cloud key to enable fallback, and
control routing with `HOMELAB_HELPER_LLM_PRIVACY` (`strict-local` never sends
anything to a cloud model — the router refuses rather than silently
downgrading or leaking).

```bash
uv run helper chat "what hosts do I have?"     # one-shot
uv run helper chat                             # REPL ('exit' to leave)
uv run helper findings narrate                 # open findings as prose
uv run helper onboard "a new mini-PC"          # conversational host onboarding
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
uv run helper plan path node0 wyhome --workload ceph-osd   # path verdict
uv run helper plan workloads                    # browse the library
uv run helper plan add-workload immich          # ranked hosts + reasons
uv run helper plan add-workload immich --narrate
uv run helper plan rebalance --narrate     # 3 candidate plans with tradeoffs
uv run helper bottlenecks --persist        # known patterns → findings + mitigations
uv run helper plan surplus                 # idle capacity → reconfiguration options
```

As you chat, a **skill profile** builds passively (deterministic keyword
inference, no extra LLM calls): `helper skills` shows it, `helper skills set
storage advanced` pins a domain so inference can't change it. The profile
tunes how much chat explains — and later feeds per-domain trust hints.

Every reply is footed with the backend that served it, e.g.
`[ollama: llama3.2 (small, local)]`.

### Using with Claude / MCP

The harness ships a native **MCP server** (the first Phase-4 deliverable) that
exposes its query surface as tools for Claude Desktop / Claude Code / Cursor:
queries (`list_hosts`, `get_host`, `list_findings`, `get_finding`,
`list_services`, `get_service`, `audit_summary`, `config_status`), the
findings lifecycle (`ack_finding`, `resolve_finding`, `suppress_finding` —
harness-DB writes only), `run_discovery` over the management-plane sources,
and `probe_host` (SSH deep discovery; key path or env reference only — no
secrets through tool arguments). Nothing writes to the lab itself.

```bash
uv run helper mcp tools    # list the tool roster
uv run helper mcp serve    # stdio server (launched by a client)

# Register with Claude Code:
claude mcp add homelab -- uv run --directory /path/to/homelab-helper helper mcp serve
```

For Claude Desktop, add the same command under `mcpServers` in its config.
Everything the server can do stays within the read-only L1 stance; source
credentials come from the same `HOMELAB_HELPER_*` env vars as the CLI.

`probe_host` is scoped, because it authenticates with your SSH key: it will
probe a host the harness already knows, at that host's recorded address, and
refuse anything else. To let an MCP client onboard hosts it hasn't seen, set
`HOMELAB_HELPER_MCP_PROBE_ALLOW` to comma-separated hostname/IP globs
(`"*.lan,10.0.1.*"`); an unknown host's name, and its `primary_ip` when given,
must both match. Otherwise add hosts from the CLI (`helper discover host`,
`helper onboard`) and let the agent probe them from there.

## Repo layout

```
.
├── docs/architecture.md            # System architecture (locked decisions)
├── docs/roadmap.md                 # Phased delivery plan
├── docs/backlog.md                 # Concrete task list (Phase 1 gaps + Phase 6)
├── docs/harness-schema-slice1.md   # Slice 1 DB schema spec (11 tables) + trust-gradient forward spec
├── docs/netbox-modeling-walkthrough.md  # NetBox modeling decisions for the example lab
├── pyproject.toml                  # uv-managed project metadata
├── src/homelab_helper/             # The package
│   ├── db/                         # Models, enums, session
│   ├── adapters/                   # NetBox, Kernel-SSH (P1); Proxmox, K8s, UniFi, ... (P3+)
│   ├── probes/                     # Probe plugin SDK + first-party probes
│   ├── engine/                     # Reconciler, assertion engine, scheduler, fingerprint
│   ├── migrations/                 # Alembic env + versions (ship in the wheel)
│   ├── cli/                        # `helper` Typer app
│   ├── api/                        # FastAPI HTTP API (P1+)
│   └── agent/                      # helper-agent daemon (P2)
└── tests/                          # pytest suite
```

## Development

This project uses [uv](https://docs.astral.sh/uv/) for environment and dependency
management. Install uv first if you don't have it.

```bash
# Sync dev environment (creates .venv, installs all deps including dev group)
uv sync --all-extras --group dev

# Run the test suite
uv run pytest

# Run the CLI
uv run helper --help

# Format + lint
uv run ruff format .
uv run ruff check . --fix

# Typecheck
uv run mypy src
```

### Pre-commit

```bash
uv run pre-commit install
```

## License

Apache License 2.0 — see [`LICENSE`](./LICENSE).
