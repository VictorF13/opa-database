# 07. Inference

Inference produces the facts that no source states: what each vehicle was
doing at every moment, which device was on which bus, what the network
really looks like, how each operator trip record relates to what happened,
where each tap took place, and when each stop was reached.

## 1. Approach

The design rests on one decision (`D-06`): **the observed track is the
backbone.** A vehicle's GPS track is segmented into activities
independently of any operator-entered record. Trip records and taps are
then attached to that timeline as evidence. This single structure handles
the whole family of problems listed in
[01-source-data.md](01-source-data.md), section 9:

| Problem | How the structure handles it |
| --- | --- |
| Trip record opened in the garage, or covering a drive to the start | The record overlaps garage or deadhead activity and is classified non-revenue |
| Record left open across several runs | One record reconciles to several runs |
| Overlapping records | Overlap is detected per bus and resolved by the runs |
| Wrong route or direction on the record | The observed pattern differs from the declared one; both are kept |
| Service with no record | A run exists with no trip record |
| Partial runs and short turns | A run has a coverage and a termination reason; recurring ones become patterns |
| Split routes | Segment patterns chain into loops |
| Wrong or missing schedule geometry | Observed geometry is proposed as a corrected pattern |
| Unlinked devices that run routes | They have runs with no bus |
| Buses without a device | They are reconciled against a sparse track built from their taps, or reported as having no track |

## 2. Common rules

- **INF-1 (MUST)** A component reads only silver, reference data, labels,
  released models, and outputs of earlier components for the same
  month.
- **INF-2 (MUST)** Every inferred row carries a `method` and, where it is
  an estimate, a `confidence` between 0 and 1. Decisions that combine
  several kinds of evidence also carry an evidence summary that shows
  what each kind contributed.
- **INF-3 (MUST)** "Unknown" is a valid result. A component never forces
  an assignment whose confidence is below its acceptance threshold. It
  records the best candidate, its confidence, and a reason code.
- **INF-4 (MUST)** Inference never overwrites a source value. A correction
  is a new column next to the original, with the reason for it (`PR-3`).
- **INF-5 (MUST)** Components are deterministic (`ARC-50`).
- **INF-6 (MUST)** Each component is specified with two methods. The
  **baseline method** is the simplest one that can work: rules or a
  direct join that a person can read and argue with. The **full method**
  is the complete treatment of the problem. A component starts on its
  baseline. It adopts the full method when the baseline fails a gate of
  section 16, or leaves more than `method.max_unresolved_frac` of its
  cases unresolved. The full method is then applied to the cases the
  baseline cannot settle, and the baseline stays as a cross-check. A
  learned model replaces a transparent method only when it beats it on
  the frozen test set by more than `gate.model_min_gain` and still
  reports per-row evidence.
- **INF-7 (MUST)** Every threshold is a parameter (`REF-20`).
- **INF-8 (MUST)** Each component writes summary metrics to `meta` on
  every materialization: coverage, counts per category and per reason code, and
  confidence distribution. These feed drift monitoring (`DQ-30`).
- **INF-9 (MUST)** Units of work are entity-days (`ARC-8`, `ARC-34`).
  Steps that are set-based (candidate search, co-location, interval
  reconciliation, tap assignment, profiles) are dbt models. Steps that
  walk a track in order (preparation, segmentation, stop events) are
  Python (`ARC-18`).

## 3. Components and order

```text
 silver.avl_pings ---> [A] track preparation ---> [B] device profiles
                                |
 silver.gtfs_* ------> [C] patterns from the schedule
                                |
                                v
                      [D] activity segmentation ---> activities, runs, blocks
                                |
 silver.afc_* -------+          |
 silver.identity_claims --> [E] bus and device linkage
                                |
                      [F] tap-derived tracks (buses without a device)
                                |
                                v
                      [G] reconciliation of trip records
                                |
              +-----------------+-----------------+
              v                 v                 v
      [H] boardings      [I] stop events   [J] learned patterns
                                                and rule proposals
              |
              v
      [K] fare code profile
```

