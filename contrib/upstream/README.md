# Upstream contributions: Google Weather minute forecast

Ready-to-submit patches for Track A of
`docs/google-weather-ha-integration-proposal.md`. This Claude session could
not open the upstream PRs itself (GitHub access is scoped to this repo and
no forks of the upstream repos exist under this account), so each PR is a
fork + `git am` + push away.

Both patches were built and verified in-session on 2026-08-10:

- library: `ruff check` / `ruff format --check` / `mypy src` (strict) /
  `pytest` all green against `tronikos/python-google-weather-api@733a65d`.
- core: `ruff@0.16.0` (core's pinned version) check + format clean against
  `home-assistant/core` `dev`; service response shape verified against the
  patched library; full test suite left to CI.

Commits carry the session's git identity — run
`git commit --amend --reset-author` after `git am` to submit under your own.

## 1. Library PR (submit first)

```sh
# Fork https://github.com/tronikos/python-google-weather-api on GitHub, then:
git clone git@github.com:moellere/python-google-weather-api
cd python-google-weather-api
git checkout -b minute-forecast
git am path/to/python-google-weather-api-minute-forecast.patch
git push -u origin minute-forecast
```

Open the PR against `tronikos/python-google-weather-api` `main` with the body
in `pr-body-library.md`.

## 2. Core PR (open as draft after the library PR lands + releases)

The patch pins `python-google-weather-api==0.0.7` in `manifest.json` and
`requirements_all.txt`; if the actual release number differs, fix both before
pushing. If `requirements_all.txt` drifted, run
`python -m script.gen_requirements_all` instead of hand-editing.

```sh
# Fork https://github.com/home-assistant/core on GitHub, then:
git clone git@github.com:moellere/core ha-core && cd ha-core
git checkout -b google-weather-minute-forecast dev
git am path/to/ha-core-google-weather-minute-forecast-service.patch
git push -u origin google-weather-minute-forecast
```

Open a **draft** PR against `home-assistant/core` `dev` with the body in
`pr-body-core.md` (fill in the library-PR and docs-PR links). A small
home-assistant.io docs PR documenting the new action is required for the
`docs-actions` quality-scale entry.

## Contents

| File | What |
|---|---|
| `python-google-weather-api-minute-forecast.patch` | models + `async_get_minute_forecast` + tests |
| `ha-core-google-weather-minute-forecast-service.patch` | `get_minute_forecast` entity service, strings/icons/services.yaml, quality-scale updates, tests + fixture |
| `pr-body-library.md` | prefilled PR body for the library PR |
| `pr-body-core.md` | prefilled PR body for the core draft PR |
