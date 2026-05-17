"""ProbeRunner — orchestrates one probe execution end-to-end.

Responsibilities:

1. Look up (or create) the ``Probe`` DB row for the probe's ``name``.
2. Open a ``DiscoveryRun`` row in the DB.
3. Invoke ``probe.run(ctx)``.
4. Optionally validate the probe's ``raw_payload`` against ``output_schema``.
5. Convert every ``ObservationData`` into an ``Observation`` row attached to
   the run.
6. Close the ``DiscoveryRun`` row (ended_at, success, error, count).

Probes never touch the session themselves; the runner owns persistence so
probes stay testable without a database.
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from homelab_helper.db.models import DiscoveryRun, Observation
from homelab_helper.db.models import Probe as ProbeRow
from homelab_helper.probes.base import (
    AdapterRegistry,
    Probe,
    ProbeContext,
    ProbeResult,
    ProbeTarget,
)

if TYPE_CHECKING:
    import uuid as _uuid

    from sqlalchemy.ext.asyncio import AsyncSession


class ProbeRunner:
    """Run a single probe and persist the result.

    Reused across runs; instances are cheap. The caller passes a fresh
    ``AsyncSession`` per ``run()`` call so transaction boundaries are
    well-defined.
    """

    def __init__(self, adapters: AdapterRegistry) -> None:
        self.adapters = adapters

    async def _resolve_probe_row(self, session: AsyncSession, probe: Probe) -> ProbeRow:
        """Look up the Probe DB row for ``probe.name``; insert if missing.

        Auto-insert keeps the runner usable on a DB that hasn't seen entry-point
        sync yet. The reconciler's ``helper probes register`` flow is the
        idiomatic path; this is a safety net.
        """
        row = (
            await session.execute(select(ProbeRow).where(ProbeRow.name == probe.name))
        ).scalar_one_or_none()
        if row is not None:
            return row
        row = ProbeRow(
            name=probe.name,
            version=probe.version,
            module_path=f"{type(probe).__module__}:{type(probe).__qualname__}",
            schema_version=probe.schema_version,
            required_privilege=probe.required_privilege,
            produces_keys=list(probe.produces_keys),
            description=probe.description,
            enabled=True,
        )
        session.add(row)
        await session.flush()
        return row

    async def run(
        self,
        probe: Probe,
        target: ProbeTarget,
        session: AsyncSession,
        *,
        host_id: _uuid.UUID | None = None,
        triggered_by: str = "manual",
        timeout_s: int = 60,
    ) -> tuple[DiscoveryRun, ProbeResult]:
        """Execute ``probe`` and return the persisted DiscoveryRun + ProbeResult.

        Caller is responsible for committing the session — the runner only
        flushes. That lets callers batch multiple probe runs in one transaction
        if desired.
        """
        probe_row = await self._resolve_probe_row(session, probe)

        run_row = DiscoveryRun(
            host_id=host_id,
            target_spec=target.primary_ip or target.hostname or target.network_cidr,
            probe_id=probe_row.id,
            probe_name=probe.name,
            probe_version=probe.version,
            privilege_level=probe.required_privilege,
            triggered_by=triggered_by,
        )
        session.add(run_row)
        await session.flush()

        ctx = ProbeContext(
            target=target,
            adapters=self.adapters,
            run_id=str(run_row.id),
            timeout_s=timeout_s,
        )

        result: ProbeResult
        try:
            result = await probe.run(ctx)
        except Exception as exc:
            tb = traceback.format_exc()
            result = ProbeResult(
                observations=[],
                success=False,
                error=f"unhandled exception in probe.run: {exc}\n{tb}",
            )

        # Optional output schema validation.
        if probe.output_schema is not None and result.success and result.raw_payload is not None:
            try:
                probe.output_schema.model_validate(result.raw_payload)
            except Exception as exc:
                result = result.model_copy(
                    update={
                        "success": False,
                        "error": f"output_schema validation failed: {exc}",
                    }
                )

        # Persist observations.
        observation_count = 0
        if result.success:
            for od in result.observations:
                session.add(
                    Observation(
                        run_id=run_row.id,
                        key=od.key,
                        value=od.value,
                        confidence=od.confidence,
                        target_type=od.target_type,
                        target_id=od.target_id,
                    )
                )
                observation_count += 1

        run_row.ended_at = datetime.now(UTC)
        run_row.success = result.success
        run_row.error = result.error
        run_row.observation_count = observation_count
        await session.flush()
        return run_row, result


__all__ = ["ProbeRunner"]
