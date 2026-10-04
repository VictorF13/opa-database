# 06. Reference data

Reference data is everything the system needs to know that is not in a raw
file: who the operating companies are, where garages and terminals are,
which rules have exceptions, what codes mean, and what value every
threshold has. It is small, curated by people, and versioned with the
code.

## 1. Rules

- **REF-1 (MUST)** Reference data lives in the repository under `ref/` as
  plain files (CSV for tables, GeoJSON for geometry, TOML for
  parameters). Nothing that qualifies as reference data is written as a
  literal in code or kept only in a notebook.
- **REF-2 (MUST)** Every reference row records where it came from:
  `source` (a document, a person, or "derived from data" with the
  analysis that derived it), `added_on`, and an optional `note`. Rows
  that describe the world also carry `valid_from` and `valid_to`.
- **REF-3 (MUST)** Each reference file has a schema. `opa ref validate`
  checks types, keys, references between files, and geometry validity. It
  runs in continuous integration.
- **REF-4 (MUST)** Reference data changes by pull request. The reference
  version is a digest of the `ref/` directory and is recorded on every
  build (`ARC-43`).
- **REF-5 (MUST)** Reference data is loaded into the `ref` schema of the
  serving database on every publish, so analysts see exactly what the
  release used.
- **REF-6 (MUST)** Every enumerated value that appears in data (an
  activity type, a status, a reason code, a flag) is defined in a
  reference table with a stable code and a description. Code never emits
  an enumerated value that is not defined there.

## 2. Tables

### 2.1 Entities

| Table | Content | Key |
| --- | --- | --- |
| `ref.company` | Operating companies: canonical identifier, AFC code, name, modality | `company_id` |
| `ref.zone` | Garages and terminals as areas | `zone_id` |
| `ref.calendar_day` | Public holidays and special days | `date` |

- **REF-7 (MUST)** A zone is an area, not a point. `ref.zone` holds a
  polygon for each garage and terminal, its kind (`garage`, `terminal`),
  the company that uses it (garages), and the terminal kind (`open`,
  `closed`). A zone may be seeded from a point with a default radius
  (`zone.garage_radius_m`, `zone.terminal_radius_m`) and is replaced by a
  drawn polygon when available. The seed point is kept.
- Initial content (Profile): 11 companies; 16 garage locations across
  those companies; 11 terminals (7 closed, 4 open) from the city's
  published terminal registry.
- Zones are validated against observation: inference reports places where
  vehicles dwell overnight that are not in `ref.zone`, and zones where
  nothing ever dwells (`INF-14`). People decide; the system proposes.

### 2.2 Rules with exceptions

| Table | Content |
| --- | --- |
| `ref.route_direction_rule` | How the AFC direction code maps to `I` and `V`: a default rule plus per-route exceptions with validity dates and evidence |
| `ref.gtfs_direction_rule` | How a GTFS shape identifier encodes direction |
| `ref.raw_path_rule` | How raw paths map to source, dataset, and period |
| `ref.filename_correction` | Explicit corrections for raw file names that are wrong |

- **REF-8 (MUST)** The default mapping is code 0 to `I` and code 1 to `V`.
  The known exception (route 614, reversed) is the first row of
  exceptions. New exceptions are proposed by inference (`INF-73`) and
  accepted by review.
- **REF-12 (MUST)** `ref.raw_path_rule` and `ref.filename_correction`
  capture the irregular month folder names, the several schedule file
  name conventions, and known typing errors, each with a real example
  used as a test case.

### 2.3 Code dictionaries

`ref.afc_code` gives meaning to the coded fields of fare collection.

| Column | Meaning |
| --- | --- |
| `code_family` | `passenger_type`, `integration_type`, `integration_bum`, `category_type`, `company_modality`, `sigben` |
| `code` | The value as recorded |
| `label` | Human-readable meaning, when known |
| `fare_class` | `passenger`, `non_passenger`, or `unknown` |
| `meaning_status` | `unknown`, `inferred`, or `confirmed` |
| `evidence` | What supports the label |

- **REF-9 (MUST)** Every code observed in silver has a row. A code seen
  for the first time is added automatically with `meaning_status =
  unknown` and `fare_class = unknown`, and raises a notice.
- **REF-10 (MUST)** A meaning is never guessed in code. `inferred` rows
  cite the profile that supports them (`INF-80`); `confirmed` rows cite
  the agency or an official document.

### 2.4 Vocabularies

| Table | Defines |
| --- | --- |
| `ref.sentinel` | Placeholder values per table and column, with their meaning |
| `ref.quality_flag` | Bit, name, and description of each quality flag per table |
| `ref.reject_reason` | Reasons a bronze row is rejected by silver |
| `ref.activity_type` | Kinds of vehicle activity (`INF-22`) |
| `ref.reconciliation_status` | Outcomes of reconciling a trip record (`INF-61`) |
| `ref.service_class` | Whether a trip record is revenue service |
| `ref.assignment_reason` | Why a tap was or was not assigned to a run |
| `ref.termination_reason` | How a run ended |
| `ref.link_method`, `ref.unlinked_reason` | How a bus and device were linked, or why not |
| `ref.stop_event_status`, `ref.stop_event_method` | How a stop event was determined |

## 3. Parameters

