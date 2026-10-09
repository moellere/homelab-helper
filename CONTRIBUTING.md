# Contributing to homelab-helper

Thanks for looking. This is a small project with a strong opinion about one
thing: **the framework never changes your lab without a deterministic policy
and, where that policy says so, a human saying yes.** Most of the guidance
below exists to keep that true as the code grows.

## Before you start

- `uv sync --all-extras --group dev --group docs`, then `uv run ruff check src tests`,
  `uv run ruff format --check src tests`, `uv run mypy src`, `uv run pytest -q`,
  `uv run mkdocs build --strict`. CI runs exactly these five; a red one blocks
  merge.
- Read `CLAUDE.md` (the invariants, and the test patterns), then the part of
  `docs/architecture.md` your change touches. The architecture doc records
  *locked* decisions; propose changing one in an issue first.
- The test suite is all mocks. Nothing you run locally talks to a lab unless
  you point it at one on purpose (`docs/live-validation.md`).

## Pull requests

- One unit of work per branch, cut fresh from `main`. Squash-merge is the
  norm, so a branch's history does not need to be pretty.
- Fork PRs are welcome. If GitHub refuses your PR, open an issue with a link
  to the branch on your fork and the commit; that is how #43 landed.
- Every PR that changes behaviour updates the docs in the same PR: the docs
  site page an operator would read (`docs/getting-started.md`,
  `docs/trust-gradient.md`, …) and `README.md`, `docs/architecture.md` for
  decisions, `docs/backlog.md` for what is now done or newly owed, `CLAUDE.md`
  for invariants and patterns a future session must know. Doc-only PRs are
  fine too; `uv run mkdocs serve` previews the site.
- Say in the PR body what you ran. "All five checks pass on Python 3.12" is
  the expected line.
- Use conventional, descriptive commit titles. We do not require a prefix.

## The three invariants a change must not break

1. **An LLM never authorizes.** `engine/trust.py::decide()` is pure Python and
   two subprocess tests assert it never imports `homelab_helper.llm`. Agents
   may draft (`propose_*`) and, since Phase 7, trigger (`execute_proposal`);
   the executor and the human still decide.
2. **Every write goes through the executor.** Adapter mutate methods carry an
   "executor-only" block comment, and `tests/test_write_isolation.py` fails if
   any other module names one. Add your new write method to its list.
3. **Reversibility is verified, never claimed.** A manifest may *ask* for a
   rollback strategy; `engine/rollback.py` probes the target to decide whether
   it is real, and an unverifiable action never runs unattended.

## Adding an action kind (the usual shape of a Phase-7 contribution)

1. Manifest: the authoring schema in `engine/manifest.py` **and** the hand
   validator `parse_manifest` in `engine/executor.py` (a test holds them in
   agreement).
2. Adapter write method, marked executor-only, plus a read-only probe the
   rollback verifier can use.
3. Rollback strategy: verify (read-only), capture, restore.
4. `REVERSIBLE_ACTION_KINDS` in `engine/escalation.py` only once that inverse
   is a tested write path.
5. Tests against fakes (`httpx.MockTransport`, an injected `kubectl` runner),
   a line in `README.md`'s write-surface list, a backlog entry.

## Reporting a bug

A failing test is the best bug report. `tests/test_virt_reconcile.py` and
issue #43 are a good model: a minimal fixture, the expected result, the
actual one.

## Agents contributing

Claude Code sessions (the maintainer's and contributors') work in this repo.
`CLAUDE.md` is written for them, and the same rules apply: no secrets in
commits, no PR opened without a human asking, nothing executed against a lab
without the trust gradient saying so.
