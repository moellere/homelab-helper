"""A Rich console that masks every secret value this process has resolved.

Error text from adapters and SSH can quote a credential back (a URL with a
token, an auth failure echoing the password). ``secrets.redact`` knows each
value read through ``secret_from_env``; this console applies it to every
string it prints, so an error path cannot forget to.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

from homelab_helper.secrets import redact


class RedactingConsole(Console):
    def print(self, *objects: Any, **kwargs: Any) -> None:
        super().print(*(redact(o) if isinstance(o, str) else o for o in objects), **kwargs)


__all__ = ["RedactingConsole"]