- **REF-20 (MUST)** Every tunable value is a named parameter in
  `ref/parameters.toml` with: name, value, unit, the document that owns
  it, and its justification (a profile fact, an analysis, or "initial
  value, to be tuned in phase N"). Code reads parameters through one typed
  accessor. A literal threshold in code is a defect.
- **REF-21 (MUST)** The digest of the parameter file is recorded on every
  build. Changing a parameter makes the affected outputs stale.
- **REF-22 (MUST)** A parameter marked "initial value" is revisited in the
  phase named, and its justification is replaced by the analysis that
  set it.

### Registry

| Parameter | Default | Unit | Basis |
| --- | --- | --- | --- |
| `time.operational_day_cutoff` | 03:00 | local time | Profile: tap volume is lowest 02:00 to 03:59 |
| `time.day_margin_min` | 30 | minutes | Initial value, P5 |
| `geo.metric_crs` | EPSG:31984 | | SIRGAS 2000, UTM zone 24S |
| `geo.area_bbox` | 3.6 S to 4.0 S, 38.3 W to 38.8 W | degrees | Initial value, P0 (to cover the whole metropolitan service area) |
| `zone.garage_radius_m` | 150 | m | Initial value, P4 |
| `zone.terminal_radius_m` | 120 | m | Initial value, P4 |
| `track.max_speed_kmh` | 100 | km/h | Profile: 0.17% of consecutive pings exceed it |
| `track.max_gap_s` | 120 | s | Profile: 99th percentile ping interval is 60 s |
| `track.stationary_speed_kmh` | 3 | km/h | Initial value, P5 |
| `track.zone_dwell_min_s` | 120 | s | Initial value, P5 |
| `profile.stationary_radius_m` | 100 | m | Initial value, P5 |
| `profile.min_active_days` | 5 | days | Initial value, P5 |
| `match.cross_track_sigma_m` | 15 | m | Profile: 90th percentile cross-track distance is about 12 m |
| `match.off_pattern_m` | 100 | m | Profile: gap between the 90th (12 m) and 99th (210 m) percentiles |
| `match.heading_min_speed_kmh` | 5 | km/h | Initial value, P5 |
| `match.heading_tolerance_deg` | 60 | degrees | Initial value, P5 |
| `run.min_length_m` | 500 | m | Initial value, P5 |
| `run.min_stops` | 3 | stops | Initial value, P5 |
| `run.full_coverage_frac` | 0.90 | fraction | Initial value, P5 |
| `link.tap_window_s` | 60 | s | Profile: tap coordinates coincide with a ping within 60 s |
| `link.tap_match_m` | 50 | m | Profile: median 0 m, 75th percentile 14 m for the true device |
| `link.tap_contradict_m` | 500 | m | Profile: rival devices are a median 3.2 km away |
| `link.accept_confidence` | 0.95 | probability | Initial value, P6 |
| `pattern.proposal_min_runs` | 20 | runs | Initial value, P7 |
| `pattern.proposal_min_days` | 5 | days | Initial value, P7 |
| `pattern.proposal_min_buses` | 3 | buses | Initial value, P7 |
| `pattern.shape_deviation_m` | 30 | m | Initial value, P7 |
| `pattern.shape_deviation_min_length_m` | 200 | m | Initial value, P7 |
| `recon.min_overlap_frac` | 0.5 | fraction | Initial value, P7 |
| `recon.direction_rule_min_runs` | 50 | runs | Initial value, P7 |
| `stop.dwell_radius_m` | 30 | m | Initial value, P7 |
| `stop.gap_high_s` | 60 | s | Confidence grade boundary |
| `stop.gap_medium_s` | 180 | s | Confidence grade boundary |
| `stop.extrapolation_max_s` | 60 | s | Initial value, P7 |
| `boarding.stop_max_lag_s` | 300 | s | Initial value, P7 |
| `ops.compute_workers` | 20 | processes | Reference host has 24 cores |
| `ops.compute_memory_gb` | 28 | GB | Reference host has 60 GB |
| `ops.min_free_fraction` | 0.10 | fraction | |
| `ops.sandbox_max_gb` | 20 | GB | Initial value, P1 |
| `ops.auto_ingest` | off | | Builds and releases are always started by a person |
| `publish.window.gold` | all released months | | `PLT-50` |
| `publish.window.silver_afc` | all released months | | `PLT-50` |
| `publish.window.silver_avl` | the reference year | | `PLT-50` |
| `publish.parallelism` | 6 | connections | Initial value, P8 |
| `dq.build_retention_days` | 30 | days | |
| `dq.release_retention` | 3 | releases | |
| `gate.model_min_gain` | 1.0 | percentage points | Initial value, P8 |
| `gate.regression_tolerance` | 0.5 | percentage points | Initial value, P8 |
| `privacy.min_cell` | 10 | cards | Common disclosure-control practice; `SEC-24` |
| `label.random_share` | 0.20 | fraction | Initial value, P9 |
| `loop.retrain_min_labels` | 100 | labels | Initial value, P9 |

The individual gate thresholds (`gate.*`) and plausibility bands (`dq.*`)
are parameters too. Their values are listed with the gates and checks
they belong to (`INF-95`, `DQ-12`).

## 4. Acceptance

Reference data is accepted when `opa ref validate` passes, every zone has
a valid polygon, every code observed in the reference month has a row in
`ref.afc_code`, and every parameter has a recorded basis.
