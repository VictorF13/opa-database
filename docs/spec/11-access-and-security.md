# 11. Access and security

## 1. Roles

Access is managed through a small, fixed set of roles. People and
programs never receive privileges directly.

| Role | Kind | Purpose |
| --- | --- | --- |
| `opa_owner` | No login | Owns every schema and object |
| `opa_pipeline` | Login | The pipeline in the `opa` database: member of `opa_owner`; publishes releases, writes `ops` |
| `opa_lake` | Login | The pipeline's access to the lake catalog database |
| `opa_lake_read` | Login | Read-only access to the lake catalog, for `opa lake shell` |
| `opa_orchestrator` | Login | The orchestrator's access to its own database |
| `opa_app_labeling` | Login | The labeling application |
| `opa_app_explorer` | Login | The exploration application |
| `opa_read_gold` | Group | Read `gold`, `ref`, and `meta` |
| `opa_read_inference` | Group | Read `inference` |
| `opa_read_silver` | Group | Read `silver`, except personal columns |
| `opa_read_pii` | Group | Read personal columns (`card_id`) |
| `opa_read_labels` | Group | Read `labels` and `feedback` |
| `opa_write_feedback` | Group | Insert into `feedback.flag` |
| `opa_label` | Group | Insert into `labels` |
| `opa_sandbox` | Group | Own a personal `sandbox_<user>` schema |
| Personal roles | Login | One per person, member of the groups that person needs |
| `opa_admin` | Superuser | Break-glass administration only |

- **SEC-1 (MUST)** Each of the three databases (`ARC-17`) has its own
  roles, and no role can connect to a database it has no business in.
  In the `opa` database, privileges on schemas and objects are granted
  only to group roles. A person's access is the set of groups their personal
  role belongs to.
- **SEC-2 (MUST)** Roles, memberships, grants, default privileges, and
  per-role settings are defined as idempotent SQL in `db/roles/` and
  applied with `opa db roles apply`. No privilege is granted by hand.
- **SEC-3 (MUST)** `opa_owner` owns everything and sets default
  privileges in every schema, so objects created later carry the right
  grants from the moment they exist.
- **SEC-4 (MUST)** No application and no pipeline step connects as a
  superuser. `opa_admin` is used for instance administration only; its
  credential is held by the system owner; every use is recorded in the
  operations log (`OPS-35`).
- **SEC-5 (MUST)** Each person has their own login role. There are no
  shared accounts. Adding and removing a person follows a runbook and is
  a reviewed change to `db/roles/`.
- **SEC-6 (MUST)** `PUBLIC` has no privileges on the database beyond
  what PostgreSQL requires, and cannot create objects in `public`.
- **SEC-7 (MUST)** Applications get the minimum they need:
  `opa_app_labeling` reads what it displays and inserts into `labels`;
  `opa_app_explorer` reads and inserts into `feedback.flag`. Neither can
  update or delete.
- **SEC-8 (MUST)** `labels` and `feedback` are append-only for every role
  except `opa_owner`: corrections are new rows (`APP-22`).
- **SEC-9 (MUST)** Per-role settings bound what an account can do:

| Setting | Read roles | Applications | Pipeline |
| --- | --- | --- | --- |
| `statement_timeout` | 15 min | 60 s | none |
| `idle_in_transaction_session_timeout` | 10 min | 5 min | 60 min |
| `default_transaction_read_only` | on | off | off |
| `work_mem` | 256 MB | 64 MB | 512 MB |
| Connection limit | 5 per person | 20 | 30 |

- **SEC-10 (MUST)** A sandbox schema belongs to one person, is excluded
  from backups, is never read by the pipeline, and is reported when it
  exceeds `ops.sandbox_max_gb` (default 20).

### Access matrix

R is read, I is insert, a dash is no access.

| Group | `gold` | `ref` | `inference` | `silver` | `card_id` | `labels` | `feedback` | `meta` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `opa_read_gold` | R | R | - | - | - | - | - | R |
| `opa_read_inference` | - | - | R | - | - | - | - | - |
| `opa_read_silver` | - | - | - | R | - | - | - | - |
| `opa_read_pii` | - | - | - | - | R | - | - | - |
| `opa_read_labels` | - | - | - | - | - | R | R | - |
| `opa_write_feedback` | - | - | - | - | - | - | I | - |
| `opa_label` | - | - | - | - | - | I | - | - |

The usual researcher profile is `opa_read_gold`, `opa_read_inference`,
`opa_read_silver`, `opa_write_feedback`, and `opa_sandbox`.

- **SEC-11 (MUST)** The expected matrix is a file in `db/roles/`.
  `opa db access verify` compares the database's actual privileges with
  it, object by object and role by role.
- **SEC-12 (MUST)** Verification runs after every `roles apply` and at
  the end of every publish. A difference fails the publish. Access can
  therefore never be lost or widened by a publish.
