# Contributing

The standards of this project are specified in
[`docs/spec/13-engineering.md`](docs/spec/13-engineering.md) (code,
tests, documentation) and
[`docs/spec/14-delivery.md`](docs/spec/14-delivery.md) (branches,
commits, pull requests, releases). This page is the short version. Where
the two differ, the specification is right.

## Setup

The only prerequisite is [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run prek install
```

Do not start, stop, or recreate the database containers defined by the
root `docker-compose.yml` on the shared host. That database is in use.

## Rules

- uv is the only tool for Python: run everything with `uv run`, and
  change dependencies with `uv add`, `uv add --dev`, and `uv remove`.
  `requirements.txt` and `requirements-dev.txt` are generated; do not
  edit them.
- Ruff lints and formats, with every rule enabled. ty checks types.
  pytest runs the tests.
- Every public module, class, and function has a Google-style docstring:

  ```python
  """Do something (imperative summary).

  Longer description when needed.

  Args:
      name (type): What it is.

  Returns:
      type: What it is.

  Raises:
      SomeError: When this happens.

  """
  ```

- New behavior comes with tests. A fixed defect comes with a test that
  fails without the fix.

## Before opening a pull request

```bash
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest
uv run prek run --all-files
```

`prek` only checks files that git tracks, so `git add` new files first.

## Workflow

1. Branch from `develop`, named `<type>/<short-description>`.
2. Open a pull request into `develop`. Its title is a single-line
   conventional commit with no scope, for example
   `feat: add tap position evidence`. The allowed types are `build`,
   `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`,
   `style`, and `test`. A `!` after the type marks a breaking change.
3. Pull requests into `develop` are squash-merged: one commit per change,
   named by the pull request title.
4. Automation keeps a release pull request open from `develop` into
   `main`, with the next version in its title and the changes so far in
   its description.
5. Merging the release pull request is the release. It is merged with a
   merge commit, never squashed and never rebased. `main` is the
   production branch: every commit on it is a release.

Until the first phase of the roadmap puts every rule of the
specification into continuous integration and repository settings,
follow the rules above even where the automation is still more lenient.
