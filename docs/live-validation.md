# Live-fleet validation — the sign-off gate

Everything in this repo is verified against mocks: `httpx.MockTransport` for
adapters, in-memory SQLite for the engine, loopback servers for the network
probes. That is deliberate — the suite must run anywhere, and it must never
touch someone's lab. It also means **no Phase 4, 5 or 6 acceptance criterion
has yet met real infrastructure.**

This runbook is that missing step. Almost all of it runs on the operator's
machine, inside the lab's network, against real credentials: it cannot run in
CI, and it cannot run from a cloud agent session — that is the point.

The exception is worth naming, because it is not a loophole. A criterion that
this particular fleet's *shape* hides — a cluster with no link asymmetry has no
asymmetry to report — can never be exercised here no matter how much real
hardware it meets. For those, a committed fixture supplies the shape and the
derivation runs through the same verbs (see P4-AC2). That is validation of the
derivation, not of the lab, and the table says which it got.

Four parts, in order:

1. **Phases 4–5** — read-only and advisory. Nothing here changes the lab.
2. **Phase 6** — the first real execution, against a guest created to be
   destroyed. Do not start it until part 1 passes.
3. **Phase 7** — an agent drafts, the daemon asks, you tap a phone.
4. **Phase 8** — a rolling node update. It has no inverse; read it first.

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

> A fleet whose cluster nodes all share one link speed has no asymmetry to
> report, so `helper bottlenecks` is correctly silent and this criterion cannot
> be exercised against such a fleet at all. **Use the asymmetric fixture
> instead** (below): it supplies the topology your lab does not have, so the
> derivation runs through the same two verbs you would use live.

```bash
uv run helper bottlenecks                            # against your own fleet
uv run helper chat "what's wrong with my ceph cluster?"
```

**Pass:** cites the `CEPH_BOTTLENECK` finding, explains the asymmetry in your
own speeds, lists the four candidate mitigations.
**Fail if:** the mitigations are generic advice rather than derived from your
topology.

#### The asymmetric fixture — when your own fleet is symmetric

`fixtures/asymmetric-lab.yaml` is a three-node cluster with one node left at
1 GbE while the others run at 2.5 GbE. It needs no hardware, no credentials and
no SSH, so it runs in CI and in a cloud session as well as on your machine. Use
a throwaway database — this seeds synthetic hosts:

```bash
export HOMELAB_HELPER_DATABASE_URL="sqlite+aiosqlite:///$HOME/.homelab-asym.db"
uv run helper db init
uv run helper discover replay fixtures/asymmetric-lab.yaml
uv run helper bottlenecks
```

**Pass:** one `high` hit, `cluster-link-asymmetry` on cluster `ceph-lab`, naming
`ceph-c` at 1000 Mbps against `ceph-a`/`ceph-b` at 2500, and four mitigations —
CRUSH-reweight away from `ceph-c`, bring it to 2500 Mbps, relocate its OSDs to
`ceph-a`, or accept it as a cold tier.

The deterministic half of this is pinned by
`tests/test_lab_replay_asymmetric.py`, including the anti-hardcoding check:
change `ceph-c`'s `speed_mbps` and the recommendation moves with it; set it to
2500 and the analyser goes quiet. What the tests cannot cover is the narration
itself, so run that once against your own router:

```bash
uv run helper bottlenecks --narrate
```

**Pass:** the prose cites `ceph-c` and the measured speeds rather than
describing a hypothetical cluster. Then unset the scratch database — the
synthetic hosts must not reach your real inventory.

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
uv run helper skills
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
**Fail if:** they look hardcoded — change a link speed and confirm the
recommendation changes with it.

On a symmetric fleet this is the same situation as P4-AC2: there is nothing to
generate. The asymmetric fixture under P4-AC2 covers it, and the
change-a-speed-and-watch-it-move check is a test rather than a manual step.

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

## Part 3 — Phase 7, an agent triggers and you tap

Needs Part 2 signed off, a phone with the Home Assistant companion app, and
the throwaway guest from Part 2 still around (or a second one).

### Step 0 — point the approval channel at your phone

```bash
export HOMELAB_HELPER_APPROVAL_NOTIFY_SERVICE=notify.mobile_app_<your phone>   # from HA's notify services
export HOMELAB_HELPER_APPROVAL_TIMEOUT_S=300
uv run helper config                 # Home Assistant URL/token must be configured too
```

### Step 1 — the trigger does nothing at the floor

From an MCP client with the `homelab` server, or the Python REPL:

