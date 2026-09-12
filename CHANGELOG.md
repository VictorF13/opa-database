# CHANGELOG

<!-- version list -->

## v1.1.0 (2026-09-12)

### Bug Fixes

- Add NOT NULL on company_id and refresh the stale table comment
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Allow null validator_id in AFC ingestion
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Clamp out-of-range AVL headings to 0 in bus_matching_avl_positions
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Correct 3 mutual-best matches the pair model wrongly zeroed out
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Detect colliding AVL month folder names ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Gate dictionary boost on being competitive with the actual evidence
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Harden AVL ingestion against raw data quirks
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Install the ml dependency group in CI ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Match GTFS referential tests to donor export
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Penalize CV variance in winner selection; rank features per family
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Read AVL rows as strings and drop corrupted ones
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Rebuild final tables with the bus-21517 correction applied
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Restrict Postgres/Adminer to bind only on Tailscale, not 0.0.0.0
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Set a fallback raw_data_root so tests can collect in CI
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Skip empty raw AVL day files ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Stop requiring raw_data_root to exist at Settings construction time
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Tolerate trailing t suffix in AFC dump filenames
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

### Chores

- Add jupyter and ipykernel as dev dependencies
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Commit bus matching model training checkpoints
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Gitignore pytest cache, coverage artifacts, notebook checkpoints, and local Claude state
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Ignore print statements in notebook lint rules
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Ignore regenerable bus matching feature checkpoints
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Remove gold dbt layer ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Retrigger CI after retargeting PR to develop
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

### Documentation

- Add a usage example for joining GTFS shape + AVL positions
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Fix stale opa credential example in remote-access guide
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Require running prek before considering a change done
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

### Features

- Add active learning labeling UI for the Trip Validity model
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add audit sampling so precision is measured, not assumed
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add AVL match info and trip timestamps to the final dataset
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add contestedness, signature, and blocking-candidate notebooks
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add covering composite indexes for AVL vehicle/device lookups
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add device-side coverage table, the other half of Section 13
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add gold layer, a normalized trip-centric warehouse for Nov 2023
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add heading, AFC-direction, fare-timing and day-continuity features
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add ml.trip_validity_fares_final, the final fare-collection table
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add ml.trip_validity_final, the final per-trip validity table
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add ml.trip_validity_route_stops, ordered stops per route+direction
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add month-level pair features, labels, and pair model
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add notebook 06, a full model-family sweep over the labeled dataset
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add notebook 06, SFFS + nested repeated CV final model sweep
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add pair-validation UI and centralize bus exclusions
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add per-date global assignment (plan Section 9)
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add per-device temporal smoothing (plan Section 10)
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add route straight-line distance features to the final dataset
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add stopping signal to the pair-validation UI
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add terminal-distance features to the final dataset
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add Trip Validity model dataset build notebooks
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Add weekday_number and is_weekend to the Trip Validity dataset
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Build bus-to-device matching active-learning labeler app
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Build bus_matching_candidate_pairs and avl_positions view
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Build Section 13's final output table, with device-swap split detection
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Cap active-learning split at exactly 250/125/125 of the 500-label budget
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Exclude no-AVL companies, weight dictionary evidence, extend same-bus-stickiness
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Full GTFS history backfill with partitioned silver layer
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Ingest device and legacy vehicle dictionary sources into bronze
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Load vehicle dictionary family bronze sources into silver
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Match AFC buses to AVL vehicles and materialize trip positions
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Raise uncertain-draw probability once calibration/test are full
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Rename vehicle dictionary silver tables to dictionary_* prefix
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Retrain every 5 labels instead of 15 once eval sets are full
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Run final model sweep ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Run full optimization on every retrain once eval sets are full
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Session-only skip tracking; fix: artifact pickling breaks on hot-reload
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Stratify audit sampling, add k-fold CV, and persist the pair model
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Substitute nearest export for missing GTFS tables
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Support 2015-2019 legacy GTFS export naming schemes
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Support 2018's whole-month AVL raw file layout
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Track the active learning app's model artifacts in git
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Train the model on company_id and garage-distance features
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Train the model on route straight-line distance features
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Train the model on the 6 terminal-distance features
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

- Treat vehicle_dictionary as a static source, same as the rest
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

### Performance Improvements

- Partition silver tables by load period ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))

### Refactoring

- Rename SILVER_* environment variables to DB_*
  ([#33](https://github.com/VictorF13/opa-database/pull/33),
  [`79a05aa`](https://github.com/VictorF13/opa-database/commit/79a05aa1537c2a5799ae8f3548e44741e2f96a31))


## v1.0.0 (2026-07-08)

- Initial Release
