# 14. Delivery

This document specifies how changes move from an idea to code running on
the host: branches, commits, pull requests, issues, continuous
integration, releases, and deployment. Wherever a widely used standard
exists, it is adopted as is.

## 1. Branches

- **DLV-1 (MUST)** `develop` is the default branch and the only
  integration branch. Every change starts as a branch from `develop` and
  returns to it through a pull request.
- **DLV-2 (MUST)** `main` always points at the latest release. It moves
  only by fast-forward, performed by the release workflow. Nobody commits
  to it or opens pull requests against it.
- **DLV-3 (MUST)** `develop` is always releasable. Work that is not ready
  for users is switched off by configuration, not parked on a long-lived
  branch.
- **DLV-4 (MUST)** Branches are named `<type>/<short-description>`, using
  the commit types of `DLV-11` (for example `feat/tap-linkage`). They are
  short-lived and deleted on merge.

This is trunk-based development with `develop` as the trunk and `main` as
a stable pointer for deployment. History stays linear, and the two
branches can never diverge, so no back-merge is ever needed.

## 2. Commits

- **DLV-10 (MUST)** Commit messages follow Conventional Commits 1.0.0,
  restricted to a single line with no scope and no body:

  ```text
  <type>: <description>
  <type>!: <description>      (breaking change)
  ```

- **DLV-11 (MUST)** Types and their effect on the version:

| Type | Use | Version effect |
| --- | --- | --- |
| `feat` | A new capability | Minor |
| `fix` | A defect correction | Patch |
| `perf` | A performance improvement | Patch |
| `refactor` | A change with no behavior change | None |
| `docs` | Documentation only | None |
| `test` | Tests only | None |
| `build` | Build system or dependencies | None |
| `ci` | Continuous integration | None |
| `style` | Formatting only | None |
| `chore` | Maintenance | None |
| `revert` | Reverting a commit | As the reverted commit |

  A `!` after the type marks a breaking change and raises the major
  version.
- **DLV-12 (MUST)** The description is in the imperative mood, starts in
  lower case, has no trailing period, and the whole line is at most 72
  characters.
- **DLV-13 (MUST)** The rule is enforced where it matters: on the pull
  request title, which becomes the commit on `develop` (`DLV-16`). The
  pattern is:

  ```text
  ^(build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)!?: [a-z0-9].{1,68}[^.\s]$
  ```

## 3. Pull requests

- **DLV-15 (MUST)** Every change reaches `develop` through a pull
  request, including changes by the owner.
- **DLV-16 (MUST)** Pull requests are merged by squash, and only by
  squash. Each pull request becomes exactly one commit whose message is
  the pull request title. Merge commits and rebase merges are disabled.

  Rationale: one commit per change gives a linear, readable history, a
  changelog that can be generated mechanically, and trivial reverts.
- **DLV-17 (MUST)** A pull request can merge only when all required
  checks pass (`DLV-31`), its branch is up to date with `develop`, and
  all review conversations are resolved.
- **DLV-18 (MUST)** A pull request is small and does one thing. It links
  the issue it resolves and names the specification requirements it
  implements.
- **DLV-19 (MUST)** When the project has two or more maintainers, one
  approving review from a code owner is required. With a single
  maintainer the review requirement is zero, and the pull request
  checklist stands in for it.
- **DLV-20 (MUST)** `.github/PULL_REQUEST_TEMPLATE.md` is:

  ```markdown
  ## Summary

  <!-- What does this change, and why? -->

  ## Related issues

  <!-- Closes #123 -->

  ## How was this tested?

  <!-- Commands run, tests added, evidence. -->

  ## Checklist

  - [ ] The title is `type: description`, one line, no scope
  - [ ] `uv run prek run --all-files` passes
  - [ ] Tests are added or updated
  - [ ] Documentation and specification are updated where behavior changed
  - [ ] No secrets and no real data are included
  ```

## 4. Issues

- **DLV-22 (MUST)** Issues are created from forms in
  `.github/ISSUE_TEMPLATE/`. Blank issues are disabled.

| Form | For | Fields |
| --- | --- | --- |
| `bug_report.yml` | Something behaves incorrectly | What happened, what was expected, steps to reproduce, version, logs |
| `feature_request.yml` | A new capability | Problem, proposed solution, alternatives |
| `data_quality.yml` | Data that looks wrong | Release, table, identifiers, period, what was expected, evidence |
| `config.yml` | Form configuration | Blank issues off; link to the security policy |

- **DLV-23 (MUST)** Labels: `bug`, `enhancement`, `data-quality`,
  `documentation`, `infrastructure`, `inference`, `performance`,
  `security`, plus `good first issue` and `help wanted`. Each form
  applies its label.
- **DLV-24 (MUST)** Roadmap phases are milestones. An issue names the
  requirement identifiers it concerns. A pull request closes its issue
  with a closing keyword.

## 5. Repository protection

- **DLV-25 (MUST)** A ruleset on `develop` requires: a pull request; the
  required status checks; an up-to-date branch; resolved conversations;
  linear history; no force pushes; no deletion.
