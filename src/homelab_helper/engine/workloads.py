"""WorkloadProfile schema + library loader (Phase 5, AC1).

A workload profile is the planner's unit of reasoning: what a service needs at
baseline (cores/RAM/footprint), how it scales, which architectures it runs on,
whether a GPU helps or is mandatory, what it depends on, and — the placement-
critical one — its **data gravity**: the large dataset it wants to live near.
``storage_gb`` is the app's own footprint; the media library it serves is
gravity, not footprint.

The starter library ships in ``homelab_helper/data/workload-library.yaml`` (baselines are
starter estimates, not benchmarks). Operators point
``HOMELAB_HELPER_WORKLOAD_LIBRARY`` at their own file to extend/override;
entries there shadow same-named starters.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

_VALID_GPU = {"none", "optional", "required"}
_VALID_NETWORK = {"any", "lan-preferred", "lan-required"}
_VALID_SCALING = {"flat", "with-users", "with-data", "with-cameras", "with-devices"}

DEFAULT_LIBRARY_PATH = Path(__file__).resolve().parent.parent / "data" / "workload-library.yaml"
"""The starter library, shipped inside the package so an installed wheel finds it."""

LIBRARY_ENV_VAR = "HOMELAB_HELPER_WORKLOAD_LIBRARY"


class WorkloadLibraryError(ValueError):
    """A library file is malformed — message names the entry and the problem."""


@dataclass(frozen=True)
class WorkloadProfile:
    name: str
    category: str
    description: str
    cpu_cores: float
    ram_mb: int
    storage_gb: float
    scaling: str = "flat"
    arch: tuple[str, ...] = ("amd64",)
    gpu: str = "none"
    gpu_purpose: str | None = None
    depends_on: tuple[str, ...] = ()
    data_gravity: str | None = None
    network_class: str = "any"
    """Path sensitivity: lan-required = sync replication, replicas must be
    LAN-grade of each other; lan-preferred = degraded-but-works cross-site."""
    artifacts: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view for the MCP surface (tuples become lists)."""
        return {k: list(v) if isinstance(v, tuple) else v for k, v in asdict(self).items()}


def _parse_entry(name: str, raw: dict[str, Any]) -> WorkloadProfile:
    def fail(problem: str) -> WorkloadLibraryError:
        return WorkloadLibraryError(f"workload {name!r}: {problem}")

    for required in ("category", "description", "cpu_cores", "ram_mb", "storage_gb"):
        if required not in raw:
            raise fail(f"missing required field {required!r}")
    gpu = str(raw.get("gpu", "none")).lower()
    if gpu not in _VALID_GPU:
        raise fail(f"gpu {gpu!r} must be one of {sorted(_VALID_GPU)}")
    scaling = str(raw.get("scaling", "flat")).lower()
    if scaling not in _VALID_SCALING:
        raise fail(f"scaling {scaling!r} must be one of {sorted(_VALID_SCALING)}")
    network_class = str(raw.get("network_class", "any")).lower()
    if network_class not in _VALID_NETWORK:
        raise fail(f"network_class {network_class!r} must be one of {sorted(_VALID_NETWORK)}")
    try:
        cpu_cores = float(raw["cpu_cores"])
        ram_mb = int(raw["ram_mb"])
        storage_gb = float(raw["storage_gb"])
    except (TypeError, ValueError) as exc:
        raise fail(f"non-numeric baseline: {exc}") from exc
    if cpu_cores <= 0 or ram_mb <= 0 or storage_gb <= 0:
        raise fail("baselines must be positive")
    arch = tuple(str(a).lower() for a in raw.get("arch") or ["amd64"])
    return WorkloadProfile(
        name=name,
        category=str(raw["category"]),
        description=str(raw["description"]),
        cpu_cores=cpu_cores,
        ram_mb=ram_mb,
        storage_gb=storage_gb,
        scaling=scaling,
        arch=arch,
        gpu=gpu,
        gpu_purpose=raw.get("gpu_purpose"),
        depends_on=tuple(raw.get("depends_on") or ()),
        data_gravity=raw.get("data_gravity"),
        network_class=network_class,
        artifacts=tuple(raw.get("artifacts") or ()),
    )


def _load_file(path: Path) -> dict[str, WorkloadProfile]:
    payload = yaml.safe_load(path.read_text())
    if not isinstance(payload, dict) or not isinstance(payload.get("workloads"), dict):
        raise WorkloadLibraryError(f"{path}: expected a top-level 'workloads' mapping")
    return {
        name: _parse_entry(name, raw if isinstance(raw, dict) else {})
        for name, raw in payload["workloads"].items()
    }


def load_workload_library(path: Path | None = None) -> dict[str, WorkloadProfile]:
    """The merged library: starter entries, shadowed by the operator's file.

    ``path`` overrides everything (tests). Otherwise the starter library loads
    first and ``HOMELAB_HELPER_WORKLOAD_LIBRARY`` — if set — is layered on top,
    same-named entries winning.
    """
    if path is not None:
        return _load_file(path)
    library = _load_file(DEFAULT_LIBRARY_PATH)
    operator_path = os.environ.get(LIBRARY_ENV_VAR)
    if operator_path:
        library.update(_load_file(Path(operator_path)))
    return library


__all__ = [
    "DEFAULT_LIBRARY_PATH",
    "LIBRARY_ENV_VAR",
    "WorkloadLibraryError",
    "WorkloadProfile",
    "load_workload_library",
]
