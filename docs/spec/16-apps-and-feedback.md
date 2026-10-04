# 16. Applications and feedback

People improve the system in two ways: by flagging what looks wrong while
they use the data, and by labeling cases when asked. This document
specifies the two applications that make that possible, how human
judgment is stored, and the loop that turns it into better models
without ever putting published data at risk.

## 1. Applications

| Application | For | Writes to |
| --- | --- | --- |
| Explorer | Looking at the data: a bus-day on a map and a timeline, a route-day, a stop, the evidence behind a link | `feedback.flag` |
| Labeling | Answering questions the system asks: is this record real service, is this the right device, what was this bus doing, is this proposed pattern real | `labels.*` |

- **APP-1 (MUST)** Both applications are built with Streamlit, live in
  the `opa-apps` package, and run as services of the platform
  (`PLT-10` to `PLT-18`). Their user interface is in Brazilian Portuguese;
  code, identifiers, and documentation are in English.
- **APP-2 (MUST)** Applications read published data and write only
  labels and flags. They compute no inference and change no published
  table. Each connects with its own role (`SEC-7`).
- **APP-3 (MUST)** Nothing is loaded until the user asks for it, and
  every query is bounded by the application role's timeout. Showing one
  bus-day takes at most 2 seconds.
- **APP-4 (MUST)** The person using an application is identified from
  the private overlay network's identity headers, set by the proxy that
  publishes the application. A label or flag cannot be saved without an
  identified person.
- **APP-5 (MUST)** Every label and flag records the data release that
  was on screen and the application version.
- **APP-6 (MUST)** Applications are tested by calling their data and
  rendering functions directly and by HTTP smoke checks.

### Explorer

- **APP-10 (MUST)** The explorer shows, for a chosen bus and day, one
  synchronized view: the track on a map colored by activity, the pattern
  and its stops, and a timeline with activities, runs, operator trip
  records, taps, and stop events. Declared and observed route and
  direction are shown side by side.
- **APP-11 (MUST)** Every object on screen (a trip record, a run, an
  activity, a tap's boarding stop, a stop event, a bus and device link,
  a pattern) has a "flag this" action that takes a flag type and a
  comment.

### Labeling

- **APP-12 (MUST)** The labeling application presents queues:

| Queue | Question | Produces |
| --- | --- | --- |
| Trip service | Is this trip record revenue service? | `labels.trip_service_label` |
| Link review | Is this device on this bus? | `labels.bus_device_label` |
| Timeline annotation | What was this bus doing all day? | `labels.timeline_annotation` |
| Proposal review | Accept or reject a proposed pattern or rule exception | `labels.proposal_review` |
| Code meaning | Is this fare code a passenger code? | `labels.code_meaning_review` |
| Flag triage | Is this flag right? | An update to the flag, and a label when confirmed |
| Adjudication | The model and a label disagree: which is right? | `labels.adjudication` |

- **APP-13 (MUST)** Items reach a queue through named streams:
  `uncertainty` (cases the model is least sure of), `random` (a uniform
  sample), and `flag` (from confirmed user flags). At least
  `label.random_share` (default 20%) of labeling effort goes to the
  random stream.
- **APP-14 (MUST)** For the random stream, the model's prediction is
  hidden until the label is saved, so the label is an independent
  judgment. For other streams it may be shown. Whether it was shown, and
  what it was, is recorded on every label.
- **APP-15 (MUST)** "Not sure" is always an available answer.

## 2. Labels

- **APP-20 (MUST)** Human judgments are stored in the `labels` schema,
  one table per kind of judgment. They are the system of record
  (`ARC-5`).
- **APP-21 (MUST)** A label refers to its subject by natural key as well
  as by identifier (for a trip record: service date, bus, open and close
  times, declared route), so that it stays valid if identifiers ever
  change.
- **APP-22 (MUST)** Label tables are append-only. A correction is a new
  row with `supersedes_label_id`. A view per table exposes the current
  labels. Nothing is updated or deleted (`SEC-8`).
- **APP-23 (MUST)** Every label row carries:

| Column | Meaning |
| --- | --- |
| `label_id` | Identifier |
| `created_by`, `created_at` | Who and when (`APP-4`) |
| `stream` | `random`, `uncertainty`, `flag`, or `import` |
| `shown_prediction`, `predicted_value`, `predicted_confidence` | What the person saw |
| `split` | `train`, `calibration`, or `test`, assigned at creation and never changed |
| `is_frozen` | Whether the label belongs to the frozen test set |
| `release_id`, `app_version` | Context (`APP-5`) |
| `supersedes_label_id` | The label this one corrects, if any |
| `duration_s`, `note` | Time taken; free text |

