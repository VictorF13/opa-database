# 13. Engineering

This document specifies how the code is organized, written, checked, and
documented. The workflow around it (branches, pull requests, continuous
integration, releases) is in [14-delivery.md](14-delivery.md).

## 1. Repository layout

```text
.
├── src/opa/
│   ├── core/         configuration, parameters, identifiers, time,
│   │                 access to the lake
│   ├── ingest/       raw inventory and fetch, bronze parsers
│   ├── inference/    Python inference components, evaluation, training
│   ├── publish/      loading a release into the serving database
│   ├── defs/         orchestrator definitions: assets, checks,
│   │                 schedules, sensors
│   ├── apps/         labeling and exploration applications
│   ├── cli/          the `opa` command
│   └── testing/      the synthetic world generator
├── dbt/              the dbt project
│   ├── models/       silver/, inference/, gold/, meta/
│   ├── seeds/        reference data
│   ├── macros/       shared SQL: identifiers, canonical keys, flags
│   └── tests/        tests that are not attached to one model
├── db/
│   ├── migrations/   numbered SQL migrations of the serving database
│   ├── roles/        roles, grants, the expected access matrix
│   └── schema.sql    generated schema snapshot
├── config/           parameters.toml
├── deploy/           Dockerfile, compose file, PostgreSQL and
│                     orchestrator configuration, systemd units, backup
│                     configuration, protected resources
├── docs/             specification, decision records, guides,
│                     reference, runbooks
├── tests/            unit/, integration/, e2e/
├── notebooks/        exploration only
├── .github/          workflows, templates, code owners
├── pyproject.toml    the project and all tool configuration
├── uv.lock
├── requirements.txt      generated
├── requirements-dev.txt  generated
└── README.md, CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md,
    LICENSE, CHANGELOG.md, CLAUDE.md
```

- **ENG-1 (MUST)** The repository holds one Python package (`opa`) and one
  dbt project (`dbt/`). There is one lockfile and one version.
- **ENG-2 (MUST)** Dependencies inside the package point one way. `core`
  imports no other part. `ingest`, `inference`, `publish`, and `apps`
  import `core` only. `defs` and `cli` may import any of them. Nothing
  imports `defs`, `cli`, or `apps`. An automated check enforces this.
- **ENG-3 (MUST)** No code manipulates the import path, and no module
  relies on being found by bare name.
- **ENG-4 (MUST)** Model artifacts, data files, and notebook outputs are
  never committed. Models live in the model store and data lives in the
  lake (see the layout in [03-platform.md](03-platform.md)).
- **ENG-5 (MUST)** The repository contains no real data (`SEC-25`).

## 2. Python and uv

- **ENG-10 (MUST)** Versions are pinned: the Python minor version in
  `.python-version`, every dependency in `uv.lock`, dbt packages in their
  lock file, container images by digest, and tool versions in the
  lockfile and hook configuration.
- **ENG-11 (MUST)** uv is the only tool used to manage Python: installing
  the interpreter, creating the environment, adding and removing
  dependencies, locking, running, and building. pip, Poetry, Conda,
  pipx, and pyenv are not used. Commands are run as `uv run <command>`,
  including `uv run dbt` and `uv run dagster`.
- **ENG-12 (MUST)** Dependencies are changed with `uv add` and
  `uv remove`, never by editing `pyproject.toml` by hand.
- **ENG-13 (MUST)** Development tools are in the `dev` dependency group.
  What only the applications need is an optional extra.
- **ENG-14 (MUST)** `requirements.txt` and `requirements-dev.txt` are
  generated from the lockfile by pre-commit hooks (`ENG-41`) and never
  edited.
- **ENG-15 (MUST)** Continuous integration installs with
  `uv sync --locked` and fails if the lockfile is out of date.
- **ENG-16 (MUST)** The Python version is the newest minor release
  supported by every runtime dependency at project start. It changes by
  decision record.
- **ENG-17 (MUST)** The project has one version, stored in
  `pyproject.toml`. It is changed only by the release workflow
  (`DLV-43`), never by hand.

