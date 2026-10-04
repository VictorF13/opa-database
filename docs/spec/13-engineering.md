# 13. Engineering

This document specifies how the code is organized, written, checked, and
documented. The workflow around it (branches, pull requests, continuous
integration, releases) is in [14-delivery.md](14-delivery.md).

## 1. Repository layout

```text
.
├── packages/
│   ├── opa-core/         configuration, identifiers, time, lake I/O,
│   │                     metadata client, check framework, test helpers
│   ├── opa-pipeline/     raw, bronze, silver, reference, gold, publish,
│   │                     checks, the command line interface
│   ├── opa-inference/    inference components, evaluation, training
│   └── opa-apps/         labeling and exploration applications
├── db/
│   ├── migrations/       numbered SQL migrations
│   ├── roles/            roles, grants, the expected access matrix
│   └── schema.sql        generated schema snapshot
├── ref/                  reference data and parameters
├── deploy/               compose file, PostgreSQL configuration,
│                         systemd units, backup configuration
├── docs/                 specification, decision records, guides,
│                         reference, runbooks
├── tests/                integration and end-to-end tests
├── notebooks/            exploration only
├── .github/              workflows, templates, code owners
├── pyproject.toml        workspace root and all tool configuration
├── uv.lock
├── requirements.txt      generated
├── requirements-dev.txt  generated
└── README.md, CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md,
    LICENSE, CLAUDE.md
```

Each package has the layout `src/<import_name>/` and `tests/`.

- **ENG-1 (MUST)** The repository is one uv workspace with the four
  packages above and a single lockfile.
- **ENG-2 (MUST)** Dependencies between packages point one way:
  `opa-pipeline`, `opa-inference`, and `opa-apps` depend on `opa-core`;
  `opa-pipeline` may depend on `opa-inference` to orchestrate it;
  nothing depends on `opa-apps`; `opa-core` depends on no other package.
  An automated check enforces this.
- **ENG-3 (MUST)** Every directory of Python code is a real package. No
  code manipulates the import path, and no two modules rely on being
  found by bare name.
- **ENG-4 (MUST)** Model artifacts, data files, and notebook outputs are
  never committed. Models live in the model store and data lives in the
  lake (see the layout in [02-architecture.md](02-architecture.md)).
- **ENG-5 (MUST)** The repository contains no real data (`SEC-25`).

## 2. Python and uv

- **ENG-10 (MUST)** Versions are pinned: the Python minor version in
  `.python-version`, every dependency in `uv.lock`, container images by
  digest, and tool versions in the lockfile and hook configuration.
- **ENG-11 (MUST)** uv is the only tool used to manage Python: installing
  the interpreter, creating the environment, adding and removing
  dependencies, locking, running, and building. pip, Poetry, Conda,
  pipx, and pyenv are not used. Commands are run as `uv run <command>`.
- **ENG-12 (MUST)** Dependencies are changed with `uv add` and
  `uv remove`, never by editing `pyproject.toml` by hand.
- **ENG-13 (MUST)** Development tools are in the `dev` dependency group.
  Each package declares only what it imports.
- **ENG-14 (MUST)** `requirements.txt` and `requirements-dev.txt` are
  generated from the lockfile by pre-commit hooks (`ENG-41`) and never
  edited.
- **ENG-15 (MUST)** Continuous integration installs with
  `uv sync --locked` and fails if the lockfile is out of date.
- **ENG-16 (MUST)** The Python version is the newest minor release
  supported by every runtime dependency at project start. It changes by
  decision record.
- **ENG-17 (MUST)** Package versions are derived from git tags by the
  build backend. No file stores a version number, and all packages of
  the workspace share the version of the repository (`DLV-44`).

## 3. Linting and formatting