- **APP-24 (MUST)** Only labels from the `random` stream, made with the
  prediction hidden, may be assigned to `test` and frozen. This keeps
  the frozen test set an unbiased sample.
- **APP-25 (MUST)** `opa labels snapshot` exports every label table to
  `exports/labels/<timestamp>/` on the BULK tier with a content digest,
  and loads it into the lake so that inference reads labels from there
  (`ARC-2`). A model
  release records the digest of the snapshot it was trained on.
- **APP-26 (MUST)** Labels can be imported from files that follow the
  import contract: natural key, verdict, author, time, and whatever is
  known about how the label was made (stream, whether a prediction was
  shown, original split). Imported labels have `stream = import`. An
  imported label is eligible for the frozen test set only if it was a
  random sample.

### Label tables

| Table | Subject | Verdict |
| --- | --- | --- |
| `trip_service_label` | A trip record | `revenue_service`, `non_revenue`, `unsure`, with an optional reason |
| `bus_device_label` | A bus, a device, a day or interval | `match`, `no_match`, `unsure` |
| `timeline_annotation` | A bus-day or device-day | An ordered list of segments: start, end, activity type, pattern |
| `proposal_review` | A pattern or rule proposal | `accept`, `reject`, with a note |
| `code_meaning_review` | A fare code | Fare class and label, with evidence |
| `adjudication` | A disagreement between a model output and a label | Which is right, and why |

## 3. Feedback

- **APP-30 (MUST)** `feedback.flag` has one row per flag: the object's
  type and identifier, its date, the flag type, a comment, who raised it
  and when, the data release, and a status.

| Status | Meaning |
| --- | --- |
| `open` | Not yet looked at |
| `confirmed` | A reviewer agrees; a label was created |
| `rejected` | A reviewer disagrees; the reason is recorded |
| `duplicate` | Same as another flag |
| `resolved` | A later release no longer shows the problem |

- **APP-31 (MUST)** A flag is a report, not a label. It becomes a label
  only when a reviewer confirms it in the triage queue. People flag what
  looks unusual, so flags are a biased sample; confirmed flags feed
  training and never the frozen test set.
- **APP-32 (MUST)** Each new release re-examines confirmed flags and
  marks those it resolves, naming the release.
- **APP-33 (MUST)** A person can see the status of the flags they
  raised.

## 4. The improvement loop

```text
  use the data --> flag --> triage --> label -----------+
                                                        |
        +-----------------------------------------------+
        v
  train or tune --> evaluate on the frozen test set --> gates
        |                                                 |
        |  fail: keep as candidate, report why            | pass
        v                                                 v
  (nothing changes)                               model release
                                                          |
                                                          v
                          materialize --> data release --> publish
```

- **APP-40 (MUST)** The loop runs for as long as the system is used. It
  has no end state.
- **APP-41 (MUST)** Training or tuning is an orchestrator job, started on
  a schedule (monthly) or when at least `loop.retrain_min_labels`
  (default 100) new labels exist, whichever comes first.
- **APP-42 (MUST)** Training and tuning use only labels that are not
  frozen. Evaluation uses the frozen test set and the independent checks
  of `INF-93`.
- **APP-43 (MUST)** A candidate model, parameter change, or rule change
  becomes a model release only if it passes every gate of `INF-95`,
  including no regression against the current release. A candidate that
  fails is kept with its evaluation report and changes nothing.
- **APP-44 (MUST)** A model release is recorded in `meta.model_release`
  with its version, the label snapshot digest, the code version, the
  parameters, and its metrics. Its artifact is stored in the model store
  with a digest.
- **APP-45 (MUST)** A model release reaches published data only through
  rematerialized months and a new data release (`ARC-40`). Published
  numbers never change underneath a reader.
- **APP-46 (MUST)** The loop's own health is tracked: flags opened and
  closed, time to triage, labels added, size of the frozen test set,
  rate of disagreement between models and labels, and the trend of every
  gated metric across releases.
- **APP-47 (MUST)** The frozen test set grows. Each month, labeling adds
  random-stream items to it, so that evaluation keeps pace with new
  periods and new kinds of cases.

## 5. Acceptance

1. A flag raised in the explorer appears in the triage queue, is
   confirmed, becomes a label, and is marked resolved by a later release.
2. A candidate that is worse than the current release on the frozen test
   set is refused by `opa release gate`.
3. Label tables reject updates and deletes from application roles.
4. A label snapshot can be restored into an empty database and
   reproduces the same digest.
