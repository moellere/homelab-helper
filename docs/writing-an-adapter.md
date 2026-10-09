# Writing an adapter

An adapter speaks to a management plane — a hypervisor, an orchestrator, a
router, a NAS, a DNS provider — and returns plain Python. Where a probe reads
one host from the inside, an adapter reads a whole system from its API.

Adapters are **read-only at L1**. The harness proposes and never applies, so
an adapter's first version exposes no method that mutates anything, however
obvious the API makes it. Writes come later, if at all, and only through the
executor — see the last section.

## The shape

The OpenMediaVault adapter is a complete, small example; the pattern it follows
is the one every adapter shares.

```python
"""<Source> adapter — read-only <what it is> source.

What the source owns that nothing else can see, how it authenticates, the
configuration variables, and the note that tests inject a client.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from homelab_helper.secrets import secret_from_env


class SourceConfigError(RuntimeError):
    """Raised when required configuration is missing."""


class SourceAdapter:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        verify_ssl: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._url, verify=verify_ssl, timeout=15.0
        )

    @classmethod
    def from_env(cls) -> SourceAdapter:
        url = os.environ.get("HOMELAB_HELPER_SOURCE_URL")
        token = secret_from_env("HOMELAB_HELPER_SOURCE_TOKEN")
        if not url or not token:
            raise SourceConfigError("HOMELAB_HELPER_SOURCE_URL and _TOKEN are required")
        return cls(url, token)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def health_check(self) -> tuple[bool, str | None]: ...

    async def list_things(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/api/things", headers=self._headers())
        resp.raise_for_status()
        return cast("list[dict[str, Any]]", resp.json())
```

Three details carry most of the weight:

**The injected client and `_owns_client`.** Tests pass an `httpx.AsyncClient`
built on `httpx.MockTransport`; the adapter must not close a client it did not
create, or the test loses its transport mid-run. Every adapter that takes a
client has this flag.

**Secrets go through `secret_from_env`.** Never `os.environ.get` for a token or
password. The value may be a `file:` / `keyring:` / `env:` reference that the
resolver dereferences, and reading through the resolver is also what registers
the value with `redact()`, so it is scrubbed from error strings the MCP server
returns. Non-secret settings use `os.environ` as usual.

**`Response.json()` returns `Any`.** Methods with a typed return cast it; mypy
runs in CI.

## Wiring it in

An adapter becomes a *source* in four places, each small:

1. **`config.py` → `SOURCES`.** A `SourceConfig` naming the required and
   optional variables and which are secret. This is what `helper config`
   reports as configured or not, and what the daemon uses to decide which
   sources to discover from by default.
2. **`mcp_server.py` → `_DISCOVERERS`.** An `async def _discover_<source>(session)`
   that builds the adapter from the environment, reads what it reads, hands
   the result to a reconcile function, closes the adapter in a `finally`, and
   returns a JSON-safe summary. The MCP `run_discovery` tool and the daemon
   both call this.
3. **`cli/discover.py`.** A `helper discover <source>` verb with `--persist`
   and `--dry-run`, wrapping its coroutine in a `try/finally` that disposes the
   engine.
4. **A reconcile function** in `engine/`, if the source produces inventory or
   findings. Findings use `make_fingerprint(kind, target_type, target_id,
   root_cause_token)` so the same condition always hits the same row, and
   resolve only when the category was observed this run — never because an
   observation was absent.

Then a row in the getting-started source table, and the README's.

## Testing it

`httpx.MockTransport` against an injected `httpx.AsyncClient`, asserting on
the requests the adapter makes and feeding it the responses the real API
returns. Capture those responses from a real system once and commit them as
fixtures; the test then pins the adapter to the API's actual shape. Never hit a
live system from the suite. `tests/test_netbox_adapter.py` is the canonical
example; every adapter has one.

## If the adapter will ever write

The rules change the moment an adapter gains a mutating method, and they are
mechanical:

- The method is called **only** from `engine/executor.py` (or `engine/rollback.py`
  when it is the inverse). `tests/test_write_isolation.py` greps for every
  write method name across the package and fails if any other module names
  one; add yours to its `WRITE_METHODS`.
- The method carries a block comment saying it exists for the executor.
- There is an *action kind* for it: the manifest schema in `engine/manifest.py`
  **and** the executor's own `parse_manifest` (a test holds the two in
  agreement), a rollback strategy in `engine/rollback.py` with a **read-only
  verifier**, and the kind joins `REVERSIBLE_ACTION_KINDS` only once its
  inverse is itself a tested write path. A kind with no inverse takes the
  `no-inverse` strategy and can never run unattended.
- A manifest for it carries the target's identity and nothing else that
  chooses what runs; what runs is fixed in code, or a proposal becomes remote
  code execution gated by one trust cell.

The CONTRIBUTING guide walks the adding-an-action-kind sequence step by step.
The reason for all of it is one sentence from the
[trust gradient](trust-gradient.md): every write to the lab goes through one
gate, and an LLM is never in it.