Components A to D depend only on AVL, the schedule, and reference data.
Segmentation therefore exists for every device, whether or not it is ever
linked to a bus.

## 4. Outputs

| Table | Grain | Published |
| --- | --- | --- |
| `device_profile` | Device, month | Yes |
| `ping_activity` | Ping | Lake only by default |
| `activity` | Activity | Through gold |
| `run` | Run | Through gold |
| `block` | Garage exit to garage return | Through gold |
| `pattern`, `pattern_stop` | Pattern; stop within a pattern | Through gold |
| `pattern_link` | Ordered pair of patterns operated consecutively | Yes |
| `pattern_proposal` | A proposed new or corrected pattern | Yes |
| `link_evidence_day` | Bus, candidate device, operational day | Yes |
| `bus_device_link` | Bus, device, date interval | Through gold |
| `unlinked_bus`, `unlinked_device` | Entity, month | Yes |
| `afc_trip_reconciliation` | Trip record | Through gold |
| `boarding_assignment` | Tap | Through gold |
| `stop_event` | Run, stop | Through gold |
| `fare_code_profile` | Code, context | Yes |
| `rule_exception_proposal` | Route | Yes |
| `run_incident_candidate` | Run | Yes |

## 5. Track preparation (A)

- **INF-10 (MUST)** A track is the ordered sequence of pings of one device
  in one operational day window (`ARC-34`). Pings with invalid
  coordinates are excluded from geometry and labeled `noise` in
  `ping_activity`. They are not removed from the data.
- **INF-11 (MUST)** Glitches are removed by consistency, not by a local
  filter. The track is split wherever the speed implied by two
  consecutive positions exceeds `track.max_speed_kmh`. Every resulting
  stretch with two or more pings is kept; a ping that cannot form a
  stretch with a neighbor is `noise`.
- **INF-12 (MUST)** An interval longer than `track.max_gap_s` between
  consecutive valid pings is a `gap` activity.
- **INF-13 (MUST)** Positions are projected once to the metric coordinate
  system (`geo.metric_crs`). Per ping, the preparation derives distance
  and time since the previous ping, implied speed, and a stationary flag
  (reported speed at or below `track.stationary_speed_kmh` and no
  meaningful displacement).

## 6. Device profiles (B)

- **INF-15 (MUST)** For each device and month the profile records: active
  days, ping count, movement radius (radius of gyration), share of time
  stationary, the zone where it most often spends the night (home zone)
  and that zone's company, the identifier family, and a class.

| Class | Definition |
| --- | --- |
| `mobile` | Moves like a vehicle on most active days |
| `stationary` | Movement radius below `profile.stationary_radius_m` on at least `profile.min_active_days` days |
| `sparse` | Too few pings or days to tell |
| `erratic` | Mostly noise or implausible movement |

- **INF-16 (MUST)** A class describes behavior, not identity. A
  `stationary` device is not declared "not a bus". It is reported as
  unresolved, with its location, so a person can identify terminal or
  depot equipment.
- **INF-14 (MUST)** A zone validation report lists (a) places where
  several devices dwell overnight that are not in `ref.zone` and (b)
  zones in `ref.zone` where no device dwells. It proposes; people update
  reference data (`REF-7`).

## 7. Patterns from the schedule (C)

A **pattern** is the unit of "a way of operating a route": a route, a
direction, an ordered list of stops, and a line geometry.

- **INF-17 (MUST)** One pattern exists for each distinct combination of
  route, direction, ordered stop list, and shape found in the schedule.
  Scheduled trips that share all four share a pattern. Stop sequences
  are positions within a pattern (`stop_seq`, starting at 1), never the
  schedule's own sequence numbers taken across trips.

  This removes the ambiguity of shapes that carry several stop lists
  (Profile: 17 of 617 shapes). A circular route published as four
  consecutive segments yields four patterns on one shape, each with its
  own stop positions and its own interval of progress along the shape.
