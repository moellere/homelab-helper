# Live-fleet validation — the sign-off gate

Everything in this repo is verified against mocks: `httpx.MockTransport` for
adapters, in-memory SQLite for the engine, loopback servers for the network
probes. That is deliberate — the suite must run anywhere, and it must never
touch someone's lab. It also means **no Phase 4, 5 or 6 acceptance criterion
has yet met real infrastructure.**

This runbook is that missing step. It runs on the operator's machine, inside
the lab's network, against real credentials. It cannot run in CI, and it
cannot run from a cloud agent session — that is the point.

Two parts, in order:

1. **Phases 4–5** — read-only and advisory. Nothing here changes the lab.
2. **Phase 6** — the first real execution, against a guest created to be
   destroyed. Do not start it until part 1 passes.

---

## Before you start

```bash
uv sync --all-extras --group dev
uv run helper config          # every source you intend to test should read "ready"
```

Use a **scratch database** so a validation run never mixes into your real
inventory, and so you can throw the whole thing away:

```bash
export HOMELAB_HELPER_DATABASE_URL="sqlite+aiosqlite:///$HOME/.homelab-validation.db"
export HOMELAB_HELPER_OPERATOR="$USER"
uv run helper db init
```

Record results as you go. A criterion that "sort of worked" is a fail — the
point of a gate is that it can stop things.

---

## Part 1 — Phases 4 & 5 (read-only)

### P4-AC1 · chat reaches the local model

```bash
uv run helper chat "what hosts do I have?"
```

**Pass:** answers from your actual inventory, footer shows the Ollama backend.
**Fail if:** it invents hosts, or silently uses a cloud backend.

### P4-AC2 · Ceph narration

```bash
uv run helper bottlenecks
uv run helper chat "what's wrong with my ceph cluster?"
```

**Pass:** cites the `CEPH_BOTTLENECK` finding, explains the 1 GbE / 2.5 GbE
asymmetry, lists the four candidate mitigations.
**Fail if:** the mitigations are generic advice rather than derived from your
topology.

### P4-AC3 · conversational onboarding

```bash
uv run helper onboard
```

Walk through a host the harness has never seen.

**Pass:** interview → confirm → registered, then warm discovery runs and the
host appears in `helper host show`.

### P4-AC4 · MCP-driven discovery

Register the server in Claude Code, then ask it to discover a known host.

```bash
claude mcp add homelab -- uv run --directory "$PWD" helper mcp serve
```

**Pass:** the client calls the right tools and results land in the harness DB
(and NetBox, if you run the sync).
**Note:** `probe_host` refuses hosts the harness doesn't already know unless
`HOMELAB_HELPER_MCP_PROBE_ALLOW` is set. A refusal here is correct behaviour,
not a failure.

### P4-AC5 · strict-local refuses cleanly

```bash
uv run helper chat --privacy strict-local "design me a migration plan for my whole lab"
```

**Pass:** a clean refusal naming the required capability tier, your policy, and
the options. **Fail if:** it silently answers with a weaker local model — the
router must never downgrade quality without saying so.

### P4-AC6 · skill profile moves on its own

After several sessions talking about ZFS, Ceph and Kubernetes:

```bash
uv run helper skills show
```

**Pass:** levels reflect the conversations without you setting them by hand.

### P5-AC1 · workload library

```bash
uv run helper plan workloads | tail -3
```

**Pass:** ≥50 entries.

### P5-AC2 · placement reasoning

```bash
uv run helper plan add-workload immich
```

**Pass:** names a host *and* its reasoning — arch constraint, RAM headroom, GPU
optionality, storage proximity.

### P5-AC3 · rebalance candidates

```bash
uv run helper plan rebalance
```

**Pass:** at least three plans with distinct tradeoffs (current hardware / one
move / one purchase). **Fail if:** any plan oscillates — a guest moving back
and forth between hosts is the bug this AC exists to catch.

### P5-AC4 · Ceph mitigations are generated

Compare `helper bottlenecks` output against the day-one report's four
mitigations (CRUSH reweight, USB 2.5GbE, OSD relocate, accept).

**Pass:** the framework derives them from your topology.
**Fail if:** they look hardcoded — change a link speed in the topology fixture
and confirm the recommendation changes with it.

### P5-AC5 · surplus reasoning

```bash
uv run helper plan surplus
```

**Pass:** flags the node with real surplus and proposes the options (restart
the stopped guests, move the DIMMs, or accept it as reserve).

### P5-AC6 · the VPN path is refused

```bash
uv run helper plan path <wyola-host> <covington-host>
```

**Pass:** refuses to place Ceph-replicated work across the link and explains
that the path inherits its worst hop's latency and reliability.

---

## Part 2 — Phase 6, first real execution

Stop here unless part 1 passed.

### Step 0 — fence the things that must never be touched

**Do this before granting anything.** An absolute boundary is the one control
no window and no override can cross, so it should exist before the first
execution, not after the first scare:

```bash
uv run helper trust boundary <your-nas>  propose --absolute --notes "never automate"
uv run helper trust boundary <your-router> propose --absolute --notes "never automate"
uv run helper trust show      # confirm they read "absolute (window-proof)"
```

### Step 1 — a guest you are willing to lose

Create a throwaway LXC on a non-critical node. Note its node, vmid, and that
it is running. Nothing below should ever name a guest you care about.

### Step 2 — confirm the floor holds

Create a pending action proposal for that guest (restart, `single-host`), then:

```bash
uv run helper exec list
uv run helper exec run <id>
```

**Pass:** refused at `propose`, with the reason trace. Check your Proxmox task
log: **there should be no API call at all** — a refused action must not touch
the target even to probe it.

### Step 3 — grant one cell, execute once

```bash
uv run helper trust grant containers restart single-host confirm
uv run helper exec run <id>
```

**Pass:** prompts with the cell and reason trace; on consent the guest actually
restarts; `helper exec receipts` shows one `succeeded` receipt whose
`rollback` column names the strategy, and whose captured prior state says the
guest was running.

### Step 4 — undo it

```bash
uv run helper exec rollback <receipt-id>
```

**Pass:** the guest is driven back to its captured state, a second receipt
records the undo, and the original shows `rolled back`.

### Step 5 — a failure demotes the cell

Point a proposal at a vmid that does not exist and run it.

**Pass:** a `failed` receipt is written, the proposal stays pending, and
`helper trust show` shows the cell dropped to `propose` with `(probation)`.
Only an explicit re-grant clears it — confirm that too.

### Step 6 — the kill switch stops work in flight

```bash
uv run helper window open --reason "validation" --minutes 10 --host <node>
uv run helper exec run <id>        # at the confirm prompt, STOP
# in a second terminal:
uv run helper window kill --yes
# now answer the first prompt
```

**Pass:** the run refuses with "elevation window … closed before dispatch —
halting", and nothing was dispatched.

### Step 7 — clean up

```bash
uv run helper window kill --yes
uv run helper trust show          # revoke grants you don't want to keep
```

Destroy the throwaway guest. Keep the scratch database if you want the
receipts as evidence; delete it otherwise.

---

## Sign-off

| Criterion | Result | Notes |
|---|---|---|
| P4-AC1 … P4-AC6 | | |
| P5-AC1 … P5-AC6 | | |
| P6 steps 0–7 | | |

Until this table is filled in, `backlog.md` should keep listing live-fleet
validation as outstanding, and no Phase-6 execution path should run against
infrastructure you are not prepared to lose.