```
propose_action("migrate", node="<src>", vmid=<throwaway>, vm_kind="qemu",
               title="validate migrate", target_node="<dst>")
execute_proposal("<id>")
```

Expected: `{"refused": "decision is propose for cell hypervisor/migrate/single-host …", "next": "an operator runs `helper exec run …`"}`,
`helper exec receipts` shows nothing new, `helper trust history` shows nothing new.

### Step 2 — grant CONFIRM, trigger, tap Approve

```bash
uv run helper trust grant hypervisor migrate single-host confirm
```

Call `execute_proposal("<id>")` again. Your phone shows **homelab-helper:
approve this action?** with Approve / Deny. Tap **Approve**.

Expected: the tool returns `outcome: succeeded` with a receipt id; the guest
is on `<dst>` in the Proxmox UI; `helper trust history` has an `approval`
event naming the channel and your device; `helper exec receipts` shows the
receipt with `strategy: prior-node`.

### Step 3 — roll it back

```bash
uv run helper exec rollback <receipt-id> --yes
```

Expected: the guest migrates back to `<src>`; the original receipt is marked
rolled back and linked to the undo receipt.

### Step 4 — Deny and timeout both leave it pending

Propose the migrate again, trigger it, tap **Deny**. Expected: `refused:
declined via home-assistant …`, proposal still pending, an `approval` event
with `approved: false`. Trigger once more and let the notification sit past
the timeout. Expected: the same refusal with `no answer within …s`.

### Step 5 — a workload restart, if you run Kubernetes

Pick a stateless Deployment you can bounce.

```bash
uv run helper trust grant containers workload-restart single-service confirm
```

```
propose_workload_action("workload-restart", namespace="<ns>", kind="deployment",
                        name="<name>", title="validate rollout restart")
execute_proposal("<id>")
```

Tap **Approve**. Expected: pods roll; the receipt's `rollback_state` has
`strategy: rollout-undo` and the revision it will return to; `helper exec
rollback <receipt-id> --yes` runs `kubectl rollout undo` to that revision.

### Step 6 — the slice-2 surfaces, as needed

Each follows the same shape — grant CONFIRM, propose, trigger, tap, check the
receipt, `helper exec rollback` — on a target you can afford to touch:

- `hypervisor cpu-type single-host`: `propose_action("cpu-type", node=, vmid=,
  vm_kind="qemu", cpu_type="x86-64-v3", …)` on the throwaway guest; the change
  shows under the guest's *Pending* tab until its next stop/start.
- `hypervisor resize single-host` / `containers resize single-host`:
  `propose_action("resize", node=, vmid=, vm_kind=, cores=… and/or memory_mib=…)`,
  or let `helper plan rightsize --persist` + the `rightsize` playbook draft it.
  A VM shows the change under *Pending* until its next stop/start; a container
  changes live. Rollback sets the prior cores/memory (and balloon) back.
- `containers argocd-sync single-service`: `propose_argocd_sync("<app>", …)` on
  an app that is already Synced (a no-op sync); rollback returns to the same
  history entry.
- `dns dns-record single-service`: `propose_dns_record("validate.lan",
  "10.0.0.250", …)`; rollback deletes the record it created.

`helper approvals show` lists what each pending proposal would get and the
answers so far.

Run 10/03/2026 (Covington lab): all three approved from the phone within a few
seconds; cpu-type showed as *pending* on guest 102 and rolled back; the DNS
record appeared on the Wyola controller and rolled back by deletion; the Argo
CD sync ran as a no-op on `app-guacamole-reconciler`. Finding: that app has
**automated sync**, and Argo CD refuses its rollback API while it is on — so the
verifier now reports such apps as unverifiable (AUTONOMOUS degrades to CONFIRM)
and the undo of a sync there is a git revert, which stays human. A timed-out
request (no tap within 300 s) was recorded as declined and changed nothing.

### Step 7 — the proactive loop (slice 3)

```bash
uv run helper discover k8s                      # now also opens workload-unhealthy findings
uv run helper daemon run --once --no-ask        # discovery + playbooks: see what gets drafted
uv run helper approvals show                    # which drafts would ask you
uv run helper daemon run --once                 # the listener asks about them; tap on the phone
```

Expected: a drifted Argo CD app or an unhealthy workload produces exactly one
pending proposal (`proposed_by` = `playbook:…`), a second pass drafts nothing
new, the listener asks once and never re-asks a denied one.

### Step 8 — the unattended run announces itself (slice 4)