- **ENG-20 (MUST)** Ruff is the only linter and formatter, at the current
  release, kept current by automated update pull requests (`DLV-34`).
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
  "**/tests/**" = [
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

- **ENG-25 (MUST)** ty is the type checker. It checks every package and
  every test, with no path excluded.
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

## 6. Tests

- **ENG-33 (MUST)** pytest is the test framework. `uv run pytest` runs
  the unit tests offline, with no database and no network, in under
  five minutes.
- **ENG-34 (MUST)** Tests are organized by scope and selected by marker:

| Marker | Scope | Needs | Runs in |
| --- | --- | --- | --- |
| (none) | Unit: one function or module | Nothing | Every pull request |
| `integration` | A step against PostgreSQL with PostGIS, or against a small lake | A database service | Every pull request |
| `e2e` | Raw files to published release on the synthetic world | A database service | Every pull request |
| `restricted` | Evaluation on real data and the frozen test set | The host | Before a data release |
| `bench` | Performance budgets | The host | Before a data release |

- **ENG-35 (MUST)** Test data is synthetic. A generator in `opa-core`
  builds a small "synthetic world": a network with a radial route, a
  circular route published as segments, and a short-turn variant; buses,
  devices, and a known assignment between them; and raw files in every
  real format, including a file that crosses midnight, a late dump, a
  duplicate, a truncated line, an unknown attribute, and an empty file.
  The truth is known, so tests assert that the pipeline recovers it.
- **ENG-36 (MUST)** The end-to-end test builds the synthetic world from
  raw files to a published release and asserts every accounting
  invariant (`DQ-10`) and the recovery of the known truth.
- **ENG-37 (MUST)** Every defect fixed gets a test that fails without the
  fix.
- **ENG-38 (MUST)** Line coverage of `opa-core`, `opa-pipeline`, and
  `opa-inference` is at least 85%, measured in continuous integration.
  Coverage never replaces the end-to-end test.
- **ENG-39 (SHOULD)** A test that verifies a requirement of this
  specification carries the requirement's identifier as a marker. A
  report lists requirements with no test.
- **ENG-40 (MUST)** Tests are deterministic: fixed seeds, no dependence
  on the clock, the locale, or test order.

## 7. Pre-commit hooks

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

## 8. Code conventions

- **ENG-44 (MUST)** Configuration is read through one typed settings
  object. Importing a module never requires configuration to be present
  and never touches the network, the database, or the lake.
- **ENG-45 (MUST)** Every parameter is read through the parameter
  accessor (`REF-20`).
- **ENG-46 (MUST)** Logging uses the standard logging module with
  structured fields. `print` is used only by the command line interface
  for its own output.
- **ENG-47 (MUST)** Errors are raised, not swallowed. Each package has a
  small exception hierarchy. A step that cannot do its job fails loudly
  and leaves no partial output (`ARC-12`).
- **ENG-48 (MUST)** SQL lives in `.sql` files or in clearly delimited
  constants, uses bound parameters for values and identifier quoting for
  names, lists columns explicitly, and never builds statements by string
  concatenation with data.
- **ENG-49 (MUST)** Notebooks are for exploration. They do not create or
  modify any table in a served schema, they connect with a read role,
  and they are committed without outputs.

## 9. Database schema

- **ENG-50 (MUST)** The serving database's structure (schemas, parent
  tables, enumerated types, views, comments, extensions) is defined by
  numbered, plain SQL migrations in `db/migrations/`, applied with
  `opa db migrate`. The publish step creates partitions and loads data;
  it does not define structure.
- **ENG-51 (MUST)** Continuous integration applies all migrations to an
  empty database and compares the result with the committed
  `db/schema.sql`. Migrations are forward-only once released, and follow
  expand-then-contract so that the previous code release still works
  against the new schema.

## 10. Documentation

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
| `docs/tutorials/` | Learning by doing: first query, first build |
| `docs/how-to/` | Task guides, including the query guide |
| `docs/reference/` | Data dictionary, command reference, parameter reference |
| `docs/explanation/` | Background: the problem, the methods, data governance |
| `docs/adr/` | Decision records |
| `docs/runbooks/` | Operational procedures |
| `docs/spec/` | This specification |

- **ENG-55 (MUST)** Continuous integration checks every link in the
  documentation.
- **ENG-56 (MUST)** The data dictionary is generated from the comments on
  tables and columns in the serving database (`GLD-8`) and from the
  `ref` vocabularies. It is never written by hand.
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
  and the license. The changelog is the list of GitHub Releases
  generated by the release automation (`DLV-45`).

## 11. Acceptance

A clean clone, with only uv installed, passes:

```bash
uv sync --locked
uv run prek run --all-files
uv run ruff format --check .
uv run ruff check .
uv run ty check
uv run pytest
```
