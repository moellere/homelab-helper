"""``Probe`` abstract base class and surrounding types.

The full contract per ``architecture.md``:

- A ``Probe`` is a plugin. Concrete probes live in this package (first-party)
  or in third-party packages that ship an entry point in the
  ``homelab_helper.probes`` group.
- Each probe declares its name, version, output ``schema_version``, the
  privilege level its execution requires, the observation keys it can
  produce, and the kinds of target it operates against (``host``, ``network``,
  ``cluster``, ``service``).
- The probe receives a :class:`ProbeContext` (target + adapters + run id)
  and returns a :class:`ProbeResult` containing a list of
  :class:`ObservationData` and a success/error indicator. The probe NEVER
  touches the DB directly; the runner (see :mod:`homelab_helper.engine.runner`)
  persists results.

Output schemas: when a probe declares ``output_schema: type[BaseModel]``,
the runner validates that the dumped form of the probe's primary output
fits the schema. Observations are emitted as flat dotted keys regardless;
the schema is for catching bugs at the probe boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

from homelab_helper.db.enums import Confidence, IntentTargetType, PrivilegeLevel


class ObservationData(BaseModel):
    """Transport object for a single observed fact.

    This is NOT the DB row — the runner converts ``ObservationData`` instances
    into ``homelab_helper.db.models.Observation`` rows after the probe completes.
    Keeping the transport object distinct means probes can be tested without
    a database.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    """Dotted key path, e.g. ``host.cpu.model``."""

    value: Any
    """Anything JSON-serializable: scalar, list, dict."""

    confidence: Confidence = Confidence.VERIFIED
    """Trust level the probe wants attached to this observation."""

    target_type: IntentTargetType | None = None
    """The kind of resource this observation describes (host/vm/lxc/etc.)."""

    target_id: str | None = None
    """The identifier for the target — Host.id, NetBox Service id, etc."""


class ProbeTarget(BaseModel):
    """Describes the thing a probe is running against.

    For host probes: ``host_id`` (DB UUID) and/or ``hostname``/``primary_ip``.
    For network probes: ``network_cidr``.
    For cluster probes: ``cluster_name``.

    Probes inspect whichever fields are relevant for their ``target_kinds``.
    """

    model_config = ConfigDict(frozen=False)

    kind: str
    """One of ``host``, ``network``, ``cluster``, ``service``."""

    host_id: str | None = None  # stringified UUID
    hostname: str | None = None
    primary_ip: str | None = None
    network_cidr: str | None = None
    cluster_name: str | None = None

    # Per-target overrides for SSH-style probes. Real secrets live in the
    # secrets store and are dereferenced via credentials_ref; this is the
    # in-flight unwrapped view the probe needs to actually connect.
    ssh_user: str | None = None
    ssh_key_path: str | None = None
    ssh_password: str | None = None
    ssh_port: int = 22


class AdapterRegistry:
    """Tiny DI container — probes ask for adapters by name.

    Kept deliberately minimal: ``registry.get("kernel-ssh")`` returns the
    adapter instance or raises ``KeyError`` if unregistered. Probes that need
    a specific adapter should fail loudly when it's missing rather than
    no-op-ing.
    """

    def __init__(self, adapters: dict[str, Any] | None = None) -> None:
        self._adapters: dict[str, Any] = dict(adapters or {})

    def register(self, name: str, adapter: Any) -> None:
        self._adapters[name] = adapter

    def get(self, name: str) -> Any:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise KeyError(f"adapter {name!r} is not registered") from exc

    def has(self, name: str) -> bool:
        return name in self._adapters


class ProbeContext(BaseModel):
    """What every ``Probe.run`` receives.

    Built by the runner before invoking the probe. Probes shouldn't construct
    these themselves except in tests.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=False)

    target: ProbeTarget
    adapters: AdapterRegistry
    run_id: str
    """Stringified UUID of the parent DiscoveryRun."""
    timeout_s: int = 60
    """Default per-probe timeout. Probes can split this further across their
    individual commands."""


class ProbeResult(BaseModel):
    """What every ``Probe.run`` returns."""

    model_config = ConfigDict(frozen=True)

    observations: list[ObservationData] = []
    success: bool = True
    error: str | None = None
    raw_payload: dict[str, Any] | None = None
    """Optional structured output that the runner can validate against
    ``Probe.output_schema``. Useful for tests and debugging."""


class Probe(ABC):
    """Base class for all probe plugins.

    Subclasses declare class-level metadata and implement :meth:`run`. The
    runner discovers probes via Python entry points (group:
    ``homelab_helper.probes``).
    """

    name: ClassVar[str]
    """Stable identifier across versions — e.g. ``host.identity``."""

    version: ClassVar[str]
    """Probe implementation version (semver-compatible string)."""

    schema_version: ClassVar[int] = 1
    """Bumps only when probe output structure changes."""

    required_privilege: ClassVar[PrivilegeLevel] = PrivilegeLevel.USER
    """Credential level the probe needs to run."""

    produces_keys: ClassVar[list[str]] = []
    """Observation keys this probe can produce (used by capability matching)."""

    target_kinds: ClassVar[list[str]] = []
    """``host``, ``network``, ``cluster``, ``service`` — what this probe applies to."""

    output_schema: ClassVar[type[BaseModel] | None] = None
    """Optional Pydantic model describing the structured output. When set, the
    runner validates ``ProbeResult.raw_payload`` against this schema."""

    description: ClassVar[str | None] = None

    @abstractmethod
    async def run(self, ctx: ProbeContext) -> ProbeResult:
        """Execute the probe against ``ctx`` and return a :class:`ProbeResult`.

        Implementations should:

        - return ``success=False`` with a non-empty ``error`` on expected
          failure modes (target unreachable, command-not-found, etc.);
        - raise on truly unexpected conditions — the runner converts those
          into ``success=False`` with the traceback in ``error``;
        - not perform DB writes (the runner persists the result);
        - keep total runtime under ``ctx.timeout_s`` as a soft target.
        """


__all__ = [
    "AdapterRegistry",
    "ObservationData",
    "Probe",
    "ProbeContext",
    "ProbeResult",
    "ProbeTarget",
]