- **INF-18 (MUST)** Construction rules:
  1. The shape's points, ordered by their sequence, form the line.
     Consecutive duplicate points are removed.
  2. Each stop is projected onto the line to get its progress (fraction
     of line length and distance in meters). Projection is sequential:
     each stop is searched forward from the previous stop's position, so
     that a line passing the same place twice is handled correctly.
  3. A pattern covers the interval of progress between its first and
     last stop.
  4. Progress must not decrease along the stop list. A stop list that
     violates this is kept, marked `quality = suspect`, and its
     offending stops are flagged. A stop farther than `match.off_pattern_m`
     from the line is flagged as not belonging to it.
  5. Direction comes from silver (`SLV-42`).
  6. The pattern identifier follows `ARC-21`. The same pattern in
     consecutive exports is one pattern with a validity interval.
- **INF-19 (MUST)** The schedule in effect on an operational day is the
  latest export dated on or before it, ignoring exports identical in
  content to their predecessor (`SLV-46`). Each pattern records its
  validity interval and schedule attributes: scheduled trips per day
  type and scheduled running time by hour.

## 8. Activity segmentation (D)

- **INF-20 (MUST)** Every ping of a device belongs to exactly one activity
  or is `noise`. Activities of a device do not overlap, and together
  with gaps they cover its whole timeline.
- **INF-21 (MUST)** Segmentation is computable from AVL, patterns, and
  reference data alone. Evidence from fare collection may refine it in a
  later pass (section 17) and must never be required.
- **INF-22 (MUST)** Activity types (`ref.activity_type`):

| Type | Meaning |
| --- | --- |
| `garage` | Inside a garage zone for at least `track.zone_dwell_min_s` |
| `terminal_layover` | Inside a terminal zone without progressing along a pattern |
| `on_pattern` | Following a pattern with forward progress |
| `deadhead` | Moving between a zone and the start or end of an `on_pattern` activity, or between two of them, without following a pattern |
| `stopped` | Stationary outside any zone and outside any `on_pattern` activity |
| `moving_unclassified` | Moving, matched to no pattern, not explained as deadhead |
| `gap` | No valid pings for longer than `track.max_gap_s` |

- **INF-23 (MUST)** Baseline method, by rules:
  1. **Anchors.** Maximal stretches inside a zone that last at least
     `track.zone_dwell_min_s` become `garage` or `terminal_layover`.
  2. **Candidates.** For each stretch between anchors, the candidate
     patterns are those in effect that day for the routes named by the
     device's own `route_id` values in the stretch and, once linkage
     exists, for the routes on the linked bus's trip records that overlap
     the stretch.
  3. **Matching.** A ping is on a candidate pattern when it lies within
     `match.off_pattern_m` of it and, above
     `match.heading_min_speed_kmh`, its heading agrees with the
     pattern's local bearing within `match.heading_tolerance_deg`. A
     stretch is assigned to the candidate on which the most pings match
     with progress that does not decrease.
  4. **Runs.** A maximal `on_pattern` stretch that advances at least
     `run.min_length_m` and passes at least `run.min_stops` stops is a
     run. Shorter stretches become `moving_unclassified`. Stops and slow
     traffic inside a run stay part of the run.
  5. **Remainder.** Moving stretches that connect a zone with a run or
     another zone become `deadhead`; other moving stretches become
     `moving_unclassified`; stationary stretches become `stopped`.
  6. **Blocks.** A block runs from leaving a garage to returning to one.
- **INF-24 (MUST)** Full method, by decoding. Candidates are widened with
  the patterns of greatest spatial overlap with the stretch, found by
  grid signature (`PERF-10`), and step 3 is replaced by a hidden Markov
  model whose states are the candidate patterns plus "off pattern".
  Emission probability comes from the cross-track distance to the
  pattern (`match.cross_track_sigma_m`) and from heading agreement.
  Transitions keep progress non-decreasing, keep the advance consistent
  with elapsed time and with the odometer difference, and penalize
  switching patterns. The most likely state sequence is found with the
  Viterbi algorithm. It handles what rules cannot: devices that report
  no route or the wrong one, overlapping routes on shared streets, and
  noisy stretches.
