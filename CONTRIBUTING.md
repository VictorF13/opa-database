# Contributing

## Development setup

```bash
uv sync
cp .env.example .env
docker compose up -d
uv run prek install   # or: pre-commit install
```

See the [README](README.md) for environment variables and running the
pipeline end to end, and [`docs/architecture.md`](docs/architecture.md)
for how the codebase is organized.

## Dependencies

Manage dependencies through `uv`, not by hand-editing `pyproject.toml`:

```bash
uv add "some-package"          # runtime dependency
uv add --dev "some-package"    # dev-only dependency (linting, tests, tooling)
uv remove some-package
```

These update `pyproject.toml` and `uv.lock` together and keep them
consistent; the pre-commit hooks also re-export `requirements.txt` /
`requirements-dev.txt` from the lockfile, so those shouldn't be edited by
hand either.

## Running things

Always run Python through `uv run` (`uv run <script.py>`, `uv run pytest`,
`uv run opa-database ...`) rather than invoking `python`/`python3`
directly, so the command runs against this project's synced environment
and pinned Python version instead of whatever interpreter happens to be
on `PATH`.

## Before opening a PR

```bash
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest
uv run prek run --all-files
```

The first four are the same checks CI runs (`.github/workflows/ci.yml`).
`prek run --all-files` runs every configured pre-commit hook
(`.pre-commit-config.yaml`) against the whole repo, which is the reliable
way to know ahead of time whether pre-commit will pass on commit, rather
than finding out hook-by-hook as you commit.

## Docstrings

Every public module, class, and function gets a
[Google-style](https://google.github.io/styleguide/pyguide.html#38-comments-and-docstrings)
docstring: an imperative one-line summary, optionally a blank line and
more detail, then `Args:`/`Returns:`/`Raises:` sections as needed. For
example:

```python
"""Do something (imperative).

Longer description if needed.

Args:
    param_name (type): What it is.

Returns:
    type: What it is.

Raises:
    SomeError: When this happens.

"""
```

See `src/opa_database/config.py` or
`src/opa_database/loaders/silver.py::replace_period` for real examples in
this codebase. `ruff`'s pydocstyle rules (`D`, part of `lint.extend-select
= ["ALL"]` in `pyproject.toml`) enforce that public objects have a
docstring at all; following the Google section layout consistently is a
project convention on top of that.

## Git workflow

- `main`: production. Every commit on `main` is a released version.
- `develop`: integration branch. All feature/fix work branches off
  `develop` and comes back via PR into `develop`, never directly into
  `main`.

The end-to-end flow:

1. Branch off `develop`, do the work, open a PR back into `develop`.
   CI (`.github/workflows/ci.yml`) runs on the PR: `check-pr-title`
   validates the title against
   [Conventional Commits](https://www.conventionalcommits.org/)
   (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `perf:`, `test:`,
   `ci:`, `build:`, `style:`, `revert:`, optionally scoped like
   `feat(silver): ...`), `validate` runs formatting/lint/type checks, and
   `test` runs the pytest suite. All three must pass before merging.
2. Once merged into `develop`, that push triggers `prep-release-pr`:
   CI automatically opens (or updates, if one is already open) a
   `chore: release vX.Y.Z` PR from `develop` into `main`, with a preview
   of the commits it would include. This release PR is never opened by
   hand and keeps accumulating/updating itself as more PRs land on
   `develop`, so nothing needs to be done to maintain it.
3. When it's actually time to ship, merge that release PR into `main`.
   That merge triggers `automate-release`
   ([python-semantic-release](https://python-semantic-release.readthedocs.io/)),
   which bumps the version in `pyproject.toml`, updates `CHANGELOG.md`,
   tags the release, and publishes a GitHub release, all derived from the
   Conventional Commit history rather than written by hand. `develop` is
   then fast-forwarded to match `main` so both branches stay in sync.

So in short: PRs always target `develop`; `develop` -> `main` PRs are
release PRs, generated and updated automatically, and merging one of
those is the actual release action.

Use the [PR template](.github/PULL_REQUEST_TEMPLATE.md)'s checklist
before requesting review on a feature/fix PR.