- **DLV-26 (MUST)** A ruleset on `main` allows updates only from the
  release workflow, fast-forward only, with no force pushes and no
  deletion.
- **DLV-27 (MUST)** Tags matching `v*` can be created only by the release
  workflow and can never be moved or deleted.
- **DLV-28 (MUST)** Repository settings: default branch `develop`; squash
  merging only; squash commit title taken from the pull request title
  with an empty body; head branches deleted automatically on merge.
- **DLV-29 (MUST)** `.github/CODEOWNERS` names an owner for every path.

## 6. Continuous integration

- **DLV-30 (MUST)** One workflow runs on every pull request and on every
  push to `develop`. Its jobs run the same commands a developer runs
  locally:

| Job | What it runs |
| --- | --- |
| `title` | The pull request title against `DLV-13` |
| `format` | `uv run ruff format --check .` |
| `lint` | `uv run ruff check .` |
| `types` | `uv run ty check` |
| `test` | `uv run pytest` with coverage |
| `integration` | Integration and end-to-end tests against a PostgreSQL and PostGIS service |
| `database` | All migrations on an empty database, schema snapshot comparison, roles applied, access verified |
| `docs` | markdownlint, link check, reference data validation |
| `lock` | `uv lock --check`, and generated requirement files are current |
| `build` | `uv build` for every package |

- **DLV-31 (MUST)** Every job in `DLV-30` is a required status check.
- **DLV-32 (MUST)** Workflow hygiene: third-party actions are pinned by
  commit; each job declares the minimum permissions it needs; runs for a
  superseded commit are cancelled; the uv cache is used; pull requests
  from forks receive no secrets.
- **DLV-33 (MUST)** The pull request checks finish within 10 minutes.
- **DLV-34 (MUST)** Dependency updates are automated: weekly, grouped
  pull requests for Python dependencies, workflow actions, container
  images, and pre-commit hook versions.
- **DLV-35 (SHOULD)** Security checks run on a schedule: a dependency
  vulnerability audit, the hosting platform's secret scanning with push
  protection, and static analysis.

## 7. Releases

Code releases and data releases are different things. A code release is a
version of the software. A data release (`DQ-25`) is a version of the
published data, and records which code release produced it.

- **DLV-40 (MUST)** Code versions follow Semantic Versioning. Tags are
  `v<major>.<minor>.<patch>`. All packages share one version.
- **DLV-41 (MUST)** Release automation (release-please in manifest mode,
  or an equivalent that meets this section) maintains a release pull
  request against `develop`. Every merge to `develop` creates or updates
  it. It contains the version change in every place the version is
  written, including the lockfile, and the new `CHANGELOG.md` section
  generated from commit messages. Its title is
  `chore: release <version>`.
- **DLV-42 (MUST)** Merging the release pull request is the act of
  releasing. The automation then creates the tag and a GitHub Release
  with the changelog section as its notes.
- **DLV-43 (MUST)** On release, a workflow builds the packages with
  `uv build`, attaches them to the GitHub Release, and fast-forwards
  `main` to the tag.
- **DLV-44 (MUST)** A release is cut deliberately by a person merging the
  release pull request. Nothing is released as a side effect of merging
  a feature.
- **DLV-45 (MUST)** The version and changelog are never edited by hand.

## 8. Deployment

Deployment installs a released code version on the host. The host is
reachable only over a private network, so deployment is pulled by the
host, never pushed by continuous integration.

- **DLV-50 (MUST)** `opa ops deploy <version>` performs a deployment:
  check out the tag; `uv sync --locked --no-dev`; apply database
  migrations; apply roles and verify access; restart the application
  services; run smoke checks; record the deployment in
  `meta.deployment`.
- **DLV-51 (MUST)** The host runs only tagged releases. A timer notices a
  new release and notifies the operator; the operator runs the
  deployment.
- **DLV-52 (MUST)** Deploying code never rebuilds or republishes data on
  its own. Data changes only through builds and releases.
- **DLV-53 (MUST)** Rollback is deploying the previous version. Database
  migrations follow expand-then-contract (`ENG-51`) so the previous
  version runs against the current schema.
- **DLV-54 (MUST)** A deployment that fails its smoke checks rolls back
  automatically and reports the failure.

## 9. Community files

- **DLV-60 (MUST)** The repository satisfies the hosting platform's
  community standards: `README.md`, `LICENSE`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md` (Contributor Covenant), `SECURITY.md` (how to
  report a vulnerability privately), issue forms, and a pull request
  template.
- **DLV-61 (MUST)** `CONTRIBUTING.md` restates, briefly and with
  commands, sections 1 to 3 of this document and the acceptance commands
  of [13-engineering.md](13-engineering.md).

## 10. Acceptance

1. A pull request with a non-conforming title, a failing test, or a
   stale branch cannot be merged.
2. A direct push to `develop` or `main` is rejected.
3. Merging a `feat` pull request updates the release pull request with a
   minor version and a changelog entry.
4. Merging the release pull request produces a tag, a GitHub Release with
   built packages, and `main` equal to the tag.
5. `opa ops deploy` installs that release on the host and records it.