## 3. Linting and formatting

- **ENG-20 (MUST)** Ruff is the only Python linter and formatter, at the
  current release, kept current by automated update pull requests
  (`DLV-34`).
- **ENG-21 (MUST)** All rules are enabled. The only rules ignored are
  those that Ruff documents as conflicting with another enabled rule or
  with the formatter:

  ```toml
  [tool.ruff.lint]
  select = ["ALL"]
  ignore = [
      "D203",   # conflicts with D211
      "D213",   # conflicts with D212
      "COM812", # conflicts with the formatter
  ]

  [tool.ruff.lint.pydocstyle]
  convention = "google"
  ```

  The ignore list is revalidated against Ruff's documentation on every
  upgrade.
- **ENG-22 (MUST)** The line length is Ruff's default. It is not
  configured.
- **ENG-23 (MUST)** Per-file exceptions exist only for test code, each
  with its reason:

  ```toml
  [tool.ruff.lint.per-file-ignores]
  "tests/**" = [
      "S101",    # assert is how pytest tests are written
      "D1",      # tests are not public API and need no docstrings
      "SLF001",  # tests may reach into private members
      "PLR2004", # literal expected values are clearer in assertions
  ]
  ```

- **ENG-24 (MUST)** A suppression in code (`# noqa`) names the rule and
  gives the reason on the same line. Any other change to the rule set
  needs a decision record.

## 4. Type checking

- **ENG-25 (MUST)** ty is the type checker. It checks the whole package
  and every test, with no path excluded.
- **ENG-26 (MUST)** All rules are active at their default level, and
  every rule that the pinned ty release disables by default is raised to
  `warn`. When the convention was set these were three rules:

  ```toml
  [tool.ty.rules]
  division-by-zero = "warn"
  possibly-unresolved-reference = "warn"
  unused-ignore-comment = "warn"
  ```

  The list is re-derived from ty's rule reference on every upgrade, so
  that no rule is ever left at `ignore`.
- **ENG-27 (MUST)** Every function has complete type annotations. A
  suppression names the rule and gives the reason.

## 5. Docstrings

- **ENG-30 (MUST)** Every public module, class, and function has a
  docstring in Google style. Private objects (names starting with an
  underscore) and tests do not need one.
- **ENG-31 (MUST)** The form is: a one-line summary in the imperative
  mood; an optional longer description; then `Args:`, `Returns:`, and
  `Raises:` sections as relevant. Each argument is written
  `name (type): description`, and the return value `type: description`.

  ```python
  def link_devices(day: date, *, min_confidence: float = 0.95) -> LinkResult:
      """Link buses to devices for one operational day.

      Combines every evidence family that applies to each bus and solves
      the assignment jointly for the whole day.

      Args:
          day (date): Operational day to process.
          min_confidence (float): Lowest confidence at which a link is
              accepted.

      Returns:
          LinkResult: Accepted links with the evidence behind each.

      Raises:
          MissingInputError: If silver data for the day is not built.

      """
  ```

- **ENG-32 (MUST)** Comments explain why, not what, and not how the
  author found out. History belongs in commit messages and decision
  records.

## 6. dbt

- **ENG-60 (MUST)** The dbt project follows dbt's documented project
  conventions: one model per file, one directory per layer, properties
  files next to the models they describe, shared logic in macros.
- **ENG-61 (MUST)** Every model, column, seed, and source has a
  description. These descriptions are the data dictionary (`ENG-56`). A
  check in continuous integration fails on a missing description.
- **ENG-62 (MUST)** Every model in silver and gold has an enforced
  contract. Every model has tests for its primary key (unique and not
  null) and for every reference and enumerated column it holds.
- **ENG-63 (MUST)** Transformation logic with more than one case
  (parsing, flagging, deduplication, classification) is covered by dbt
  unit tests with small, explicit inputs and expected outputs.
- **ENG-64 (MUST)** SQL is linted with SQLFluff using the dbt templater
  and the DuckDB dialect. Keywords are lower case, columns are listed
  explicitly, and logic is built from named common table expressions.
