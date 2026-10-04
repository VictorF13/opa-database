# 18. Coexistence

The host may already run other deployments: databases, containers, data
directories, and scheduled jobs that people depend on. This system is
built beside them. It takes nothing from their design, and it must never
disturb them.

This is the only document in the specification that refers to anything
that exists before the system is built. It states isolation rules only.

## 1. Protected resources

A **protected resource** is anything on the host that this system does
not own and people rely on. Protected resources are declared in
`deploy/protected.toml`:

| Kind | Declared by |
| --- | --- |
| Container projects and containers | Project name, container name |
| Volumes | Volume name |
| Databases | Host, port, database name |
| Paths | Directory |
| Network ports | Address and port |
| Scheduled jobs | Unit or job name |

- **COX-1 (MUST)** The declaration is filled in before any platform work
  starts, from an inventory of the host, and is reviewed by the owner.

## 2. Isolation

- **COX-2 (MUST)** The system uses its own container project name
  (`PLT-10`), its own ports, its own directories under the tier roots
  (`PLT-6`), its own database instance, and its own roles. None of these
  collide with a protected resource.
- **COX-3 (MUST)** No command of the system modifies, stops, restarts,
  recreates, moves, or deletes a protected resource. Every command that
  could remove or replace something checks its target against the
  declaration first and refuses on a match. This check is tested.
- **COX-4 (MUST)** The system has no build-time dependency on a protected
  resource. It never reads a protected database or directory to compute
  its data. The only permitted contact is the one-time export of
  section 3, through a read-only connection.
- **COX-5 (MUST)** Access to protected resources is unchanged by anything
  this system does: same addresses, same ports, same accounts, same
  privileges, same contents. People who use them keep doing so exactly
  as before, for as long as the owner keeps them running.
- **COX-6 (MUST)** Configuration files, compose files, and data volumes
  that belong to a protected deployment are not edited, moved, or
  renamed, because their location and name can be part of how that
  deployment is found and started.
- **COX-7 (MUST)** The system's resource use leaves protected services
  workable: compute and memory limits (`PLT-30`) account for them, and
  the free-space check (`PLT-8`) counts their data.

## 3. One-time export of human judgments

Human judgments made before this system existed are valuable evidence.
They enter the system as files, through the label import contract
(`APP-26`), and in no other way.

- **COX-8 (MUST)** Before the first build, existing human judgments
  (labels, confirmations, reviewed assignments) are exported once from
  wherever they are, over a read-only connection, into import files.
  The export records, for each judgment, how it was made as far as is
  known.
- **COX-9 (MUST)** The import files are stored in the exports area of the
  BULK tier, included in backups as class A, and loaded with
  `opa labels import`. From then on the system depends only on its own
  `labels` schema.
- **COX-10 (MUST)** Imported judgments are evidence to evaluate against,
  not truth to reproduce. They follow the same rules as every other
  label, including eligibility for the frozen test set.

## 4. Backup of protected databases

- **COX-11 (SHOULD)** When the BACKUP tier has capacity, each protected
  database is dumped once, over a read-only connection, into the backup
  repository, and the dump is verified by restoring it to a scratch
  instance. This protects data that would otherwise have no backup. It
  changes nothing in the protected database.

## 5. Source code

- **COX-12 (MUST)** The system is implemented as a new source tree
  (`13-engineering.md`, section 1). Code that predates it is not a
  design input: it is not imported, copied, or adapted without being
  held to this specification like any new code.
- **COX-13 (MUST)** Earlier code is tagged, preserved on a maintenance
  branch, and removed from the working tree of `develop` at phase P0, so
  that tooling, checks, and documentation describe one system. Files
  that a protected deployment needs in place (`COX-6`) are the exception:
  they stay exactly where they are until that deployment is retired.

## 6. Retirement

- **COX-14 (MUST)** Retiring a protected resource is outside the scope of
  this system. It happens only by an explicit, recorded decision of the
  owner, after a verified backup, and never as a side effect of building
  or operating this system.

## 7. Acceptance

1. The protected declaration is complete and reviewed.
2. A test shows that destructive commands refuse a protected target.
3. Before and after each phase, every protected service answers on its
   usual address with its usual accounts.
4. The one-time export is loaded, snapshotted, and backed up.
