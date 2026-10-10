# Releasing

Releases are tag-driven. Pushing a `v*` tag runs
`.github/workflows/release.yml`, which re-runs the CI gate on the tagged
commit, builds the sdist and wheel, smoke-tests the wheel from an empty
directory, publishes to PyPI, and creates a GitHub release with the
artifacts attached and notes generated from the merged PRs.

## One-time setup (before the first tag)

1. **PyPI trusted publisher.** On PyPI, create the project `homelab-helper`
   (or reserve it via "Add a pending publisher") and register a trusted
   publisher with owner `moellere`, repository `homelab-helper`, workflow
   `release.yml`, environment `pypi`. No API token is stored anywhere.
2. **GitHub environment.** Under the repo's Settings → Environments, create
   `pypi` and add yourself as a **required reviewer**. The workflow then stops
   at the `publish-pypi` job until you approve it, so whoever pushes the tag
   — a person or an agent acting on their instruction — cannot reach PyPI
   alone. The gate, build and smoke test have already run by then.
3. **Tag protection** (optional). Restrict who can push `v*` tags.

## Cutting a release

```bash
# on main, green CI
uv version 1.0.0rc1                # bumps pyproject.toml; pick the next version
git commit -am "Release 1.0.0rc1"  # on a branch — main takes PRs only
git push origin main               # after the squash-merge, let CI pass on the release commit
git tag v1.0.0rc1
git push origin v1.0.0rc1          # triggers the release workflow
```

The workflow refuses a tag that doesn't match `pyproject.toml`'s version, so
a typo in the tag fails fast instead of publishing a mislabelled build. A tag
is never moved or deleted: a bad release is followed by the next version.

An agent session may do all of this on the operator's say-so — the version
bump PR, the tag, the push — under the rule in `CLAUDE.md` ("Releases"); the
required reviewer on the `pypi` environment is where the operator's own click
stays in the loop.

Versions follow PEP 440. Pre-releases (`a`, `b`, `rc`) are marked as
pre-releases on GitHub; `pip`/`uv` won't install them unless asked
(`uv tool install --prerelease allow homelab-helper`, or pin the version).

## What users get

```bash
uv tool install --prerelease allow homelab-helper   # while only pre-releases exist
# or: pipx install homelab-helper==<version>
helper --install-completion
helper config init && helper db init
```

`helper version` prints the installed version, which is what to ask for in
bug reports.