- **ENG-65 (MUST)** Tables written by Python are declared as dbt sources
  with the same kinds of tests, so every lake table is tested the same
  way (`DQ-15`).
- **ENG-66 (MUST)** The project uses the current stable line of dbt Core
  with the DuckDB adapter. Moving to a new major version of dbt is a
  decision record, taken once the adapter supports it.

## 7. Orchestrator definitions

- **ENG-70 (MUST)** Definitions follow the orchestrator's standard
  project layout. dbt models are loaded as assets through the
  orchestrator's dbt integration, not redeclared by hand.
- **ENG-71 (MUST)** Every asset declares its partitioning, its upstream
  assets, its code version (`ARC-53`), an owner, and a description.
- **ENG-72 (MUST)** Continuous integration loads the full set of
  definitions and fails on any error, so a broken graph is never merged.

## 8. Tests

- **ENG-33 (MUST)** pytest is the test framework for Python. `uv run
  pytest` runs the unit tests offline, with no database and no network,
  in under five minutes.
- **ENG-34 (MUST)** Tests are organized by scope:

| Kind | Scope | Needs | Runs in |
| --- | --- | --- | --- |
| Python unit | One function or module | Nothing | Every pull request |
| dbt unit | One model's logic on explicit inputs | Nothing | Every pull request |
| `integration` | A step against PostgreSQL with PostGIS and a small lake | A database service | Every pull request |
| `e2e` | Raw files to published release on the synthetic world, run through the orchestrator | A database service | Every pull request |
| `restricted` | Evaluation on real data and the frozen test set | The host | Before a data release |
| `bench` | Performance budgets | The host | Before a data release |

- **ENG-35 (MUST)** Test data is synthetic. A generator in `opa.testing`
  builds a small "synthetic world": a network with a radial route, a
  circular route published as segments, and a short-turn variant; buses,
  devices, and a known assignment between them; and raw files in every
  real format, including a file that crosses midnight, a late dump, a
  duplicate, a truncated line, an unknown attribute, and an empty file.
  The truth is known, so tests assert that the pipeline recovers it.
- **ENG-36 (MUST)** The end-to-end test materializes the synthetic world
  from raw files to a published release through the orchestrator, and
  asserts every accounting invariant (`DQ-10`) and the recovery of the
  known truth.
- **ENG-37 (MUST)** Every defect fixed gets a test that fails without the
  fix.
- **ENG-38 (MUST)** Line coverage of `core`, `ingest`, `inference`, and
  `publish` is at least 85%, measured in continuous integration.
  Coverage never replaces the end-to-end test.
- **ENG-39 (SHOULD)** A test that verifies a requirement of this
  specification carries the requirement's identifier. A report lists
  requirements with no test.
- **ENG-40 (MUST)** Tests are deterministic: fixed seeds, no dependence
  on the clock, the locale, or test order.

## 9. Pre-commit hooks

- **ENG-41 (MUST)** Hooks are run by prek from
  `.pre-commit-config.yaml`. The hook set is the canonical hooks
  published by Astral for uv, Ruff, and ty:

  ```yaml
  repos:
    - repo: https://github.com/astral-sh/uv-pre-commit
      rev: <pinned>
      hooks:
        - id: uv-lock
        - id: uv-export
          name: export runtime requirements
          args: [--no-dev, --output-file, requirements.txt]
        - id: uv-export
          name: export development requirements
          args: [--only-group, dev, --output-file, requirements-dev.txt]
    - repo: https://github.com/astral-sh/ruff-pre-commit
      rev: <pinned>
      hooks:
        - id: ruff-check
          args: [--fix]
        - id: ruff-format
    - repo: https://github.com/astral-sh/ty-pre-commit
      rev: <pinned>
      hooks:
        - id: ty
  ```

- **ENG-42 (MUST)** Hook versions match the versions in the lockfile.
- **ENG-43 (MUST)** `uv run prek run --all-files` passes before any pull
  request is opened. Because hooks see only tracked files, new files are
  added to the index first.

