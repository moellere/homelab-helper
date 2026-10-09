# Writing a probe

A probe is the unit of discovery: it runs against one target, returns a list of
observations, and never touches the database. The runner persists what it
returns, the reconciler turns observations into inventory and findings. That
split is what makes a probe testable with no database and no lab.

First-party probes live in `homelab_helper.probes`; yours can live in any
package that declares an entry point in the `homelab_helper.probes` group.

## The contract

Subclass `Probe`, declare the class-level metadata, implement `run`.

```python
from typing import ClassVar

from homelab_helper.db.enums import IntentTargetType, PrivilegeLevel
from homelab_helper.probes.base import ObservationData, Probe, ProbeContext, ProbeResult


class HostUptimeProbe(Probe):
    name: ClassVar[str] = "host.uptime"            # stable across versions
    version: ClassVar[str] = "0.1.0"
    schema_version: ClassVar[int] = 1              # bump only when the output shape changes
    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.USER
    target_kinds: ClassVar[list[str]] = ["host"]
    produces_keys: ClassVar[list[str]] = ["host.uptime.seconds"]
    description: ClassVar[str | None] = "Seconds since boot, from /proc/uptime."

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        target = ctx.target
        if not ctx.adapters.has("kernel-ssh"):
            return ProbeResult(success=False, error="kernel-ssh adapter is not registered")
        ssh = ctx.adapters.get("kernel-ssh")
        try:
            async with ssh.session(
                target.primary_ip or target.hostname,
                user=target.ssh_user,
                key_path=target.ssh_key_path,
                port=target.ssh_port,
            ) as conn:
                res = await conn.run("cat /proc/uptime")
        except Exception as exc:
            return ProbeResult(success=False, error=f"ssh failure: {exc}")
        seconds = parse_uptime(res.stdout) if res.ok else None
        if seconds is None:
            return ProbeResult(success=False, error="could not read /proc/uptime")
        return ProbeResult(
            observations=[
                ObservationData(
                    key="host.uptime.seconds",
                    value=int(seconds),
                    target_type=IntentTargetType.HOST,
                    target_id=target.host_id or target.hostname,
                )
            ]
        )
```

What each piece is for:

| | |
|---|---|
| `name` | The stable identifier. Dotted, namespaced by what it reads: `host.*`, `network.*`, `talos.*`. It is the key the database registers the probe under, so it never changes once shipped. |
| `produces_keys` | Every observation key the probe can emit. Capability matching uses it — a host "has" a fact when some probe can produce it. |
| `required_privilege` | `NONE`, `USER` or `ROOT`. The runner checks it against what the target's credentials allow before running. |
| `target_kinds` | `host`, `network`, `cluster` or `service`. A probe reads the matching fields off `ctx.target`. |
| `output_schema` | Optional. A Pydantic model for the structured output; when set, the runner validates `ProbeResult.raw_payload` against it, which catches shape bugs at the probe boundary. |

And the rules `run` follows:

- Return `success=False` with a non-empty `error` on the failure modes you
  expect: target unreachable, command missing, output unparseable.
- Raise on anything you did not expect; the runner records the traceback as
  the error.
- Never write to the database. Never shell out to anything that changes the
  target.
- Keep the total under `ctx.timeout_s` (60 seconds by default) as a soft
  target; split it across your own commands if you run several.

## Observations

An `ObservationData` is a flat, dotted key and a JSON-serialisable value. Emit
one per fact, not one blob. The reconciler reads specific keys —
`host.storage.devices`, `host.network.interfaces`, `host.memory.dimms`,
`host.memory.mem_total_bytes` and so on — and anything else is stored as an
observation and available to assertions and to chat.

If your probe reads something the reconciler should turn into inventory (a new
kind of part with an identity, say), the probe is half the work: the other
half is a lineage method in the reconciler, and that is a conversation to have
in an issue first.

## Adapters

A probe does not open its own connections. It asks the `AdapterRegistry` on the
context for a named adapter — `kernel-ssh` for Linux hosts, `talos` for Talos
nodes — and fails loudly if it is missing. This is what lets a test hand the
probe a fake.

## Registering it

In your package's `pyproject.toml`:

```toml
[project.entry-points."homelab_helper.probes"]
"host.uptime" = "my_package.probes:HostUptimeProbe"
```

Install the package next to `homelab-helper`, then:

```bash
helper probes register     # re-syncs entry points into the database; idempotent
helper probes list
helper discover host <hostname> --ssh-user <user> --ssh-key <path>
```

`helper db init` does the same sync, so a probe present at install time needs
nothing more. A misbehaving entry point — import error, not a `Probe` — is
skipped with a warning rather than aborting discovery; one bad plugin does not
keep the framework from booting.

## Testing it

Two layers, the same way the first-party probes are tested:

**Parsers are plain functions.** Keep every bit of output parsing in
module-level functions (`parse_uptime` above) and unit-test those against
captured output, including the junk cases: empty, truncated, a different
distribution's format.

**`run` gets a fake adapter.** Build a `ProbeContext` by hand with an
`AdapterRegistry` holding a fake whose `session()` yields an object whose
`run()` returns canned results. The first-party suite's `tests/test_probes.py`
is the model. For network probes that genuinely need a socket, use an asyncio
loopback server on an ephemeral 127.0.0.1 port rather than mocking
`asyncio.open_connection`.

Nothing in the test suite may reach a real host. If a test needs a live SSH
target it is an integration test, gated behind an environment variable, and
skipped by default.