- **INF-25 (MUST)** A run records: device, bus when linked, operational
  date, pattern, route, direction, start and end time, first and last
  stop position reached, coverage (share of the pattern's length
  traversed), distance, ping count, longest gap, share of pings off the
  pattern, completeness class, termination reason, service evidence,
  previous and next run of the same block, and confidence.

| Completeness class | Meaning |
| --- | --- |
| `full` | Coverage at least `run.full_coverage_frac` |
| `partial_start` | Began after the pattern's first stops |
| `partial_end` | Ended before the pattern's last stops |
| `partial_both` | Both |

- **INF-26 (MUST)** Termination reasons (`ref.termination_reason`):
  `reached_end`, `short_turn` (ended at the end of an accepted shorter
  pattern), `to_garage`, `stationary` (ended in a long stop outside any
  zone), `gap` (data ended), `switched_pattern`, `unknown`.
- **INF-27 (MUST)** Service evidence on a run is one of `taps`,
  `trip_record`, `both`, or `none`. Following a pattern is an
  observation; being in revenue service is a conclusion that needs
  evidence. A run with `none` is kept and reported as unconfirmed.
- **INF-28 (MUST)** Runs of one device never overlap. Activities are
  written with a time range so that the serving database can enforce
  this (`PERF-24`).
- **INF-29 (MUST)** When a route is published as consecutive segments,
  each segment traversal is a run, and consecutive runs are linked
  (previous, next). The chain is the operated loop.

## 9. Bus and device linkage (E)

- **INF-30 (MUST)** Linkage decides, for each bus and operational day,
  which device was on board, and consolidates daily decisions into date
  intervals. It resolves the contradictions that the dictionaries
  contain; it does not inherit them.
- **INF-31 (MUST)** Candidates for a bus-day come from three sources: any
  dictionary claim; any device with a ping within `link.tap_window_s`
  and `link.tap_match_m` of one of the bus's geotagged taps (found by
  grid and time-bucket lookup, with no dictionary needed); and devices
  whose runs overlap the bus's trip records on the same route.
- **INF-32 (MUST)** Evidence families. No family is required; a family
  that does not apply to a bus-day contributes nothing, neither for nor
  against.

| Family | Applies when | Signal |
| --- | --- | --- |
| Tap position | The bus has geotagged taps | Share of taps with a ping of the device within `link.tap_window_s` closer than `link.tap_match_m`; share contradicted by a ping farther than `link.tap_contradict_m` |
| Route agreement | The device reports route codes | Agreement between the device's `route_id` and the route on the bus's trip records over time |
| Run and record agreement | The device has runs | Temporal overlap of the bus's trip records with the device's runs on the same route and direction |
| Tap timing | The bus has taps | Taps fall while the device is stationary, at or near stops |
| Garage company | The device has a home zone | The home zone's company equals the bus's company |
| Dictionary claim | A claim exists | A prior whose weight is learned per dictionary; never decisive alone |
| Continuity | Earlier days are linked | The same pairing persists from day to day |

  Profile: tap position is close to decisive where it applies (the true
  device coincides with the tap; the best rival is kilometers away) and
  applies to about 91% of buses. The other families exist for the rest.
- **INF-33 (MUST)** Scoring a candidate pair for a day:
  1. **Baseline method, by tap position.** For a bus-day with at least
     `link.min_taps` geotagged taps, the device with the largest share
     of matched taps is chosen when that share and its margin over the
     next device are clear and no tap contradicts it. Its confidence
     comes from those two numbers, calibrated on labeled pairs.
     Dictionary claims and continuity only break ties.
  2. **Full method, by combined evidence.** For bus-days the baseline
     cannot settle (too few geotagged taps, no clear winner,
     contradictions), every family of `INF-32` contributes a
     log-likelihood ratio and the sum is the pair's score (probabilistic
     record linkage in the Fellegi-Sunter sense). The ratios are
     estimated from the data, starting from the pairs the baseline
     established, and calibrated to probabilities on labeled pairs.
- **INF-34 (MUST)** Daily assignment is solved jointly for all buses and
  devices of the day as a minimum-cost matching in which every bus also
  has a "none" option. A device is assigned to at most one bus per day
  and a bus to at most one device. A second device with strong evidence
  for the same bus is reported, not assigned.