```bash
uv run helper trust grant containers workload-restart single-service autonomous   # or let the streak promote it
uv run helper daemon run --once           # a drafted restart now runs with no tap …
# … and the phone shows "✓ homelab-helper: Rollout restart …" with the undo one-liner
uv run helper exec rollback <receipt>     # optional; a confirmed rollback is silent
```

Draft the proposal **after** the grant: the cron listener asks about any pending
agent proposal on its next tick, and a question that timed out at CONFIRM is
never run unattended later, even once the cell is AUTONOMOUS.

Expected: the receipt exists before the notification arrives (`helper exec
receipts`); the notice names the cell, the target, `unattended`, and
`helper exec rollback <id>`. Break the next one on purpose (scale a workload
that does not exist) and the "✗" notice reports the failure and the
demotion to PROPOSE.

### Step 9 — clean up

Revoke the grants unless you want to keep them (`helper trust show`),
unset the approval service if you do not want agents able to ask.

---

## Part 4 — rolling a node update (Phase 8)

`node-update` is the first action with **no inverse**. There is no rollback, no
snapshot and no undo: if a dist-upgrade breaks a node, you recover it from a
backup. So it can never be auto-promoted and never runs unattended — every one
is a decision you make.

Sequencing is **your** procedure, not one action. One action is one decision
and one receipt, which is the gradient's unit; a composite drain-update-reboot
action would hide which step failed. Do one node at a time, and never start the
next until the last is verified.

### Before the first one

```bash
uv run helper discover versions        # 8.1 names which nodes are behind
uv run helper exec list                # the node-update drafts, if the daemon has run
```

Confirm `helper trust show` has **no** grant on `host-os/node-update/single-host`
yet. Grant it when you are ready to do the first one by hand, and consider
removing the grant afterwards until the next maintenance window.

### Per node

1. **Drain.** Migrate the node's running guests off it. `helper plan rebalance`
   proposes targets; each move is a `migrate` proposal with a real rollback
   (`prior-node`), so this step is the reversible one.
2. **Confirm it is drained.** The executor refuses an undrained node, but check
   first — a refusal costs you a round trip. Stopped guests are fine.
3. **Update.** `helper exec run <id>` on the node-update proposal. It needs a
   `CONFIRM` grant and your consent; expect minutes, not seconds, and watch the
   receipt rather than the terminal.
4. **Reboot.** By hand, from the Proxmox UI or console. Deliberately not an
   action: a node that does not come back is the hairiest failure mode here, and
   an action whose receipt already said "succeeded" would be a poor place for
   it.
5. **Verify.** The node is back, quorate, and `helper discover versions` no
   longer names it. Migrate guests back if you want them there.
6. **Next.** Only now.

