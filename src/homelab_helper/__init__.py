"""homelab-helper — OSS framework for homelab planning, inventory, and audit."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("homelab-helper")
except PackageNotFoundError:  # pragma: no cover - editable install before metadata exists
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