- **INF-35 (MUST)** Consolidation turns daily assignments into intervals.
  An isolated deviation inside a stable pairing is corrected and logged.
  A clean changeover from one device to another is a swap and produces
  two intervals. An interval is extended across days without evidence
  only when no other pairing claims either party.
- **INF-36 (MUST)** A link is accepted at or above
  `link.accept_confidence`. Every bus and every mobile device that is
  not linked is listed with a reason:

| Unlinked bus reason | Meaning |
| --- | --- |
| `company_without_feed` | No device of the bus's company ever appears in AVL in the period |
| `no_candidate` | No device has any evidence for the bus |
| `insufficient_evidence` | A best candidate exists below the threshold |
| `conflict` | Two candidates cannot be separated |

| Unlinked device reason | Meaning |
| --- | --- |
| `stationary`, `sparse`, `erratic` | From the device profile |
| `no_bus_evidence` | Moves like a bus; no bus has evidence for it |
| `conflict` | Competes with another device for one bus |

- **INF-37 (MUST)** "A company has no AVL feed" is computed from the data
  for each period. It is never a hard-coded list.
- **INF-38 (MUST)** `link_evidence_day` keeps, per candidate pair and day,
  the value of every family and the resulting score, so any link can be
  explained and audited.
- **INF-39 (MUST)** Accepted links never overlap in time for the same
  device or the same bus.

## 10. Tap-derived tracks (F)

- **INF-50 (MUST)** A bus with no accepted link and with geotagged taps
  gets a sparse track made of its tap positions. Profile: this applies to
  a company that provides fares and no AVL (about 14% of taps, 61% of
  them geotagged).
- **INF-51 (MUST)** A tap-derived track is used to verify route and
  direction against the declared pattern, to estimate which part of the
  pattern was covered, and to locate boardings. Runs built from it carry
  `track_source = taps` and a correspondingly lower confidence.
- **INF-52 (MUST)** Tap-derived tracks are never mixed with AVL tracks,
  and are not used for stop events.

## 11. Reconciliation of trip records (G)

- **INF-60 (MUST)** Every trip record in `silver.afc_trips` receives
  exactly one reconciliation status, one service class, and any number
  of flags. No record is excluded.
- **INF-61 (MUST)** Statuses (`ref.reconciliation_status`):

| Status | Meaning |
| --- | --- |
| `matched` | One record and one run cover each other by at least `recon.min_overlap_frac` |
| `spans_runs` | One record covers several runs |
| `shares_run` | Several records fall within one run |
| `partial` | The record overlaps a run by less than the threshold |
| `no_service_activity` | A track exists; during the record the vehicle was only in garage, layover, deadhead, or stopped (the activity is recorded) |
| `tap_track_only` | Reconciled against a tap-derived track |
| `no_track` | No device linked and no usable tap track, or the track has a gap for the whole record |

- **INF-62 (MUST)** Service class (`ref.service_class`) is
  `revenue_service`, `non_revenue`, or `undetermined`, decided from the
  status, the activities during the record, and passenger taps. A record
  reconciled to `no_track` is `undetermined` unless its taps alone settle
  it.
- **INF-63 (MUST)** Flags, independent of status: `overlaps_other_record`
  (with the group of records involved), `close_time_missing`,
  `route_mismatch`, `direction_mismatch`, `opened_in_garage`,
  `closed_late`, `opened_late`.
- **INF-64 (MUST)** A record with no closing time is given an effective
  end for matching (the next record's opening on the same bus, or its
  own last tap), marked as estimated.
- **INF-65 (MUST)** Every run receives the reverse view: `has_record`,
  `shared_record`, or `no_record`. A run with `no_record` is service that
  was operated and never registered.

### Corrections

- **INF-70 (MUST)** A reconciled record carries, next to its declared
  values, `observed_route_id`, `observed_direction`,
  `observed_pattern_id`, and the observed start and end times, each with
  its source.
- **INF-71 (MUST)** Declared values are never changed (`INF-4`). Gold
  exposes both, and states which one its convenience views use
  (`GLD-31`).
