# 14. Delivery

This document specifies how changes move from an idea to code running on
the host: branches, commits, pull requests, issues, continuous
integration, releases, and deployment. Wherever a widely used standard
exists, it is adopted as is.

## 1. Branches

- **DLV-1 (MUST)** `develop` is the default branch and the only
  integration branch. Every change starts as a branch from `develop` and
  returns to it through a pull request.
- **DLV-2 (MUST)** `main` is the production branch. Every commit on it is
  a release. It changes only when the release pull request from `develop`
  is merged, and by the release commit that the automation adds right
  after (section 7). Nobody else commits to it, and no branch other than
  `develop` is ever merged into it.
- **DLV-3 (MUST)** `develop` is always releasable. Work that is not ready
  for users is switched off by configuration, not parked on a long-lived
  branch.
- **DLV-4 (MUST)** Branches are named `<type>/<short-description>`, using
  the commit types of `DLV-11` (for example `feat/tap-linkage`). They are
  short-lived and deleted on merge.

This is the two-branch core of Git Flow: an integration branch, and a
production branch on which every commit is a release. Git Flow's release
and hotfix branches are not used, because `DLV-3` makes them unnecessary:
a fix is released by merging it into `develop` and merging the release
pull request. As in Git Flow, `main` is merged back into `develop` after
every release, so `develop` always contains everything that `main` does.

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
  request title, which becomes the commit (`DLV-16`). The pattern is:

  ```text
  ^(build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)!?: [a-z0-9].{1,68}[^.\s]$
  ```

## 3. Pull requests

- **DLV-15 (MUST)** Every change reaches `develop` through a pull
  request, including changes by the owner.
- **DLV-16 (MUST)** The merge method depends on the target branch, and
  the repository enforces it:

| Target | Method | Why |
| --- | --- | --- |
| `develop` | Squash, and only squash | One commit per change, whose message is the pull request title: a readable history and trivial reverts |
| `main` | Merge commit, and only a merge commit | `main` receives the commits of `develop` unchanged. A squash or a rebase would create different commits on `main`, and the two branches would drift apart |

- **DLV-17 (MUST)** A pull request into `develop` can merge only when all
  required checks pass (`DLV-31`), its branch is up to date with
  `develop`, and all review conversations are resolved.
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
  squash as the only merge method; no force pushes; no deletion. The
  release automation is the only actor allowed to bypass it, and only to
  bring `main` back into `develop` (`DLV-46`).
- **DLV-26 (MUST)** A ruleset on `main` requires: a pull request; a
  merge commit as the only merge method; a required check that fails
  unless the pull request's head branch is `develop`; the required
  status checks; an up-to-date branch, which proves that the previous
  release was brought back into `develop`; no force pushes; no deletion.
  The release automation is the only actor allowed to bypass it, and
  only to add the release commit (`DLV-43`).
- **DLV-27 (MUST)** Tags matching `v*` can be created only by the release
  workflow and can never be moved or deleted.
- **DLV-28 (MUST)** Repository settings: default branch `develop`; squash
  and merge-commit methods enabled, rebase merging disabled; the commit
  title for both methods taken from the pull request title, with an
  empty body; head branches deleted automatically on merge.
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
| `sql` | `uv run sqlfluff lint dbt` |
| `dbt` | The dbt project parses, its unit tests pass, every description is present, and the orchestrator's definitions load |
| `docs` | markdownlint, link check, dbt documentation build |
| `lock` | `uv lock --check`, and generated requirement files are current |
| `build` | `uv build`, and the container image builds |

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

```text
 feature branch --squash--> develop --release pull request--> main
                               |          (merge commit)        |
                               |                                |
                     automation keeps the              every commit here
                     release pull request              is a release
                     up to date
```

- **DLV-40 (MUST)** Code versions follow Semantic Versioning. Tags are
  `v<major>.<minor>.<patch>`. All packages share one version.
- **DLV-41 (MUST)** Whenever `develop` holds at least one commit since
  the last release that changes the version (`DLV-11`), automation keeps
  exactly one release pull request open from `develop` into `main`, and
  updates it on every push to `develop`:
  - its title is `chore: release <next version>`, with the next version
    computed from the commit types since the last release;
  - its description is the release notes so far: the commits since the
    last release, grouped by type, generated from their messages.
- **DLV-42 (MUST)** Merging the release pull request is the act of
  releasing. A person does it deliberately. Nothing is released as a
  side effect of merging a feature.
- **DLV-43 (MUST)** On that merge, the release workflow, in order:
  1. computes the version from the commits since the last release;
  2. writes it everywhere the version is stored (every package's
     `pyproject.toml` and the lockfile) and adds the new section to
     `CHANGELOG.md`;
  3. commits those files to `main` as one release commit,
     `chore: release <version>`;
  4. creates the tag `v<version>` on the release commit;
  5. publishes a GitHub Release for the tag with the same notes;
  6. builds the package and the container image from the tag, attaches
     the package to the release, and publishes the image to the
     container registry;
  7. brings `main` back into `develop` (`DLV-46`).
- **DLV-44 (MUST)** The version is stored in the repository
  (`ENG-17`) and `CHANGELOG.md` follows the Keep a Changelog format.
  Both are written only by the release workflow, never by hand.
- **DLV-45 (MUST)** The release commit does not start another release,
  and the push that brings it into `develop` does not open a release
  pull request, because it holds no releasable commit (`DLV-41`).
- **DLV-46 (MUST)** After every release `develop` is brought level with
  `main`, so that it holds the release commit, the version, the
  changelog, and the tag:
  - normally by fast-forward, which is possible whenever nothing was
    merged into `develop` during the release;
  - otherwise by merging `main` into `develop` with a merge commit;
  - if that merge conflicts, the workflow stops, raises an alert, and
    opens a pull request `chore: sync main into develop` for a person to
    resolve. Until it is merged, the next release pull request cannot be
    merged (`DLV-26`).
- **DLV-47 (MUST)** The release tooling is a conventional-commit release
  tool (python-semantic-release is the reference choice) plus the
  hosting platform's own pull request and release features.

## 8. Deployment

Deployment installs a released code version on the host. The host is
reachable only over a private network, so deployment is pulled by the
host, never pushed by continuous integration.

- **DLV-50 (MUST)** `opa ops deploy <version>` performs a deployment:
  pull the release's container image and pin it by digest; apply the
  serving database's migrations; apply roles and verify access; restart
  the project's services on the new image; run smoke checks; record the
  deployment in `ops`.
- **DLV-51 (MUST)** The host runs only tagged releases. A timer notices a
  new release and notifies the operator; the operator runs the
  deployment.
- **DLV-52 (MUST)** Deploying code never rebuilds or republishes data on
  its own. Data changes only through orchestrator runs and data
  releases.
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
   stale branch cannot be merged into `develop`.
2. A direct push to `develop` or `main` is rejected.
3. A squash or rebase merge into `main` is impossible, and a pull request
   into `main` from any branch other than `develop` fails its check.
4. Merging a `feat` pull request into `develop` creates or updates the
   release pull request into `main`, with the next minor version in its
   title and the change in its description.
5. Merging the release pull request produces a release commit on
   `main` with the new version and changelog section, a tag on it, a
   GitHub Release with the built package, and a published image.
   Afterwards `develop` and `main` point at the same commit.
6. A release made while `develop` moved ends with `main` merged into
   `develop`, or with a sync pull request and an alert.
7. `opa ops deploy` installs that release on the host and records it.