- **SEC-13 (MUST)** Publishing never drops a schema. Tables are swapped
  inside existing schemas (`PERF-31`), so schema-level privileges and
  default privileges persist.

## 2. Privacy

Fare card identifiers and the travel histories attached to them are
personal data under Brazilian data protection law (LGPD).

- **SEC-20 (MUST)** `card_id` exists only in `silver.afc_boardings` and
  is readable only by members of `opa_read_pii`, through column-level
  privileges. Membership is granted per person, for a stated purpose,
  and recorded.
- **SEC-21 (MUST)** Every other layer and role uses `card_key`:

  ```text
  card_key = first 128 bits of HMAC-SHA-256(secret key, card_id)
  ```

  The same card always yields the same key, so travel-pattern research
  works unchanged. Placeholder card values yield no key.
- **SEC-22 (MUST)** The secret key is a file outside the repository and
  outside the database, readable only by the pipeline's operating-system
  user, and backed up separately from the data it protects (`BKP-6`).
  Losing it means keys cannot be reproduced: a rebuild then uses a new
  key and starts a new release series. Rotating it follows the same
  procedure, deliberately.
- **SEC-23 (MUST)** Data is classified, and the class decides who may
  see it and whether it may leave the project:

| Class | Data | Handling |
| --- | --- | --- |
| Open | Schedule, reference lists | No restriction |
| Internal | Pings, activities, runs, stop events, trip records | Project members |
| Pseudonymous | Taps with `card_key` | Project members; still personal data, because a sequence of taps can identify a person |
| Identified | `card_id` | `opa_read_pii` only |

- **SEC-24 (MUST)** Nothing derived from tap-level data leaves the
  project except as aggregates in which every cell counts at least
  `privacy.min_cell` (default 10) distinct cards, or after an explicit,
  recorded review.
- **SEC-25 (MUST)** The repository contains no real data. Test fixtures
  are synthetic (`ENG-35`). Continuous integration rejects files above a
  size limit and runs secret scanning.
- **SEC-26 (MUST)** Logs, error messages, and the operations log never
  contain card identifiers.
- **SEC-27 (MUST)** The legal basis for processing, the data sharing
  terms with the agency, and the retention period are recorded in
  `docs/explanation/data-governance.md`. Until they are confirmed they
  are open items (`19-open-items-and-risks.md`).

## 3. Secrets

- **SEC-30 (MUST)** Secrets are files under `${OPA_SECRETS_DIR}`, outside
  the repository, with owner-only permissions: database passwords, the
  remote raw store credential, the pseudonymization key, and the backup
  repository password. The environment file holds only non-secret
  configuration and the paths of those files.
- **SEC-31 (MUST)** No secret appears in the repository, in a container
  image, in a command line, or in a log. The hosting platform's secret
  scanning and push protection are enabled.
- **SEC-32 (MUST)** Every secret has a rotation procedure in the runbook,
  and rotation is exercised once before the system is declared
  production-ready.

## 4. Network and host

- **SEC-40 (MUST)** The database, the web SQL client, the applications,
  and the orchestrator's web interface listen only on the host's private
  overlay address. Access control lists of the overlay network restrict
  each port to the devices of the people who need it. The lists are
  documented in the runbook.
- **SEC-41 (MUST)** Traffic is encrypted in transit by the overlay
  network. If any service is ever reachable outside it, TLS on that
  service becomes mandatory first.
- **SEC-42 (MUST)** The host accepts inbound connections only through the
  overlay network, uses key-based SSH, and applies security updates
  automatically.
- **SEC-43 (MUST)** Third-party images (the web SQL client) are pinned by
  digest and updated on the same schedule as the database (`PLT-21`).
- **SEC-44 (MUST)** The orchestrator's web interface can launch any run
  and has no login of its own. Reaching it is therefore an operator
  privilege: its port is open only to the operator's devices.
- **SEC-45 (MUST)** Direct access to the lake (its catalog database and
  its files) exposes every layer, including personal columns. It is
  limited to the pipeline and to people who hold `opa_read_pii`.
  Everyone else reads the serving database.

## 5. Audit

- **SEC-50 (MUST)** Connections and schema-changing statements are
  logged (the logging settings of [03-platform.md](03-platform.md)).
- **SEC-51 (MUST)** Every row in `labels` and `feedback` records who
  wrote it and when. Applications identify the person from the overlay
  network's identity headers (`APP-4`), not from a typed name.
- **SEC-52 (MUST)** Every quarter an access review lists roles,
  memberships, and members of `opa_read_pii`, and the owner confirms or
  removes each. The review is recorded.

## 6. Acceptance

1. `opa db access verify` passes on a freshly published release.
2. A read role cannot write anywhere but its sandbox and cannot read
   `card_id`.
3. A query from a read role is cancelled at its timeout.
4. A deliberate attempt to commit a secret is blocked.
5. Dropping and republishing all served tables leaves every privilege
   intact.