- **INF-73 (MUST)** For every route, reconciliation measures how often
  the declared direction agrees with the observed one. A route with at
  least `recon.direction_rule_min_runs` runs that disagrees consistently
  is written to `rule_exception_proposal`. Accepted proposals become
  rows of `ref.route_direction_rule` (`REF-8`).
- **INF-74 (MUST)** Runs that end with termination reason `stationary`,
  and partial runs that do not match any recurring variant, are written
  to `run_incident_candidate` with the place and time the vehicle
  stopped.

## 12. Boardings (H)

- **INF-75 (MUST)** Every tap in `silver.afc_boardings` receives an
  assignment status and reason. No tap is excluded.

| Reason | Meaning |
| --- | --- |
| `in_run` | The tap time falls inside a run of the bus's device |
| `layover_before_run` | The tap falls in a terminal layover; it is assigned to the run that departs next from that terminal |
| `non_service_context` | The tap falls in garage or deadhead activity; not assigned |
| `no_run` | A track exists and no run is near the tap time; not assigned |
| `no_track` | The bus has no track at that time; not assigned |

- **INF-76 (MUST)** Fare class (`passenger`, `non_passenger`, `unknown`)
  comes from `ref.afc_code` and is independent of assignment. An
  unassigned passenger tap is a finding, reported in accounting.
- **INF-77 (MUST)** Each tap gets a position with its source: `tap` (its
  own coordinate), `interpolated` (from the linked device's track at the
  tap time), or `none`.
- **INF-78 (MUST)** Each assigned tap gets a boarding stop: the most
  recent stop of the run that the vehicle served at or before the tap
  time, when that was within `boarding.stop_max_lag_s`; otherwise the
  stop nearest to the vehicle's progress at the tap time. The method and
  a confidence are recorded.

## 13. Fare code profile (K)

- **INF-80 (MUST)** For every coded fare field, the profile tabulates
  each code against context: activity type at tap time, zone, amount
  paid, number of distinct cards, time of day. A code that occurs almost
  only in garages with no payment, or on a single placeholder card, is
  proposed as `non_passenger`.
- **INF-81 (MUST)** Proposals are reviewed and recorded in
  `ref.afc_code` with `meaning_status = inferred` and the supporting
  profile (`REF-10`). Until then a code stays `unknown`.

## 14. Stop events (I)

- **INF-85 (MUST)** For every run built from an AVL track, there is one
  row per stop of the run's pattern.

| Status | Meaning |
| --- | --- |
| `observed` | The vehicle was stationary within `stop.dwell_radius_m` of the stop; arrival and departure are measured |
| `passed` | No dwell detected; the passing time is interpolated between the surrounding pings |
| `extrapolated` | The stop lies just beyond the first or last ping of the run, within `stop.extrapolation_max_s` |
| `not_reached` | The stop lies outside the part of the pattern the run covered |
| `unobserved` | The run covered the stop's position but the track there was missing or off the pattern |

- **INF-86 (MUST)** Method: the run's progress along the pattern over
  time is made non-decreasing (isotonic regression over the pings'
  projected progress), then each stop's progress is located on that
  curve. Dwell is detected from stationary pings near the stop. Heading
  resolves places where the pattern passes the same street twice.
- **INF-87 (MUST)** Times never go backwards along a run: for consecutive
  stops with times, the earlier stop's departure is not after the later
  stop's arrival.
- **INF-88 (MUST)** Each event records the time between the two pings
  that bracket it and a confidence grade: `high` at or below
  `stop.gap_high_s`, `medium` up to `stop.gap_medium_s`, `low` above.
- **INF-89 (MUST)** A time is never invented. Where the track cannot
  support an estimate, the status says so and the times are null.

## 15. Learned patterns (J)

- **INF-40 (MUST)** Observation can propose patterns. A proposal has a
  kind, supporting statistics, and a status: `proposed`, `accepted`, or
  `rejected`. Proposals above strict support thresholds may be accepted
  automatically; all others wait for review in the application
  (`APP-12`).

