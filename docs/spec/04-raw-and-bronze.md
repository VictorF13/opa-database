# 04. Raw and bronze

## 1. Raw store

The raw files are the only irreplaceable input. The system reads them from
a remote file store and keeps a verified local mirror on the BULK tier.

### 1.1 Access

- **RAW-1 (MUST)** The system accesses the remote raw store with a
  credential whose scope is read-only. The credential is stored outside
  the repository (`SEC-30`).
- **RAW-2 (MUST)** The system never creates, modifies, moves, or deletes
  anything in the remote raw store.
- **RAW-3 (MUST)** `opa raw inventory` lists the entire remote store
  without downloading file contents and records every object in the
  manifest (section 1.3). It is the first step of every ingest run and is
  cheap enough to run daily.

### 1.2 Local mirror

- **RAW-4 (MUST)** `opa raw fetch` downloads files selected by source and
  period into `${OPA_BULK_ROOT}/raw/files/`, preserving the remote
  relative path. A file is accepted only if its size and checksum match
  the remote listing; otherwise it is discarded and retried.
- **RAW-5 (MUST)** A SHA-256 digest is computed locally for every fetched
  file and stored in the manifest. It is the file's identity for
  everything downstream.
- **RAW-6 (MUST)** Mirrored files are immutable: written once, then made
  read-only on disk. No step overwrites or deletes a mirrored file.
- **RAW-7 (MUST)** If the content at a remote path changes, the new
  content becomes a new version in the manifest and the previous content
  is kept under `raw/versions/`. Downstream layers treat each version as
  a distinct file.
- **RAW-8 (MUST)** If a file disappears from the remote store, the local
  copy is kept and the manifest marks it `missing_remote`. Removal never
  propagates.
- **RAW-9 (MUST)** The remote store may hold two objects with the same
  path. Objects are identified by the remote object identifier, not by
  path, and each is mirrored under a distinct local name.
- **RAW-10 (MAY)** The mirror can be partial. The manifest is always
  complete. A step that needs a file that is not mirrored fetches it.
- **RAW-11 (MUST)** `opa raw verify` re-hashes mirrored files against the
  manifest. It runs on a schedule (`OPS-21`) and after any storage
  incident. A mismatch is a critical alert, and the file is re-fetched.

### 1.3 Manifest

`meta.raw_file`, one row per file version:

| Column | Meaning |
| --- | --- |
| `file_id` | Identifier derived from the SHA-256 digest once fetched; provisional (remote identifier and remote checksum) before |
| `remote_id`, `remote_path` | Object identifier and path in the remote store |
| `source` | `avl`, `afc`, `gtfs`, `dictionary`, or `unknown` |
| `dataset_hint` | The bronze dataset(s) this file feeds, or `ignored` |
| `ignore_reason` | Why a file is not ingested, when `ignored` |
| `size_bytes`, `remote_checksum`, `sha256` | Integrity |
| `remote_modified_at`, `first_seen_at`, `fetched_at` | Timeline |
| `status` | `listed`, `mirrored`, `superseded`, `missing_remote`, `unsupported` |
| `period` | Delivery period parsed from the path or name |
| `encoding` | Detected or declared text encoding |

`meta.raw_file_member` lists the members of every archive (name, size,
checksum, whether ingested).

- **RAW-12 (MUST)** Every file in the manifest is classified: it either
  feeds a named bronze dataset or is marked `ignored` with a reason. A
  file whose source or format is not recognized makes the inventory check
  fail until someone classifies it. Nothing is skipped silently.
- **RAW-13 (MUST)** Objects that are not ordinary files (for example
  cloud-native documents with no downloadable content) are recorded with
  status `unsupported`.
- **RAW-14 (MUST)** The rules that map paths to sources, datasets, and
  periods are data (`ref.raw_path_rule`), covered by tests that use real
  path examples, including the irregular folder names and the known
  filename error (`REF-12`).

### 1.4 Phase P0 inventory

Before bronze is finalized, the full remote store is inventoried and
profiled (`17-roadmap.md`, P0). The outputs are: years present per source,
total size, every distinct file layout, every attribute and column seen,
and confirmation or correction of each profile fact in
[01-source-data.md](01-source-data.md).

## 2. Bronze

Bronze is a lossless copy of the raw files in one uniform format. Its
purpose is to make every later step independent of file formats, and to
make it impossible to lose information before anyone has decided what
matters.

### 2.1 Rules

- **BRZ-1 (MUST)** Bronze is organized into datasets. A dataset holds one
  kind of record from one source (section 2.3).
- **BRZ-2 (MUST)** Each raw file (or archive member) produces exactly one
  output file per dataset it feeds, at
  `bronze/<dataset>/period=<YYYY-MM>/<file_id>.parquet`. The output path
  depends only on the raw file's identity and delivery period. Two raw
  files can never write to the same path, and re-ingesting a file can
  only replace its own output.
- **BRZ-3 (MUST)** Every record of the source is kept. Every field is
  stored as text, exactly as it appears after decoding and after the
  format's own unquoting or unescaping. Bronze does not trim, convert
  types, interpret sentinels, deduplicate, filter, or reorder.