**Pass:** each node updates with a receipt naming the exit code, the undrained
refusal produces no SSH call at all (check the node's auth log), and
`helper discover versions` resolves that node's `pve-updates` finding on the
next pass.

**Stop if:** a node does not come back, a dist-upgrade exits non-zero, or
quorum drops. There is no undo — recover that node before touching another.

---

## Sign-off

✅ validated · ⏳ not yet run · ⚪ not observable on this fleet, with the reason.
A criterion only this lab's shape hides is not `⚪` if a fixture can supply that
shape — see the asymmetric fixture under P4-AC2.

| Criterion | Result | Notes |
|---|---|---|
| P4-AC1 chat grounded | ✅ 10/03/2026 | 17 hosts named from inventory, cloud footer honest. Found: the footer said "cloud" but not that Ollama had been tried and was unreachable — `RouterResult.skipped` + a `skipped:` line (PR #52). |
| P4-AC2 Ceph narration | ✅ derivation 10/09/2026 · ⏳ narration | Not observable on this fleet by construction: bmax0–3 are symmetric 1 GbE, Ceph HEALTH_OK, covomv on 10 GbE, so the analyser is correctly silent. `fixtures/asymmetric-lab.yaml` supplies the asymmetry — `discover replay` + `bottlenecks` produce the `CEPH_BOTTLENECK` finding a narrator would cite, through the real CLI, with no hardware (`tests/test_lab_replay_asymmetric.py`). The prose half is one `helper bottlenecks --narrate` against your own router; still to run. |
| P4-AC3 onboard | ⏳ | Interactive; not yet run. |
| P4-AC4 MCP discovery | ✅ 10/03/2026 | `probe_host bmax3` from Claude Code: 4 probes, 33 observations, 0 failures, capability changes reconciled. |
| P4-AC5 strict-local refusal | ✅ 10/03/2026 | Names the tier, the policy, each backend's exclusion reason, and the three options. |
| P4-AC6 skill profile | ✅ mechanism 10/03/2026 | One Ceph/CSI question inferred `storage` + `container-orchestration` (basic, evidence 1). Longitudinal drift still to observe. |
| P5-AC1 workload library | ✅ 10/03/2026 | 67 entries. |
| P5-AC2 placement | ✅ 10/03/2026 | `immich` → bmax0 with RAM headroom, threads, GPU optionality and photo-library data gravity; arm/RAM rejections explained. |
| P5-AC3 rebalance | ✅ after fix 10/03/2026 | First run: no migrations-only plan. Then: Proxmox guests planned onto a NAS, arm64 Pis and Talos workers, one VM ping-ponging. Three defects fixed (PR #51); now three plan classes, all moves within bmax0–3, no repeated VM. |
| P5-AC4 Ceph mitigations | ✅ 10/09/2026 | Against the asymmetric fixture: all four mitigations derived from its facts — CRUSH-reweight away from `ceph-c`, bring it 1000→2500 Mbps, relocate its OSDs to `ceph-a`, accept it as a cold tier. The runbook's "fail if they look hardcoded" check is now a test rather than a manual step: change `ceph-c`'s speed and the recommendation moves with it; make it symmetric and the pattern goes quiet. |
| P5-AC5 surplus | ✅ 10/03/2026 | bmax0: three stopped guests, 32 GiB spare DIMMs, three options. Gap: covomv (a NAS running Docker) is also called surplus — the planner has no "not a hypervisor" notion; same root as the P5-AC3 targets defect, noted in backlog. |
| P5-AC6 VPN path refused | ✅ 10/03/2026 | Needed a topology file (none existed): two sites, VPN 6 ms RTT measured, bandwidth a placeholder. `plan path wynode2 bmax0` → LAN-grade: no. |
| P6 steps 0–7 | ✅ by way of P7 (10/03/2026) | Grants, pessimistic gate, execution, receipts, rollback, override logging and demotion-on-reject all ran live during the Phase 7 sessions; step 5 (a *dispatch failure* demotes) and step 6 (kill switch mid-flight) were exercised by tests only. |
| P7 steps 0–6 | ✅ 10/03/2026 | Covington lab: guest 102 (devbox clone) migrated bmax0→bmax3→bmax0 and rolled back to bmax3; `homepage` deployment restarted (rev 19) and undone (rev 20 from 18); Approve, Deny and both undo paths exercised from a Pixel; every answer on `trust history`. Finding: Android shows the buttons only when the notification is expanded — hint + `clickAction: noAction` added. |
| P7 step 7 (proactive loop) | ✅ 10/03/2026 | app-wirestudio resync drafted by `argocd-resync`, asked by the listener, approved from the phone, executed (receipt actor `listener`). Found: Synced/Degraded apps got a useless resync (fixed: OutOfSync only); app-of-apps blipped OutOfSync under automated sync and the phone was asked before Argo healed it (fixed: 15-min debounce + withdrawal). Daemon now runs from cron every 15 min. |
| P8 node-update (rolling, per node) | ⏳ | Not yet run. No inverse: see Part 3 before the first one. |
| P7 step 8 (unattended run notice) | ✅ 10/05/2026 | `containers/workload-restart/single-service` granted AUTONOMOUS; an agent-drafted restart of `homepage/deployment/homepage` ran from `helper daemon run --once` with no tap (rev 20 → 21, receipt actor `listener`, rollback `rollout-undo` verified); the phone showed the ✓ notice with `unattended` and the `helper exec rollback` line, after the receipt. Failure/demotion path not forced live — it needs a write that fails after a read that succeeds; pinned by tests. Cell left at AUTONOMOUS by the operator's choice. Found: the 15-min cron listener asked about the proposal while the cell was still CONFIRM, the ask timed out, and the listener then (correctly) refused to run it unattended after the grant — draft *after* granting. |

Open as of 10/09/2026, all of it operator time rather than code:

- **P4-AC3 onboard** — interactive, needs a host the harness has never seen.
- **P4-AC2 narration** — one `helper bottlenecks --narrate` against the
  asymmetric fixture and your own router. No hardware; see that section.
- **P6 steps 5 and 6 live** — a dispatch failure demoting a cell, and the kill
  switch mid-flight. Both need a write that fails after a read that succeeded,
  which is hard to stage honestly; pinned by tests meanwhile.
- **P8 node-update** — the first rolling update. Read Part 3 first: there is no
  inverse, so the cell cannot reach AUTONOMOUS and never should.

Phase 7 is signed off end to end; the first unattended execution ran 10/05/2026
and `containers/workload-restart/single-service` is live at AUTONOMOUS.