| Kind | Evidence | Result when accepted |
| --- | --- | --- |
| Short turn | Partial runs on one pattern that repeatedly start and end at the same stops, over at least `pattern.proposal_min_runs` runs, `pattern.proposal_min_days` days, and `pattern.proposal_min_buses` buses | A new pattern covering that part, with its parent pattern recorded |
| Shape correction | In-service pings that sit consistently more than `pattern.shape_deviation_m` from the line over at least `pattern.shape_deviation_min_length_m` | A pattern version with the observed geometry, linked to the pattern it corrects |
| Chain | Runs on two patterns that follow each other without layover | A row in `pattern_link` |
| Unscheduled service | Repeated `moving_unclassified` paths with passenger taps | A pattern with no schedule counterpart |

- **INF-41 (MUST)** Accepted patterns have `source = observed` and take
  part in segmentation from the next materialization on.
- **INF-42 (MUST)** Schedule-derived patterns are never altered. A
  correction is a separate pattern that points to the one it corrects.

## 16. Evaluation and gates

- **INF-90 (MUST)** Evaluation data is human judgment stored in `labels`
  (`APP-20`): trip service labels, bus and device labels, and timeline
  annotations of whole bus-days.
- **INF-91 (MUST)** A **frozen test set** is a subset of labels marked
  frozen. It is never used to fit, tune, or select anything. It is
  sampled at random (never by model uncertainty) and grows over time.
- **INF-92 (MUST)** The frozen set includes at least 30 fully annotated
  bus-days, stratified by company, route type (radial, circular,
  segmented), and day type, before milestone M1 is declared.
- **INF-93 (MUST)** Independent checks that need no labels are computed
  for every month materialized:
  - share of passenger taps that fall inside an observed dwell at their
    inferred boarding stop;
  - agreement between odometer difference and pattern distance on runs;
  - error when each ping near a stop is held out and its time is
    predicted from its neighbors;
  - run duration against the scheduled running time for the hour.
- **INF-94 (MUST)** An evaluation asset computes every metric for each
  month and stores the results in `meta.evaluation`. The release gate
  reads them at the release's snapshot (`DQ-26`).
- **INF-95 (MUST)** A release is published only if it passes these
  gates. Thresholds are parameters (`gate.*`), initial values to be
  confirmed at M1:

| Component | Metric | Gate |
| --- | --- | --- |
| Linkage | Precision of accepted links on human-confirmed pairs | At least 0.99 |
| Linkage | Recall on human-confirmed pairs | At least 0.95 |
| Segmentation | Time-weighted agreement of activity type with annotations | At least 0.95 |
| Segmentation | Median error of run start and end times | At most 60 s |
| Reconciliation | Agreement of service class with frozen trip labels | At least 0.97, and every disagreement adjudicated |
| Stop events | Violations of `INF-87` | Zero |
| Stop events | Median held-out timing error | At most 15 s |
| All | Accounting invariants (`DQ-10`) | All hold |
| All | Change against the previous release on every gated metric | No decrease beyond `gate.regression_tolerance` (0.5 point) |

- **INF-96 (MUST)** A disagreement between a model and a frozen label is
  adjudicated by a person. If the label was wrong, a corrected label is
  added as a new version; the old one is kept (`APP-22`).
- **INF-97 (MUST)** Coverage metrics (share of fleet-days linked, share of
  passenger taps assigned, share of stop events observed) are reported
  for every release and compared with the previous one.

## 17. Passes and learning over time

- **INF-98 (MUST)** Inference for a month runs in two passes. Pass 1 runs
  A to G with schedule patterns and accepted observed patterns. Pass 2 repeats
  segmentation with the linked bus's trip records as extra candidates,
  then reconciliation, boardings, and stop events. Each materialization
  reports the share of runs whose pattern changed between passes.
- **INF-99 (MUST)** Learning over time happens only through explicit,
  reviewable inputs: accepted pattern proposals, accepted rule
  exceptions, reviewed code meanings, new labels, and released models.
  Nothing learned is applied silently, and every such input is versioned
  and recorded on every materialization that used it.