- **BRZ-4 (MUST)** Every row carries lineage columns:

| Column | Meaning |
| --- | --- |
| `_file_id` | The raw file version the row came from |
| `_row` | The record's 1-based ordinal within that file (line number for delimited text, element ordinal for XML) |
| `_ingest_run_id` | The task run that wrote the row |

- **BRZ-5 (MUST)** A record that cannot be split into the dataset's field
  structure (wrong field count, broken quoting, undecodable bytes,
  malformed markup) is written to `bronze/_quarantine/<dataset>/` with its
  raw text, position, and a reason code. It is not dropped and it does
  not fail the file.
- **BRZ-6 (MUST)** Fields that the dataset definition does not know are
  preserved in an `_extra` column (a map from name or position to text),
  and a schema-drift event is recorded in `meta`. Nothing is discarded
  because it was unexpected.
- **BRZ-7 (MUST)** For nested sources, every element is represented.
  Elements at levels that carry data are rows in their own dataset; an
  element that would otherwise leave no trace (for example a parent with
  no children) is a row in a catch-all dataset.
- **BRZ-8 (MUST)** Record accounting holds per file and is stored in
  `meta.bronze_file`:

  ```text
  records in the raw file = bronze rows + quarantined records
  ```

  For XML, the count of elements per tag in the file equals the count
  represented in bronze.
- **BRZ-9 (MUST)** At ingest, the range of event dates covered by each
  file is computed and stored in `meta.bronze_coverage` (file, dataset,
  event date, row count). Silver uses this index to know which files feed
  which date (`SLV-3`). Rows whose event timestamp cannot be read are
  counted under an "undated" bucket.
- **BRZ-10 (MUST)** Validation at bronze is structural only: expected
  header or field count, expected root element, declared encoding. A
  violation raises a drift event and never rejects rows beyond `BRZ-5`.
- **BRZ-11 (MUST)** Text is decoded with the encoding recorded in the
  manifest. Encodings differ between files of the same source and are
  detected per file.
- **BRZ-12 (MUST)** An empty raw file produces a manifest entry with zero
  records and no Parquet file. It is a recorded fact, not an error.
- **BRZ-13 (MUST)** Ingest is deterministic: the same raw file always
  produces the same bronze rows in the same order.
- **BRZ-14 (MUST)** Missing-value rules are fixed per format. Delimited
  text: an empty field is stored as the empty string. XML: an absent
  attribute is stored as null and an attribute present with no content as
  the empty string.

### 2.2 Why bronze is keyed by file

A daily AVL file covers 03:00 UTC to 03:00 UTC of the next day, and an AFC
dump covers many service dates. If a step wrote "one output per event date"
while reading "one input per file", two files would write to the same
output and the later one would overwrite the earlier. Keying bronze by
file, and deriving event-date coverage as an index (`BRZ-9`), makes that
failure impossible by construction.

### 2.3 Datasets

| Dataset | Source | One row per | Named fields |
| --- | --- | --- | --- |
| `avl_ping` | AVL CSV | Line | The nine fields of the source dictionary, by source name |
| `afc_viagem` | AFC XML | `Viagem` element | Every attribute of the element and of each ancestor, named `<Tag>__<attribute>` |
| `afc_passageiro` | AFC XML | `Passageiro` element | Every attribute of the element, plus `_viagem_row` linking to its parent row |
| `afc_other` | AFC XML | Any element not represented above | Tag, path, attributes as a map |
| `gtfs_<member>` | GTFS zip | Line of one member file | The member's header names |
| `dict_<name>` | Dictionary CSV | Line | The file's header names |

- **BRZ-20 (MUST)** `avl_ping` handles both layouts (headerless daily
  files and monthly files with a header) and produces the same field
  names for both. Which layout applied is recorded in the manifest.
- **BRZ-21 (MUST)** `afc_viagem` has one row for every `Viagem` element,
  including one with no `Passageiro` children.
- **BRZ-22 (MUST)** Every member of a GTFS archive that is delimited text
  becomes a dataset named after the member, whether or not it is in the
  known list. Members that are not delimited text are listed in
  `meta.raw_file_member` and marked not ingested.
- **BRZ-23 (MUST)** Bronze does not borrow data between files. A GTFS
  export that lacks a member simply has no file for that dataset; the
  gap is handled, visibly, in silver (`SLV-44`).
- **BRZ-24 (MUST)** Formats discovered by the P0 inventory (for example
  the earlier AFC layout) get their own datasets under the same rules.
  Adding a dataset is a reviewed change with a sample-based test.

### 2.4 Format

- **BRZ-30 (MUST)** Bronze files follow `ARC-10` to `ARC-12`. Rows are
  stored in source order.
- **BRZ-31 (MUST)** Bronze is never edited in place. A changed dataset
  definition is applied by re-ingesting from raw.

## 3. Acceptance

Bronze is accepted for a period when:

1. Every manifest file of the period is `mirrored` and verified, or
   `ignored` with a reason.
2. `BRZ-8` holds for every file.
3. Re-ingesting the period changes no output bytes.
4. For three files per source chosen at random, a script reconstructs the
   original records from bronze plus quarantine and they match the raw
   file.
