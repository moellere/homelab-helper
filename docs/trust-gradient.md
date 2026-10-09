# The trust gradient, for operators

This page is about what the levels mean from your seat, how to raise one
without regretting it, and what to do when something goes wrong. The design
behind it — why it is shaped this way — is in the
[architecture](architecture.md#trust-gradient-l2-authorization-model).

## The one rule

Every write to your lab runs through a single function:

```
decide(action, context) → BLOCK | PROPOSE | CONFIRM | AUTONOMOUS
```

It is plain Python over a handful of facts. **No model is ever in it.** A
model may *draft* an action — chat, the MCP tools and the playbooks all do —
and policy decides whether that draft runs. A fresh install has every cell at
`PROPOSE`, which means the framework executes nothing until you say otherwise,
one kind of change at a time.

## The four levels

| Level | What happens | When you see it |
|---|---|---|
| `BLOCK` | Refused, with the policy reason. | A domain or host you fenced off, or an action the policy forbids outright. |
| `PROPOSE` | Logged as a proposal. You apply it by hand, or not. | **The default for everything.** `helper exec list` shows them; `helper exec accept` records that you did it yourself. |
| `CONFIRM` | The framework may execute it, but asks first — on the CLI, or on your phone. | The first level you grant. Each action is a separate yes. |
| `AUTONOMOUS` | Executes unattended, leaves a receipt, notifies you afterwards, can roll itself back. | Only once a cell has earned it or you have granted it, and only for changes with a verified undo. |

## What a cell is

Trust is not granted to "Proxmox" or to "the framework". It is granted to a
**cell**:

```
cell = domain × action-kind × blast-radius
```

`hypervisor / restart / single-host` is one cell. `hypervisor / migrate /
single-host` is a different one, and a clean record on the first says nothing
about the second. This is deliberate: the unit you trust is as narrow as the
unit that can go wrong.

```bash
helper trust show       # every domain, every granted cell, boundaries, open windows
```

### Domains

`inventory-metadata`, `containers`, `dns`, `network-fabric`, `storage`,
`hypervisor`, `host-os`, `secrets`. Each has a ceiling no cell inside it can
exceed. `secrets` is **absolute**: nothing in it can execute, and no runtime
gesture can change that.

### Action kinds that exist today

| Domain | Kinds | Undo |
|---|---|---|
| `hypervisor` (Proxmox guests) | `start` `stop` `shutdown` `restart` `migrate` `cpu-type` `resize` | prior power state or a snapshot, prior node, prior config |
| `containers` (Kubernetes) | `workload-restart` `workload-scale` | rollout undo, prior replicas |
| `containers` (Argo CD) | `argocd-sync` | Argo CD's own history |
| `dns` (UniFi static DNS) | `dns-record` | the prior record, or deleting the created one |
| `host-os` | `node-update` (an apt dist-upgrade on one drained node) | **none** — see below |

## Granting your first cell

Start with something reversible and small. A container restart is the usual
first cell: its undo is a rollout undo, and the harness verifies that undo
exists before every run rather than taking the proposal's word for it.

```bash
helper trust grant containers workload-restart single-service confirm
helper exec list                      # pending proposals the gate would now let run
helper exec run <proposal-id>         # asks you; then executes; then writes a receipt
helper exec receipts                  # what ran, at which level, with its rollback state
helper trust history                  # the append-only record of every grant, run and answer
```

`CONFIRM` is the right first level because it proves the whole machine —
execution, receipt, rollback — while a human is still the last step.

### Asking on your phone

With a Home Assistant notify service configured
(`HOMELAB_HELPER_APPROVAL_NOTIFY_SERVICE=notify.mobile_app_<phone>`), a
`CONFIRM` action triggered by an agent or the daemon arrives as a notification
with Approve and Deny. Your tap is recorded as an approval event on the same
history the CLI writes to. Deny, or no answer within the timeout, executes
nothing and leaves the proposal pending for the CLI path.

```bash
helper approvals show      # channel status, what would ask you, who answered what
```

## How a cell rises — and falls

**Up, slowly.** A cell that is *reversible* and *low-blast* (`metadata-only`
or `single-host`) earns one rung after five clean confirmed runs: `CONFIRM`
becomes `AUTONOMOUS`. Nothing else auto-promotes; every other cell needs an
explicit grant to leave `PROPOSE`, and never rises on its own.

**Down, instantly.** One bad outcome — a run that fails, a rollback that was
needed — drops the cell to `PROPOSE` and puts it on probation, where it banks
no credit at all. `helper exec reject` on a proposal breaks the clean streak
without demoting.

**Pinned below autonomy.** An action with no inverse — `node-update` is the
first — can never reach `AUTONOMOUS`: its rollback verifier always reports
*unverified, and why*, and the escalation rules do not count it as reversible.
The most it can be is `CONFIRM`, forever, which is the correct amount of trust
for an apt dist-upgrade.

## Raising a floor without regretting it

Two things sit above the cell's own level and are harder to cross:

**Soft-hard floors** — the verified-rollback requirement, and per-host
ceilings you set with `helper trust boundary`. An action that meets one of
these at `AUTONOMOUS` is *degraded to `CONFIRM`*: the framework asks rather
than crossing on its own. You can cross them deliberately in two ways, both
logged, both interactive, both unavailable to any agent:

- a **per-action override** on `helper exec run`, an explicit "I accept this"
  for one action;
- an **elevation window**, a time-boxed lift *scoped* to named hosts or cells,
  never blanket, at most eight hours, with no auto-renew:

```bash
helper window open --reason "maintenance" --minutes 60 --host node2
helper window list
helper window revoke <window-id>
helper window kill                     # the kill switch: every open window, now
```

The kill switch also halts any in-flight autonomous action at its next
checkpoint. If you are unsure what is running, run it first and ask questions
after.

**Absolute floors** — `secrets`, and any host you mark absolute:

```bash
helper trust boundary <host> --absolute
```

No window, no override, no grant can reach an absolute boundary. The only way
to change it is the same command, by you, on purpose. **Set these before you
grant anything.** The live validation runbook makes it step zero for a reason:
fence the NAS, the firewall and the backup target first, then start trusting.

## When something goes wrong

```bash
helper exec receipts                   # find the run
helper exec rollback <receipt-id>      # restore the state captured before it ran
helper trust history                   # see what the gate decided, and why
```

A rollback uses what the harness captured *before* the action — a snapshot, a
prior power state, prior replicas — and refuses honestly when no inverse
exists. The order of operations inside a run is deliberate: the gate decides
first, assuming nothing can be undone, so a refused action never touches the
target even to check; only an authorized action captures rollback state, which
for a snapshot is itself a write.

## What agents can and cannot do

Chat, the MCP server and the daemon's playbooks can *draft* proposals and can
*trigger* a pending one. They cannot grant, elevate, override, roll back, open a
window or change a boundary — the MCP surface has no such tool, and tests
assert that it never gains one. At `CONFIRM` an agent's trigger still ends in
your tap; at `PROPOSE` it ends in a row in `helper exec list` and nothing else.