## 10. Code conventions

- **ENG-44 (MUST)** Configuration is read through one typed settings
  object. Importing a module never requires configuration to be present
  and never touches the network, a database, or the lake.
- **ENG-45 (MUST)** Every parameter is read through the parameter
  accessor (`REF-20`).
- **ENG-46 (MUST)** Logging uses the standard logging module with
  structured fields. `print` is used only by the command line interface
  for its own output.
- **ENG-47 (MUST)** Errors are raised, not swallowed. A step that cannot
  do its job fails loudly and leaves no partial output (`ARC-12`).
- **ENG-48 (MUST)** Transformation SQL lives in dbt models. SQL elsewhere
  (publishing, operations) uses bound parameters for values and
  identifier quoting for names, and never builds statements by string
  concatenation with data.
- **ENG-49 (MUST)** Notebooks are for exploration. They do not create or
  modify any table in the lake or in a served schema, they connect with
  a read role, and they are committed without outputs.

## 11. Serving database schema

- **ENG-50 (MUST)** The structure of the serving database (schemas,
  parent tables, enumerated types, views, extensions) is defined by
  numbered, plain SQL migrations in `db/migrations/`, applied with
  `opa db migrate`. Publishing creates partitions and loads data; it
  does not define structure. The structure of lake tables is defined by
  the dbt models and Python table definitions that write them.
- **ENG-51 (MUST)** Continuous integration applies all migrations to an
  empty database and compares the result with the committed
  `db/schema.sql`. It also checks that every served table matches the
  contract of the lake table it copies. Migrations are forward-only once
  released, and follow expand-then-contract so that the previous code
  release still works against the new schema.

## 12. Documentation

- **ENG-52 (MUST)** Decisions are recorded in `docs/adr/` as numbered
  decision records in the MADR format: context, options considered,
  decision, consequences.
- **ENG-53 (MUST)** Documentation is GitHub Flavored Markdown, checked by
  markdownlint with its default rules. The only adjustment is that the
  line-length rule does not apply to tables and code blocks. Prose is
  wrapped at 80 columns. Each file has exactly one top-level heading,
  headings are in sentence case, code blocks state their language, and
  links between documents are relative.
- **ENG-54 (MUST)** `docs/` follows the Diataxis structure, plus the
  project's own records:

| Directory | Content |
| --- | --- |
| `docs/tutorials/` | Learning by doing: first query, first run |
| `docs/how-to/` | Task guides, including the query guide |
| `docs/reference/` | Command reference, parameter reference, raw inventory |
| `docs/explanation/` | Background: the problem, the methods, data governance |
| `docs/adr/` | Decision records |
| `docs/runbooks/` | Operational procedures |
| `docs/spec/` | This specification |

- **ENG-55 (MUST)** Continuous integration checks every link in the
  documentation.
- **ENG-56 (MUST)** The data dictionary and the table lineage are dbt's
  generated documentation, built in continuous integration from the
  descriptions of `ENG-61` and published as a static site. It contains
  definitions only, never data. The same descriptions are written as
  comments on the served tables at publish time (`GLD-8`).
- **ENG-57 (MUST)** A query guide shows the efficient form of each
  workload pattern (`10-performance.md`, section 7) with runnable
  examples.
- **ENG-58 (MUST)** `CLAUDE.md` gives coding agents what they need: the
  exact commands, the layer rules, the gotchas, and where the
  specification is. The command reference is generated from the command
  line interface, and a test fails when a documented command does not
  exist.
- **ENG-59 (MUST)** The root `README.md` states what the project is, how
  to install and run it, where the documentation is, how to contribute,
  and the license. `CHANGELOG.md` follows the Keep a Changelog format
  and is written only by the release workflow (`DLV-44`).

## 13. Acceptance

A clean clone, with only uv installed, passes:

```bash
uv sync --locked
uv run prek run --all-files
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run sqlfluff lint dbt
uv run pytest
```
