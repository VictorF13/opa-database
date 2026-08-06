"""Manual labeler for buses build_vehicle_identity.py couldn't resolve.

For one bus number at a time, shows one of that bus's real November trips
(>=15 minutes, so there's enough GPS signal to judge from) plotted against
its GTFS route, alongside every AVL vehicle_id active during that exact
trip window. You look at the map and decide which candidate (if any) is
plausibly the real bus.

Ranking uses trip_finder's own already-trained model (validity_model.joblib
/ completeness_model.joblib in tools/trip_finder/model_store, loaded once
at startup -- no training happens in this app, only inference), scored
against EVERY active candidate in the window, not just a geometrically
pre-filtered handful: filtering to "top 10 by raw distance" before scoring
would silently exclude a candidate that only looks right once the model
weighs distance, correlation, speed profile, and timing together -- the
model can only rank what it's actually shown. The 23-feature vector matches
tools/trip_finder/predict_trips.py's FEATURE_COLUMNS exactly (kept in sync
by hand, not imported, same reasoning predict_trips.py itself gives for not
importing from trip_finder/app.py); most of it comes for free from
aggregate_trip_candidates (already computed for every candidate) or is
cheap and cacheable per-route (iv_overlap_m, shape_start_end_dist_m) or
per-trip (the speed-percentile lookup against
scratch.trip_shape_samples_scored). Candidates already excluded from
consideration in the original model's own training data (another confident
trip claims them for an overlapping time window --
scratch.trip_match_predictions' success intervals) are excluded here too,
for consistency with what the model actually learned "candidate" to mean.

The raw geometric stats (distance-to-line, start/end proximity, progress
correlation -- same tools/trip_finder/scoring.py functions used for its own
spot-check) are still shown alongside the model's probability, not
replaced by it: the model ranks and pre-selects the top MAX_CANDIDATES_SHOWN,
but you're still the one deciding, with both signals in front of you.

This is the same "agreement across many trips" idea build_vehicle_identity.py
already used on the *automatic* prediction sources, just extended to trips a
human is willing to look at directly. Labeling more than one trip for the
same bus is expected and useful: one trip's geometry can be ambiguous (a
short overlap, noisy GPS), but a candidate that keeps coming up as the right
one across several independently-reviewed trips is strong, self-consistent
evidence -- exactly the same statistical argument the automatic dictionary
was built on, just with your eyes doing the discrimination instead of a
trust_tier threshold.

Each candidate is shown as its own ida/volta map pair (same convention as
trip_finder/static/index.html: the route drawn first with its GTFS
start/end permanently labeled, the candidate's own pings on top colored
white->black by time) so you can see, per shown vehicle_id, whether its
track actually walks the route in the direction implied. Every candidate
also carries a live confidence figure -- _combined_confidence pools every
confident automatic prediction ever made for this bus (across every
candidate it ever pointed to, not just today's) with every trip you've
manually reviewed, and reports a Wilson-score lower-confidence-bound on top
of the raw agreement fraction, since a raw 1-for-1 fraction and a 50-for-50
fraction shouldn't read as equally certain.

Bus queue order: buses already in scratch.november_2023_vehicle_identity
(the ones build_vehicle_identity.py already resolved automatically) are
never shown. Among the rest, buses with the FEWEST distinct candidate
possibilities come first -- a bus with 3000 trials split across 2
candidates is a much easier call than one with 10 trials split across 10,
so this clears the easy cases fastest rather than dwelling on the buses
with the most raw evidence volume. Buses with zero possibilities at all
sort last. Within a bus, trips are shown longest-GTFS-route first (more of
the road to actually judge a candidate's track against), not longest
wall-clock duration. Move on explicitly with the "next bus" button whenever
you're satisfied (or stuck) -- there's no automatic promotion out of this
app; that happens later, by re-aggregating scratch.vehicle_identity_labels
the same way build_vehicle_identity.py aggregates the automatic sources.

Writes only to a BRAND NEW table, scratch.vehicle_identity_labels. Never
touches trip_match_predictions, trip_finder_predictions,
november_2023_vehicle_identity, or any other existing table.

Prefetching: the slow part of serving a trip is candidates_for_trip (every
AVL ping in the window) plus scoring every active vehicle against the
route -- too slow to do while you're sitting there waiting to see the next
trip. So the moment a bus becomes current (at startup or via "next bus"),
IdentityStore.start_prefetch resolves and orders that bus's entire
qualifying trip list (cheap, one batched query -- see qualifying_trips) and
hands it to a background thread (_prefetch_worker, its own Postgres
connection so it never blocks the request-handling one) that scores trips
in serving order, longest GTFS route first. The worker deliberately only
stays PREFETCH_AHEAD trips ahead of serve_index rather than racing through
every trip in the list at once -- a bus can have hundreds of qualifying
trips, and letting the worker fire that many heavy avl_pings scans back to
back saturated Postgres badly enough to stall the very request waiting on
the current trip (measured: ~53s instead of ~1s). By the time you submit
one trip's label, the next one or two are usually already sitting in
trip_cache and /api/next returns instantly; if you're faster than the
prefetcher, it just falls back to scoring that one trip inline.

Run with:
    uv run tools/vehicle_identity_labeler/app.py

Then open http://localhost:8012
"""

from __future__ import annotations

import json
import math
import sys
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import joblib
import numpy as np
import psycopg
import pyproj
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))
# find_candidates.py itself does `from scoring import ...` (sibling-style,
# assuming its own directory is on sys.path, not tools/) -- add that too.
sys.path.insert(0, str(Path(__file__).parent.parent / "trip_finder"))
from trip_finder.find_candidates import (
    aggregate_trip_candidates,
    fetch_success_intervals,
)
from trip_finder.scoring import (
    UTM_24S,
    ShapeGeom,
    iv_overlap_m,
    load_shapes,
    local_fortaleza,
    project_pings,
    shape_start_end_dist_m,
)

if TYPE_CHECKING:
    from sklearn.ensemble import HistGradientBoostingClassifier

DSN = "postgresql://opa:opa@localhost:5432/opa"
MODEL_DIR = Path(__file__).parent.parent / "trip_finder" / "model_store"
MIN_TRIP_MINUTES = 15
MIN_PINGS_FOR_CANDIDATE = 3
MAX_CANDIDATES_SHOWN = 10

# Kept in sync by hand with tools/trip_finder/predict_trips.py's
# FEATURE_COLUMNS -- same reasoning predict_trips.py itself documents for not
# importing this from trip_finder/app.py: importing app.py would execute its
# module-level `store = LabelStore(DSN)` and spin up that entire live app.
FEATURE_COLUMNS = [
    "n_pings_in_window",
    "ida_avg_dist_to_line_m",
    "ida_progress_corr",
    "ida_start_proximity_m",
    "ida_end_proximity_m",
    "volta_avg_dist_to_line_m",
    "volta_progress_corr",
    "volta_start_proximity_m",
    "volta_end_proximity_m",
    "duration_sec",
    "day_of_week",
    "hour_of_trip_start",
    "hour_of_trip_end",
    "iv_overlap_m",
    "ida_shape_start_end_dist_m",
    "volta_shape_start_end_dist_m",
    "ida_implied_speed_kmh",
    "ida_speed_percentile",
    "volta_implied_speed_kmh",
    "volta_speed_percentile",
    "total_distance_m",
    "ping_timespan_sec",
    "spatial_dispersion_m",
]

MAX_BATCH_TRIPS_PER_BUS = 40
# How many rendered (shapes+pings) instances /api/rapid/next sends up front,
# and how many more /api/rapid/page sends per subsequent page -- kept small
# since rendering a trip means real DB round trips (shapes_latlon_for,
# single_candidate_pings), not just a JSONB slice. The full stored pool per
# candidate is every winning trip up to MAX_BATCH_TRIPS_PER_BUS, so a
# candidate that dominated many trips has real depth to page through.
INSTANCES_PER_PAGE = 2

# FULL means the model saw the whole leg (start to end); PARTIAL means it
# only caught part of it -- a weaker, less legible trip to review on a map.
# NOT_THIS_ROUTE (or anything else predict_trips doesn't emit) sorts last.
_COMPLETENESS_RANK = {
    "IDA_FULL": 0,
    "VOLTA_FULL": 0,
    "IDA_PARTIAL": 1,
    "VOLTA_PARTIAL": 1,
}


def _completeness_rank(completeness: str | None) -> int:
    """Rank a predicted completeness class, FULL first, PARTIAL next, else last."""
    return _COMPLETENESS_RANK.get(completeness, 2)


def _diversify_by_route(
    wins: list[tuple[dict[str, Any], float, str | None, float | None]],
) -> list[tuple[dict[str, Any], float, str | None, float | None]]:
    """Reorder wins so distinct routes come first, without dropping any.

    A given AVL vehicle overwhelmingly runs one route all day, so a strict
    one-instance-per-route dedup left most candidates with exactly one
    stored instance no matter how much evidence backed them (observed: a
    candidate that won 15/40 scored trips collapsed to a single instance).
    This keeps every winning trip -- interleaved round-robin across
    line_number, in each line's incoming (completeness, then probability)
    order -- so a bus running several routes shows different ones first,
    but a bus/candidate that only ever ran one route still has real depth
    to page through.
    """
    by_line: dict[
        str, list[tuple[dict[str, Any], float, str | None, float | None]]
    ] = {}
    for win in wins:
        by_line.setdefault(win[0]["line_number"], []).append(win)
    queues = list(by_line.values())
    interleaved = []
    for i in range(max((len(q) for q in queues), default=0)):
        interleaved.extend(q[i] for q in queues if i < len(q))
    return interleaved


def _order_trips(trips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order trips longest-GTFS-route-first, round-robin across lines.

    Within each line, longest route first; across lines, round-robin
    (most-trips-remaining line first so a line with a long tail doesn't
    get starved behind short ones).

    Shared by start_prefetch (interactive review's serving order) and
    _batch_score_bus (which additionally caps to the first
    MAX_BATCH_TRIPS_PER_BUS of this order -- see that function's docstring
    for why scoring literally every trip isn't necessary or worth the time).
    """
    by_line: dict[str, list[dict[str, Any]]] = {}
    for trip in trips:
        by_line.setdefault(trip["line_number"], []).append(trip)
    for line_trips in by_line.values():
        line_trips.sort(
            key=lambda t: (
                t["_shape_length_m"],
                t["trip_closed_at"] - t["trip_opened_at"],
            ),
            reverse=True,
        )
    queues = sorted(by_line.values(), key=len, reverse=True)
    interleaved: list[dict[str, Any]] = []
    for i in range(max((len(q) for q in queues), default=0)):
        interleaved.extend(q[i] for q in queues if i < len(q))
    return interleaved


app = FastAPI()


class LabelCandidateIn(BaseModel):
    """One candidate's shown stats plus the user's decision, as submitted."""

    candidate_vehicle_id: int
    candidate_device_id: str | None
    n_pings_in_window: int
    best_avg_dist_to_line_m: float | None
    best_direction: str | None
    selected: bool


class LabelIn(BaseModel):
    """A full labeling submission for one (bus, trip) pair."""

    vehicle_number: str
    line_number: str
    trip_opened_at: datetime
    trip_closed_at: datetime
    candidates: list[LabelCandidateIn]


class IdentityStore:
    """Postgres connection plus the tiny bit of session state this app needs.

    Tracks which bus is currently being reviewed, which buses have been
    explicitly skipped/exhausted this session so "next bus" doesn't loop
    back to them immediately, and the prefetch queue/cache (see the module
    docstring's "Prefetching" section).
    """

    def __init__(self, dsn: str) -> None:
        """Connect to Postgres (main + a dedicated prefetch connection)."""
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.prefetch_conn = psycopg.connect(dsn, autocommit=True)
        # Exclusively for /api/rapid/* request handlers -- each _batch_worker
        # thread opens and owns its own connection instead (see
        # start_batch_workers), so a rapid-review click never has to queue
        # up behind whatever the background pass is doing, and psycopg
        # connections aren't safe for concurrent use across threads anyway.
        self.rapid_conn = psycopg.connect(dsn, autocommit=True)
        # Bus numbers some _batch_worker thread has already claimed, so a
        # second worker's next_unscored_bus call doesn't pick the same one
        # before the first has finished (and written to batch_progress).
        self.batch_in_progress: set[str] = set()
        # (bus, candidate) pairs skipped this session in the rapid-review
        # flow -- unlike deny (permanent, scratch.vehicle_identity_batch_
        # rejected), this is just "not right now", so /api/rapid/next
        # excludes it in-memory without writing anything durable.
        self.rapid_skipped: set[tuple[str, int]] = set()
        self.transformer = pyproj.Transformer.from_crs(
            "EPSG:4326", f"EPSG:{UTM_24S}", always_xy=True
        )
        self.shape_cache: dict[
            tuple[date, str], dict[tuple[date, str, str], ShapeGeom]
        ] = {}
        self.route_feature_cache: dict[tuple[date, str], dict[str, float]] = {}
        self.current_bus: str | None = None
        self.skipped_this_session: set[str] = set()
        self.trip_list: list[dict[str, Any]] = []
        self.serve_index = 0
        self.trip_cache: dict[tuple[str, str, datetime, datetime], dict[str, Any]] = {}
        self.prefetch_generation = 0
        # Per-bus model tally: candidate_vehicle_id -> how many trips (out of
        # every trip scored so far, served or not) the model's own top pick
        # was this candidate with >= MODEL_TALLY_MIN_PROB. Reset in
        # start_prefetch. model_tally_seen prevents double-counting a trip
        # that gets scored via more than one path (prefetch worker, sync
        # fallback, or a cache hit).
        self.model_tally: dict[int, int] = {}
        self.model_tally_trips_scored = 0
        self.model_tally_seen: set[tuple[str, str, datetime, datetime]] = set()
        self._ensure_schema()
        self.claimed_vehicle_ids = self._load_claimed_vehicle_ids()

        self.validity_model: HistGradientBoostingClassifier = joblib.load(
            MODEL_DIR / "validity_model.joblib"
        )
        completeness_path = MODEL_DIR / "completeness_model.joblib"
        self.completeness_model: HistGradientBoostingClassifier | None = (
            joblib.load(completeness_path) if completeness_path.exists() else None
        )
        # A vehicle_id already confidently claimed by ANOTHER trip during an
        # overlapping time window can't also be this trip's candidate -- same
        # exclusion find_candidates.py applies before generating training
        # data, so the model was never shown these as valid options either.
        self.succ_vehicle, self.succ_start, self.succ_end = fetch_success_intervals(
            self.conn
        )

    def _load_claimed_vehicle_ids(self) -> set[int]:
        """avl_vehicle_ids already confidently claimed by a DIFFERENT bus.

        Pools scratch.november_2023_vehicle_identity (the strong, >=90%
        agreement/>=3 trips automatic dictionary) with
        scratch.vehicle_identity_confirmed (this app's own manual
        confirmations) -- a vehicle_id in either is already someone else's
        real bus for this month, so it should never be offered as a
        "possibility" for a still-unresolved one. Loaded once at startup
        for the automatic table (doesn't change mid-session); api_confirm
        adds newly-confirmed ids to this set live as you go, rather than
        re-querying here each time.
        """
        rows = self.conn.execute(
            """
            SELECT avl_vehicle_id FROM scratch.november_2023_vehicle_identity
            WHERE avl_vehicle_id IS NOT NULL
            UNION
            SELECT avl_vehicle_id FROM scratch.vehicle_identity_confirmed
            WHERE avl_vehicle_id IS NOT NULL
            """
        ).fetchall()
        return {r[0] for r in rows}

    def _ensure_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scratch.vehicle_identity_labels (
                vehicle_number text NOT NULL,
                line_number text NOT NULL,
                trip_opened_at timestamptz NOT NULL,
                trip_closed_at timestamptz NOT NULL,
                candidate_vehicle_id integer NOT NULL,
                candidate_device_id text,
                n_pings_in_window integer NOT NULL,
                best_avg_dist_to_line_m double precision,
                best_direction text,
                selected boolean NOT NULL,
                labeled_at timestamptz NOT NULL,
                PRIMARY KEY (
                    vehicle_number, trip_opened_at, trip_closed_at, candidate_vehicle_id
                )
            )
            """
        )
        # Deliberately separate from scratch.november_2023_vehicle_identity:
        # that table is DROP+CREATE rebuilt from scratch every time
        # build_vehicle_identity.py runs, so a manual confirmation written
        # directly into it would just get wiped on the next automatic
        # rebuild. This table is this app's own durable record of "you
        # personally signed off on this one" -- build_vehicle_identity.py
        # was updated to fold these in as a third evidence source whenever
        # it rebuilds, so a confirmation here does eventually make it into
        # the canonical dictionary, without this app ever touching that
        # table directly.
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scratch.vehicle_identity_confirmed (
                vehicle_number text PRIMARY KEY,
                avl_vehicle_id integer,
                device_id text,
                agreement_pct double precision,
                confidence_lower_bound double precision,
                total_trials integer,
                confirmed_at timestamptz NOT NULL
            )
            """
        )
        # Rapid-review batch pass (see _batch_worker): one row per (bus,
        # candidate) that ever won as the model's top pick on at least one
        # trip, with its best up-to-4 trip instances for on-demand map
        # rendering. Rebuilt in place per bus (DELETE + re-INSERT) each time
        # _batch_score_bus reruns that bus, not append-only.
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scratch.vehicle_identity_batch_scores (
                vehicle_number text NOT NULL,
                candidate_vehicle_id integer NOT NULL,
                candidate_device_id text,
                n_trips_scored integer NOT NULL,
                n_trips_as_top_pick integer NOT NULL,
                avg_top_probability double precision,
                instances jsonb NOT NULL,
                scored_at timestamptz NOT NULL,
                PRIMARY KEY (vehicle_number, candidate_vehicle_id)
            )
            """
        )
        # A bus this app has already batch-scored at least once -- lets the
        # worker skip it on subsequent passes without re-deriving that from
        # batch_scores (which could have zero rows for a bus with no
        # confident candidates at all, indistinguishable from "not
        # processed yet" otherwise).
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scratch.vehicle_identity_batch_progress (
                vehicle_number text PRIMARY KEY,
                scored_at timestamptz NOT NULL
            )
            """
        )
        # A (bus, candidate) pair you explicitly said "no" to in the rapid
        # review flow -- excluded from future /api/rapid/next picks for
        # that bus so the same rejected candidate never resurfaces.
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scratch.vehicle_identity_batch_rejected (
                vehicle_number text NOT NULL,
                candidate_vehicle_id integer NOT NULL,
                rejected_at timestamptz NOT NULL,
                PRIMARY KEY (vehicle_number, candidate_vehicle_id)
            )
            """
        )

    def next_bus(self) -> str | None:
        """Pick the not-yet-resolved bus with the fewest candidate possibilities.

        Easiest to disambiguate first, not the one with the most raw
        evidence volume (a bus with 3000 trials split across 2 candidates
        is an easier call than one with 10 trials split across 10).

        "Possibilities" is the same vehicle_id-space evidence pool
        prior_evidence/_combined_confidence already use: confident
        (high_confidence_valid) trip_labeler 'vehicle'-source and
        trip_finder predictions, plus your own manual labels here so far.
        Buses with zero possibilities at all (nothing to narrow down from)
        sort last, same as before.
        """
        row = self.conn.execute(
            """
            WITH evidence AS (
                SELECT vehicle_number, entity_id::integer AS avl_vehicle_id
                FROM scratch.trip_match_predictions
                WHERE source = 'vehicle' AND trust_tier = 'high_confidence_valid'
                UNION ALL
                SELECT vehicle_number, predicted_candidate_vehicle_id
                FROM scratch.trip_finder_predictions
                WHERE trust_tier = 'high_confidence_valid'
                  AND predicted_candidate_vehicle_id IS NOT NULL
                UNION ALL
                SELECT vehicle_number, candidate_vehicle_id
                FROM scratch.vehicle_identity_labels
                WHERE candidate_vehicle_id != -1
            ),
            per_bus AS (
                SELECT vehicle_number,
                       count(*) AS n_trials,
                       count(DISTINCT avl_vehicle_id) AS n_distinct
                FROM evidence
                GROUP BY vehicle_number
            ),
            all_buses AS (
                SELECT DISTINCT vehicle_number FROM silver.afc_boardings
                WHERE trip_opened_at >= '2023-11-01' AND trip_opened_at < '2023-12-01'
            )
            SELECT a.vehicle_number
            FROM all_buses a
            LEFT JOIN per_bus p USING (vehicle_number)
            WHERE a.vehicle_number NOT IN (
                SELECT vehicle_number FROM scratch.november_2023_vehicle_identity
            )
            AND a.vehicle_number NOT IN (
                SELECT vehicle_number FROM scratch.vehicle_identity_confirmed
            )
            AND a.vehicle_number != ALL(%(skipped)s)
            ORDER BY
                COALESCE(p.n_distinct, 0) = 0,
                COALESCE(p.n_distinct, 999999) ASC,
                COALESCE(p.n_trials, 0) DESC,
                a.vehicle_number
            LIMIT 1
            """,
            {"skipped": list(self.skipped_this_session)},
        ).fetchone()
        return row[0] if row else None

    def next_unscored_bus(
        self, conn: psycopg.Connection, *, also_exclude: set[str]
    ) -> str | None:
        """Pick the not-yet-batch-scored bus with the fewest possibilities.

        also_exclude is store.batch_in_progress, passed explicitly rather
        than read directly so this stays a pure query given its inputs --
        with several _batch_worker threads running concurrently, a bus
        another thread has already claimed (mid-scoring, not yet written
        to batch_progress) must also be skipped, or two threads would
        redundantly score the same bus.

        Same fewest-possibilities-first ordering as next_bus (see its
        docstring), but scoped to scratch.vehicle_identity_batch_progress
        instead of skipped_this_session -- this is a durable, continuously
        running background pass (_batch_worker), not the interactive
        per-session queue, so "already handled" needs to persist across
        restarts rather than reset each session.

        Also tiebreaks on fewest November trips (ascending), between the
        possibilities-count tier and the trials tiebreak: a bus with a
        genuinely huge trip count is usually a shared/generic AFC code
        rather than one real physical bus (observed one with ~1200 trips
        in a single month), and batch-scoring it takes proportionally
        long -- this keeps the worker moving through many normal-sized
        buses instead of stalling on one pathological one first.
        """
        row = conn.execute(
            """
            WITH evidence AS (
                SELECT vehicle_number, entity_id::integer AS avl_vehicle_id
                FROM scratch.trip_match_predictions
                WHERE source = 'vehicle' AND trust_tier = 'high_confidence_valid'
                UNION ALL
                SELECT vehicle_number, predicted_candidate_vehicle_id
                FROM scratch.trip_finder_predictions
                WHERE trust_tier = 'high_confidence_valid'
                  AND predicted_candidate_vehicle_id IS NOT NULL
                UNION ALL
                SELECT vehicle_number, candidate_vehicle_id
                FROM scratch.vehicle_identity_labels
                WHERE candidate_vehicle_id != -1
            ),
            per_bus AS (
                SELECT vehicle_number,
                       count(*) AS n_trials,
                       count(DISTINCT avl_vehicle_id) AS n_distinct
                FROM evidence
                GROUP BY vehicle_number
            ),
            all_buses AS (
                SELECT vehicle_number,
                       count(DISTINCT (line_number, trip_opened_at, trip_closed_at))
                           AS n_trips
                FROM silver.afc_boardings
                WHERE trip_opened_at >= '2023-11-01' AND trip_opened_at < '2023-12-01'
                GROUP BY vehicle_number
            )
            SELECT a.vehicle_number
            FROM all_buses a
            LEFT JOIN per_bus p USING (vehicle_number)
            WHERE a.vehicle_number NOT IN (
                SELECT vehicle_number FROM scratch.november_2023_vehicle_identity
            )
            AND a.vehicle_number NOT IN (
                SELECT vehicle_number FROM scratch.vehicle_identity_confirmed
            )
            AND a.vehicle_number NOT IN (
                SELECT vehicle_number FROM scratch.vehicle_identity_batch_progress
            )
            AND a.vehicle_number != ALL(%(also_exclude)s)
            ORDER BY
                COALESCE(p.n_distinct, 0) = 0,
                COALESCE(p.n_distinct, 999999) ASC,
                a.n_trips ASC,
                COALESCE(p.n_trials, 0) DESC,
                a.vehicle_number
            LIMIT 1
            """,
            {"also_exclude": list(also_exclude)},
        ).fetchone()
        return row[0] if row else None

    def single_candidate_pings(
        self,
        vehicle_id: int,
        trip_opened_at: datetime,
        trip_closed_at: datetime,
        conn: psycopg.Connection,
    ) -> list[dict[str, Any]]:
        """Return one candidate's own pings for one trip window.

        For rapid-review map rendering, where the candidate is already
        known (from scratch.vehicle_identity_batch_scores) rather than
        being discovered by scanning every active vehicle. A direct
        (vehicle_id, metric_timestamp) index range scan, not the city-wide
        candidates_for_trip query the interactive flow needs.
        """
        rows = conn.execute(
            """
            SELECT metric_timestamp, longitude, latitude
            FROM silver.avl_pings
            WHERE vehicle_id = %(vid)s
              AND metric_timestamp BETWEEN %(start)s AND %(end)s
            ORDER BY metric_timestamp
            """,
            {"vid": vehicle_id, "start": trip_opened_at, "end": trip_closed_at},
        ).fetchall()
        return [{"t": t.isoformat(), "lon": lon, "lat": lat} for t, lon, lat in rows]

    def qualifying_trips(
        self,
        vehicle_number: str,
        conn: psycopg.Connection,
        *,
        exclude_labeled: bool = True,
    ) -> list[dict[str, Any]]:
        """Return this bus's >=15min November trips.

        exclude_labeled=True (the interactive-review default) skips trips
        already manually labeled in this app; the batch-scoring pass
        (_batch_score_bus) wants exclude_labeled=False, since the model
        should see every trip regardless of what's already been reviewed
        by hand.

        Each trip's GTFS feed and longest ida/volta shape length are
        resolved here too, but via a dedupe-then-batch-join, not a LATERAL
        per trip row: a first attempt joined route_shape_geoms once per
        trip (find_candidates.py's own fetch_bad_trips pattern) and that
        alone took ~26s for one 353-trip bus -- the per-row correlated
        ORDER BY/LIMIT fallback doesn't get an efficient index lookup, and
        with hundreds of trips usually sharing only a handful of distinct
        (line, date) pairs, resolving the full row set was pure waste.
        Batching over the distinct pairs (same unnest() pattern
        scoring.load_shapes already uses for multi-key lookups) cut this to
        two small queries.

        Trips whose line has no shape in any feed at all are dropped here
        and sentinel-written immediately (exclude_labeled=True only -- the
        batch pass has no use for that bookkeeping and shouldn't write to
        scratch.vehicle_identity_labels), so start_prefetch and the
        background worker never have to special-case them.
        """
        exclusion_clause = (
            """
              AND NOT EXISTS (
                  SELECT 1 FROM scratch.vehicle_identity_labels l
                  WHERE l.vehicle_number = b.vehicle_number
                    AND l.trip_opened_at = b.trip_opened_at
                    AND l.trip_closed_at = b.trip_closed_at
              )
            """
            if exclude_labeled
            else ""
        )
        trip_rows = conn.execute(
            f"""
            SELECT DISTINCT vehicle_number, line_number, trip_opened_at, trip_closed_at
            FROM silver.afc_boardings b
            WHERE vehicle_number = %(vn)s
              AND trip_opened_at >= '2023-11-01' AND trip_opened_at < '2023-12-01'
              AND trip_closed_at - trip_opened_at >= interval '%(min)s minutes'
            {exclusion_clause}
            """,  # noqa: S608
            {"vn": vehicle_number, "min": MIN_TRIP_MINUTES},
        ).fetchall()
        if not trip_rows:
            return []
        trips = [
            {
                "vehicle_number": r[0],
                "line_number": r[1],
                "trip_opened_at": r[2],
                "trip_closed_at": r[3],
            }
            for r in trip_rows
        ]

        pairs = {(t["line_number"], t["trip_opened_at"].date()) for t in trips}
        feed_rows = conn.execute(
            """
            SELECT want.line, want.d,
                   COALESCE(r.resolved_feed_version_date, fb.fallback_feed) AS feed
            FROM unnest(%(lines)s::text[], %(dates)s::date[]) AS want(line, d)
            LEFT JOIN scratch.trip_feed_resolution r
              ON r.line_number = want.line AND r.trip_date = want.d
            LEFT JOIN LATERAL (
                SELECT g.feed_version_date AS fallback_feed
                FROM scratch.route_shape_geoms g
                WHERE g.line_number = want.line
                  AND r.resolved_feed_version_date IS NULL
                ORDER BY abs(g.feed_version_date - want.d)
                LIMIT 1
            ) fb ON r.resolved_feed_version_date IS NULL
            """,
            {"lines": [p[0] for p in pairs], "dates": [p[1] for p in pairs]},
        ).fetchall()
        feed_by_pair = {(ln, d): feed for ln, d, feed in feed_rows}

        feed_lines = {
            (feed_by_pair[p], p[0]) for p in pairs if feed_by_pair.get(p) is not None
        }
        length_by_feed_line: dict[tuple[date, str], float] = {}
        if feed_lines:
            length_rows = conn.execute(
                """
                SELECT want.feed, want.line, max(g.shape_length_m)
                FROM unnest(%(feeds)s::date[], %(lines)s::text[]) AS want(feed, line)
                JOIN scratch.route_shape_geoms g
                  ON g.feed_version_date = want.feed AND g.line_number = want.line
                GROUP BY want.feed, want.line
                """,
                {
                    "feeds": [p[0] for p in feed_lines],
                    "lines": [p[1] for p in feed_lines],
                },
            ).fetchall()
            length_by_feed_line = {(f, ln): float(m or 0.0) for f, ln, m in length_rows}

        out: list[dict[str, Any]] = []
        for trip in trips:
            pair = (trip["line_number"], trip["trip_opened_at"].date())
            feed = feed_by_pair.get(pair)
            if feed is None:
                if exclude_labeled:
                    self.write_sentinel(trip, conn)
                continue
            trip["resolved_feed_version_date"] = feed
            trip["_shape_length_m"] = length_by_feed_line.get(
                (feed, trip["line_number"]), 0.0
            )
            out.append(trip)
        return out

    def shapes_for(
        self, feed_version_date: date, line_number: str, conn: psycopg.Connection
    ) -> dict[tuple[date, str, str], ShapeGeom]:
        """Load (and cache) a (feed, line)'s UTM-projected ida/volta shapes.

        For scoring only (compute_direction_metrics needs meters, not
        degrees) -- see shapes_latlon_for for the map-display version. Takes
        an explicit conn since this runs from both the request-handling
        thread and the background prefetch thread (its own connection).
        """
        key = (feed_version_date, line_number)
        if key not in self.shape_cache:
            self.shape_cache[key] = load_shapes(conn, [key])
        return self.shape_cache[key]

    def shapes_latlon_for(
        self, feed_version_date: date, line_number: str, conn: psycopg.Connection
    ) -> dict[str, list[tuple[float, float]]]:
        """Return shape_id -> [(lat, lon), ...] in WGS84, for map display only.

        scoring.load_shapes returns line_geom_proj (UTM 24S, meters) --
        plotting that directly on a lat/lon map would place the route
        thousands of kilometers off. This queries the plain line_geom
        column instead, same as trip_finder/app.py's own _fetch_shapes.
        """
        rows = conn.execute(
            """
            SELECT shape_id, ST_AsGeoJSON(line_geom)
            FROM scratch.route_shape_geoms
            WHERE feed_version_date = %(feed)s AND line_number = %(line)s
            """,
            {"feed": feed_version_date, "line": line_number},
        ).fetchall()
        out: dict[str, list[tuple[float, float]]] = {}
        for shape_id, geojson in rows:
            coords = json.loads(geojson)["coordinates"]
            out[shape_id] = [(lat, lon) for lon, lat in coords]
        return out

    def route_features_for(
        self,
        feed_version_date: date,
        line_number: str,
        shapes: dict[tuple[date, str, str], ShapeGeom],
    ) -> dict[str, float]:
        """Cache iv_overlap_m/ida+volta_shape_start_end_dist_m per (feed, line).

        These three model features describe the ROUTE, not any one trip or
        candidate -- computed once per (feed, line) and reused for every
        trip and every candidate that shares it, same caching discipline as
        shape_cache.
        """
        key = (feed_version_date, line_number)
        if key not in self.route_feature_cache:
            overlap = iv_overlap_m(shapes)
            start_end = shape_start_end_dist_m(shapes)
            self.route_feature_cache[key] = {
                "iv_overlap_m": overlap.get(key, np.nan),
                "ida_shape_start_end_dist_m": start_end.get((*key, "I"), np.nan),
                "volta_shape_start_end_dist_m": start_end.get((*key, "V"), np.nan),
            }
        return self.route_feature_cache[key]

    def speed_ref_for_trip(
        self, vehicle_number: str, trip_opened_at: datetime, conn: psycopg.Connection
    ) -> dict[str, tuple[float, float]]:
        """Return shape_id -> (implied_speed_kmh, speed_percentile) for one trip.

        Per-trip, not per-candidate or bulk-preloaded -- a point lookup
        against scratch.trip_shape_samples_scored's own
        (vehicle_number, trip_opened_at) index, same reference distribution
        find_candidates.py's fetch_speed_ref reads, just fetched one trip at
        a time here instead of bulk-loaded for a fixed trip list up front.
        """
        rows = conn.execute(
            """
            SELECT shape_id, implied_speed_kmh, speed_percentile
            FROM scratch.trip_shape_samples_scored
            WHERE vehicle_number = %(vn)s AND trip_opened_at = %(to)s
            """,
            {"vn": vehicle_number, "to": trip_opened_at},
        ).fetchall()
        return {shape_id: (speed, pct) for shape_id, speed, pct in rows}

    def prior_evidence(
        self, vehicle_number: str, conn: psycopg.Connection
    ) -> dict[int, int]:
        """Map candidate vehicle_id -> supporting confident-trip count for this bus.

        Drawn from the automatic sources (trip_labeler/trip_finder), used to
        force weakly-geometric but evidence-backed candidates into the shown
        list. Takes an explicit conn -- see shapes_for's docstring.
        """
        rows = conn.execute(
            """
            SELECT avl_vehicle_id, count(*) FROM (
                SELECT entity_id::integer AS avl_vehicle_id
                FROM scratch.trip_match_predictions
                WHERE source = 'vehicle' AND trust_tier = 'high_confidence_valid'
                  AND vehicle_number = %(vn)s
                UNION ALL
                SELECT predicted_candidate_vehicle_id
                FROM scratch.trip_finder_predictions
                WHERE trust_tier = 'high_confidence_valid'
                  AND predicted_candidate_vehicle_id IS NOT NULL
                  AND vehicle_number = %(vn)s
            ) x
            GROUP BY avl_vehicle_id
            """,
            {"vn": vehicle_number},
        ).fetchall()
        return dict(rows)

    def device_ids_for(self, avl_vehicle_ids: list[int]) -> dict[int, str]:
        """Map avl_vehicle_id -> a representative device_id, November 2023 only.

        Not scoped to any one trip -- used for the bus-level tally, which
        can rank a candidate the current trip's own window never even saw
        pings for. vehicle_id<->device_id pairing is highly stable (only
        9/1484 November vehicle_ids ever showed >1 device_id), so "most
        recent ping in November" is a safe, cheap stand-in for "the true
        mode".

        LATERAL + LIMIT 1 per vehicle_id, not a DISTINCT ON over the whole
        set: DISTINCT ON forced Postgres to index-scan every one of a
        vehicle's ~89k November pings and sort them just to keep one row,
        instead of an index-backward-scan straight to the answer. Measured
        stuck at 2+ minutes per call with no date filter at all (silver.
        avl_pings is partitioned by metric_timestamp, so that scanned every
        month of data too); with the date filter but still DISTINCT ON,
        1.2s warm/13.5s cold; this version measured 0.1ms. _bus_tally calls
        this on every /api/next, so this was the actual "submission is
        slow" bottleneck.
        """
        if not avl_vehicle_ids:
            return {}
        rows = self.conn.execute(
            """
            SELECT v.vid, sub.device_id
            FROM unnest(%(vids)s::integer[]) AS v(vid)
            CROSS JOIN LATERAL (
                SELECT device_id
                FROM silver.avl_pings
                WHERE vehicle_id = v.vid
                  AND metric_timestamp >= '2023-11-01'
                  AND metric_timestamp < '2023-12-01'
                ORDER BY metric_timestamp DESC
                LIMIT 1
            ) sub
            """,
            {"vids": avl_vehicle_ids},
        ).fetchall()
        return dict(rows)

    def manual_evidence(self, vehicle_number: str) -> tuple[int, dict[int, int]]:
        """Return (n_trips_manually_reviewed, {candidate_vehicle_id: n_selected}).

        n_trips_manually_reviewed is the trial denominator: every trip you've
        looked at counts once, regardless of how many candidates (0, 1, or
        several) you selected for it.
        """
        rows = self.conn.execute(
            """
            SELECT candidate_vehicle_id, count(*) FILTER (WHERE selected)
            FROM scratch.vehicle_identity_labels
            WHERE vehicle_number = %(vn)s AND candidate_vehicle_id != -1
            GROUP BY candidate_vehicle_id
            """,
            {"vn": vehicle_number},
        ).fetchall()
        n_trips = self.conn.execute(
            """
            SELECT count(DISTINCT (trip_opened_at, trip_closed_at))
            FROM scratch.vehicle_identity_labels
            WHERE vehicle_number = %(vn)s
            """,
            {"vn": vehicle_number},
        ).fetchone()
        return (n_trips[0] if n_trips else 0), dict(rows)

    def candidates_for_trip(
        self,
        trip_opened_at: datetime,
        trip_closed_at: datetime,
        conn: psycopg.Connection,
    ) -> dict[int, dict[str, Any]]:
        """Return every AVL vehicle active in the trip window with its pings.

        Keyed by vehicle_id; each entry also tracks the device_id(s) seen so
        the most-common one in this window can be reported per candidate.
        Takes an explicit conn -- see shapes_for's docstring.
        """
        rows = conn.execute(
            """
            SELECT vehicle_id, device_id, metric_timestamp, longitude, latitude
            FROM silver.avl_pings
            WHERE metric_timestamp BETWEEN %(start)s AND %(end)s
            ORDER BY vehicle_id, metric_timestamp
            """,
            {"start": trip_opened_at, "end": trip_closed_at},
        ).fetchall()

        by_vehicle: dict[int, dict[str, Any]] = {}
        for vehicle_id, device_id, ts, lon, lat in rows:
            entry = by_vehicle.setdefault(
                vehicle_id, {"device_ids": {}, "pings": [], "ts": []}
            )
            entry["device_ids"][device_id] = entry["device_ids"].get(device_id, 0) + 1
            entry["pings"].append((lon, lat))
            entry["ts"].append(ts)
        return by_vehicle

    def write_sentinel(self, trip: dict[str, Any], conn: psycopg.Connection) -> None:
        """Mark a trip reviewed-and-unusable (no GTFS shape in any feed).

        candidate_vehicle_id=-1 is not a real vehicle_id; it's a sentinel
        _bus_tally/manual_evidence explicitly exclude. Takes an explicit
        conn since qualifying_trips (its only caller) runs from both the
        request-handling thread and the batch-scoring thread.
        """
        conn.execute(
            """
            INSERT INTO scratch.vehicle_identity_labels
                (vehicle_number, line_number, trip_opened_at, trip_closed_at,
                 candidate_vehicle_id, candidate_device_id, n_pings_in_window,
                 best_avg_dist_to_line_m, best_direction, selected, labeled_at)
            VALUES
                (%(vn)s, %(ln)s, %(o)s, %(c)s,
                 -1, NULL, 0, NULL, NULL, false, now())
            ON CONFLICT DO NOTHING
            """,
            {
                "vn": trip["vehicle_number"],
                "ln": trip["line_number"],
                "o": trip["trip_opened_at"],
                "c": trip["trip_closed_at"],
            },
        )

    def start_prefetch(self, vehicle_number: str) -> None:
        """Build this bus's full trip list and kick off background pre-scoring.

        Must be called while holding _lock. qualifying_trips already
        resolved every trip's feed/shape-length in one batched query.
        Ordering has two levels: within a line_number, longest GTFS route
        first (more of the road to judge a candidate against; duration is
        only a tiebreak); across line_numbers, round-robin -- a bus running
        several routes gets a DIFFERENT one every trip for as long as
        possible before ever repeating one, since a candidate that keeps
        checking out across genuinely different routes is far stronger
        disambiguating evidence than several trips on the same route. A
        bus that only ever ran one route in November just serves that
        route's trips in shape-length order, same as before.

        Hands the ordered list to the background worker, which prioritizes
        staying PREFETCH_AHEAD trips ahead of serve_index (so navigating
        trip-to-trip stays fast) and only once that's satisfied continues
        through the REST of the list too, at that same lower priority --
        purely to fill in model_tally, so you can see how consistently the
        model agrees across trips you haven't even reached yet. This is
        safe now in a way it wasn't before the afc_boardings index fix:
        each trip costs ~1-3s instead of ~25s+, so working through a whole
        bus's trip list in the background no longer means firing off
        enough slow queries at once to stall the foreground request.
        """
        self.prefetch_generation += 1
        generation = self.prefetch_generation
        trips = self.qualifying_trips(vehicle_number, self.conn)
        self.trip_list = _order_trips(trips)
        self.serve_index = 0
        self.trip_cache = {}
        self.model_tally = {}
        self.model_tally_trips_scored = 0
        self.model_tally_seen = set()
        threading.Thread(
            target=_prefetch_worker, args=(generation,), daemon=True
        ).start()


store = IdentityStore(DSN)
_lock = threading.Lock()


def _trip_key(trip: dict[str, Any]) -> tuple[str, str, datetime, datetime]:
    return (
        trip["vehicle_number"],
        trip["line_number"],
        trip["trip_opened_at"],
        trip["trip_closed_at"],
    )


def _build_trip_payload(
    trip: dict[str, Any], conn: psycopg.Connection
) -> dict[str, Any]:
    """Score one trip's candidates and package everything the UI needs.

    Assumes trip["resolved_feed_version_date"] is already set (start_prefetch
    resolves it before a trip ever reaches the queue). Runs on whichever
    connection the caller passes -- the background prefetch thread uses its
    own (store.prefetch_conn); a same-thread fallback uses store.conn.
    """
    feed = trip["resolved_feed_version_date"]
    shapes = store.shapes_for(feed, trip["line_number"], conn)
    candidates, top_pick = _rank_candidates(trip, shapes, conn)
    shapes_latlon = store.shapes_latlon_for(feed, trip["line_number"], conn)
    return {
        "vehicle_number": trip["vehicle_number"],
        "line_number": trip["line_number"],
        "trip_opened_at": trip["trip_opened_at"].isoformat(),
        "trip_closed_at": trip["trip_closed_at"].isoformat(),
        "resolved_feed_version_date": feed.isoformat(),
        "shapes": {
            shape_id: [{"lat": lat, "lon": lon} for lat, lon in pts]
            for shape_id, pts in shapes_latlon.items()
        },
        "candidates": candidates,
        "model_top_candidate_vehicle_id": top_pick[0] if top_pick else None,
        "model_top_candidate_probability": round(top_pick[1], 4) if top_pick else None,
        "model_top_candidate_completeness": top_pick[2] if top_pick else None,
        "model_top_candidate_completeness_probability": top_pick[3]
        if top_pick
        else None,
    }


PREFETCH_AHEAD = 3
MODEL_TALLY_MIN_PROB = 0.5


def _record_model_tally(
    key: tuple[str, str, datetime, datetime], payload: dict[str, Any]
) -> None:
    """Count one trip's model top pick toward the per-bus tally.

    Must be called while holding _lock. Idempotent per trip
    (model_tally_seen) since the same trip can reach this from more than
    one path -- the prefetch worker, api_next's synchronous fallback, or
    (rarely) both racing on the same trip. Only counts a "vote" when the
    model's own top pick clears MODEL_TALLY_MIN_PROB -- a trip where even
    the best candidate is unlikely shouldn't silently count as a vote for
    that candidate, same convention as this project's other trust_tier
    thresholds.
    """
    if key in store.model_tally_seen:
        return
    store.model_tally_seen.add(key)
    store.model_tally_trips_scored += 1
    vid = payload.get("model_top_candidate_vehicle_id")
    prob = payload.get("model_top_candidate_probability")
    if vid is not None and prob is not None and prob >= MODEL_TALLY_MIN_PROB:
        store.model_tally[vid] = store.model_tally.get(vid, 0) + 1


def _prefetch_worker(generation: int) -> None:
    """Background thread, two-tier priority.

    First stays PREFETCH_AHEAD trips ahead of serve_index (so trip-to-trip
    navigation stays fast); once that's satisfied, continues through the
    REST of trip_list too, same lower priority, purely so model_tally can
    grow toward covering every trip without you having to review each one
    yourself (see start_prefetch's docstring for why this is safe now).
    Stops the moment the bus changes again (generation bumped) so a stale
    worker never writes results for a bus that's no longer current.
    """
    while True:
        with _lock:
            if generation != store.prefetch_generation:
                return
            window_end = min(store.serve_index + PREFETCH_AHEAD, len(store.trip_list))
            trip = next(
                (
                    store.trip_list[i]
                    for i in range(store.serve_index, window_end)
                    if _trip_key(store.trip_list[i]) not in store.trip_cache
                ),
                None,
            )
            if trip is None:
                trip = next(
                    (
                        t
                        for t in store.trip_list
                        if _trip_key(t) not in store.model_tally_seen
                    ),
                    None,
                )
            if trip is None:
                return
        payload = _build_trip_payload(trip, store.prefetch_conn)
        with _lock:
            if generation != store.prefetch_generation:
                return
            store.trip_cache[_trip_key(trip)] = payload
            _record_model_tally(_trip_key(trip), payload)


def _batch_score_bus(vehicle_number: str, conn: psycopg.Connection) -> None:
    """Score up to MAX_BATCH_TRIPS_PER_BUS trips and write rapid candidates.

    Reuses _build_trip_payload per trip (same model-scoring path as
    interactive review) purely for its model_top_candidate_vehicle_id/
    probability -- not interested in the full map payload here, that gets
    rebuilt on demand (single_candidate_pings) only for whichever
    candidate you're actually reviewing in the rapid flow, so this doesn't
    have to store a giant ping blob per trip for every bus up front.

    Capped, not exhaustive: a bus with hundreds of trips doesn't need all
    of them scored to reveal a dominant winner (observed one bus resolve
    to 379/1200, a clearly dominant ~32% share, that first became obvious
    long before trip 1200) -- scoring capped-at-40, longest-GTFS-route-
    first (_order_trips, same ordering interactive review uses) gets a
    reliable read on most buses in a small fraction of the time a full
    trip history would take, which is what actually gates how fast new
    candidates reach the rapid queue.

    A candidate only earns a row if it was the model's own top pick (with
    probability >= MODEL_TALLY_MIN_PROB) on at least one trip -- same bar
    _record_model_tally uses. Its winning trips are ranked FULL completeness
    first (IDA_FULL/VOLTA_FULL over IDA_PARTIAL/VOLTA_PARTIAL, see
    _completeness_rank) and by probability within the same rank -- a
    PARTIAL trip is a worse thing to review on a map even at a higher raw
    probability, since only part of the route is visible to judge by eye --
    then reordered (not dropped) so distinct routes come first, see
    _diversify_by_route: an AVL vehicle overwhelmingly runs one route all
    day, so every winning trip is kept, just interleaved round-robin across
    line_number, meaning a candidate with lots of evidence still has real
    depth to page through even when most of it is the same route.
    /api/rapid/next and /api/rapid/page render this pool a couple at a
    time rather than all at once, since rendering means real shapes/pings
    DB round trips per trip.
    """
    all_trips = store.qualifying_trips(vehicle_number, conn, exclude_labeled=False)
    trips = _order_trips(all_trips)[:MAX_BATCH_TRIPS_PER_BUS]
    per_candidate: dict[
        int, list[tuple[dict[str, Any], float, str | None, float | None]]
    ] = {}
    for trip in trips:
        payload = _build_trip_payload(trip, conn)
        vid = payload["model_top_candidate_vehicle_id"]
        prob = payload["model_top_candidate_probability"]
        if vid is None or prob is None or prob < MODEL_TALLY_MIN_PROB:
            continue
        per_candidate.setdefault(vid, []).append(
            (
                trip,
                prob,
                payload["model_top_candidate_completeness"],
                payload["model_top_candidate_completeness_probability"],
            )
        )

    now = datetime.now(UTC)
    device_ids = store.device_ids_for(list(per_candidate.keys()))
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM scratch.vehicle_identity_batch_scores "
            "WHERE vehicle_number = %(vn)s",
            {"vn": vehicle_number},
        )
        for vid, wins in per_candidate.items():
            wins.sort(key=lambda w: (_completeness_rank(w[2]), -w[1]))
            diversified = _diversify_by_route(wins)
            instances = [
                {
                    "line_number": t["line_number"],
                    "trip_opened_at": t["trip_opened_at"].isoformat(),
                    "trip_closed_at": t["trip_closed_at"].isoformat(),
                    "resolved_feed_version_date": t[
                        "resolved_feed_version_date"
                    ].isoformat(),
                    "model_valid_probability": round(p, 4),
                    "model_completeness": comp,
                    "model_completeness_probability": comp_p,
                }
                for t, p, comp, comp_p in diversified
            ]
            cur.execute(
                """
                INSERT INTO scratch.vehicle_identity_batch_scores
                    (vehicle_number, candidate_vehicle_id, candidate_device_id,
                     n_trips_scored, n_trips_as_top_pick, avg_top_probability,
                     instances, scored_at)
                VALUES (%(vn)s, %(vid)s, %(did)s, %(nts)s, %(ntp)s, %(avg)s,
                        %(inst)s, %(now)s)
                """,
                {
                    "vn": vehicle_number,
                    "vid": vid,
                    "did": device_ids.get(vid),
                    "nts": len(trips),
                    "ntp": len(wins),
                    "avg": sum(p for _, p, _, _ in wins) / len(wins),
                    "inst": json.dumps(instances),
                    "now": now,
                },
            )
        cur.execute(
            """
            INSERT INTO scratch.vehicle_identity_batch_progress
                (vehicle_number, scored_at)
            VALUES (%(vn)s, %(now)s)
            ON CONFLICT (vehicle_number) DO UPDATE SET scored_at = EXCLUDED.scored_at
            """,
            {"vn": vehicle_number, "now": now},
        )


N_BATCH_WORKERS = 4
_batch_lock = threading.Lock()


def _claim_next_bus(conn: psycopg.Connection) -> str | None:
    """Atomically pick and claim the next bus for this worker thread.

    Holds _batch_lock only for the pick-and-mark-claimed step, not the
    (slow) scoring itself, so N_BATCH_WORKERS threads querying and
    claiming in quick succession never race each other onto the same bus.
    """
    with _batch_lock:
        vehicle_number = store.next_unscored_bus(
            conn, also_exclude=store.batch_in_progress
        )
        if vehicle_number is not None:
            store.batch_in_progress.add(vehicle_number)
        return vehicle_number


def _batch_worker() -> None:
    """Continuously batch-score every not-yet-resolved bus in the background.

    Runs from app startup for as long as the app is up, entirely
    independent of the interactive review session (current_bus/
    prefetch_generation) -- its own connection (never shared with any
    other thread, including other _batch_worker instances), its own
    progress tracking (scratch.vehicle_identity_batch_progress) so it
    survives app restarts and keeps working through the remaining pool
    whether or not anyone is actively reviewing. Stops naturally once
    _claim_next_bus finds nothing left.

    N_BATCH_WORKERS of these run concurrently (see start_batch_workers) to
    multiply throughput -- each trip is already a small, independent unit
    of work (its own avl_pings query + vectorized scoring), so running
    several buses' worth of that work in parallel scales reasonably rather
    than fighting over one shared connection or thread.
    """
    conn = psycopg.connect(DSN, autocommit=True)
    while True:
        vehicle_number = _claim_next_bus(conn)
        if vehicle_number is None:
            return
        try:
            _batch_score_bus(vehicle_number, conn)
        finally:
            with _batch_lock:
                store.batch_in_progress.discard(vehicle_number)


def start_batch_workers() -> None:
    """Launch N_BATCH_WORKERS background batch-scoring threads."""
    for _ in range(N_BATCH_WORKERS):
        threading.Thread(target=_batch_worker, daemon=True).start()


start_batch_workers()


def _speed_feature(
    speed_ref: dict[str, tuple[float, float]],
    shape: ShapeGeom | None,
    duration_sec: float,
) -> tuple[float, float]:
    """(implied_speed_kmh, speed_percentile) for one trip/direction.

    Same fallback find_candidates.py's _lookup_speed uses: a directly
    computed implied speed (shape length / duration) with NaN percentile
    when scratch.trip_shape_samples_scored has no precomputed row for this
    trip (mostly trips resolved through the nearest-feed fallback).
    """
    if shape is None:
        return np.nan, np.nan
    speed, pct = speed_ref.get(shape.shape_id, (np.nan, np.nan))
    if np.isnan(speed) and duration_sec > 0:
        speed = shape.length_m / 1000 / (duration_sec / 3600)
    return speed, pct


def _score_all_candidates(
    trip: dict[str, Any],
    shapes: dict[tuple[Any, str, str], ShapeGeom],
    conn: psycopg.Connection,
) -> tuple[list[dict[str, Any]], list[list[float]]]:
    """Build the display dict and the model's feature row for every candidate.

    Split out of _rank_candidates purely to keep that function's statement
    count down -- see _rank_candidates' own docstring for why every active
    candidate is scored here rather than a geometrically pre-filtered subset.
    """
    raw = store.candidates_for_trip(
        trip["trip_opened_at"], trip["trip_closed_at"], conn
    )
    t_open, t_close = trip["trip_opened_at"], trip["trip_closed_at"]
    excluded_by_time = (store.succ_start < t_close.timestamp()) & (
        store.succ_end > t_open.timestamp()
    )
    excluded_vehicles = set(store.succ_vehicle[excluded_by_time].tolist())
    raw = {
        vid: entry
        for vid, entry in raw.items()
        if vid not in store.claimed_vehicle_ids and vid not in excluded_vehicles
    }
    prior = store.prior_evidence(trip["vehicle_number"], conn)
    feed, line = trip["resolved_feed_version_date"], trip["line_number"]
    shape_i = shapes.get((feed, line, "I"))
    shape_v = shapes.get((feed, line, "V"))

    if not raw:
        return [], []

    vehicle_ids: list[int] = []
    lons: list[float] = []
    lats: list[float] = []
    epochs: list[float] = []
    for vehicle_id, entry in raw.items():
        for (lon, lat), ts in zip(entry["pings"], entry["ts"], strict=True):
            vehicle_ids.append(vehicle_id)
            lons.append(lon)
            lats.append(lat)
            epochs.append(ts.timestamp())

    x, y = project_pings(np.array(lons), np.array(lats), store.transformer)
    agg = aggregate_trip_candidates(
        np.array(vehicle_ids, dtype=np.int64), np.array(epochs), x, y, shape_i, shape_v
    )

    # trip/route-level features: identical for every candidate on this trip,
    # computed once rather than per candidate
    duration_sec = (t_close - t_open).total_seconds()
    local_open, local_close = local_fortaleza(t_open), local_fortaleza(t_close)
    route_feats = store.route_features_for(feed, line, shapes)
    speed_ref = store.speed_ref_for_trip(trip["vehicle_number"], t_open, conn)
    ida_speed, ida_pct = _speed_feature(speed_ref, shape_i, duration_sec)
    volta_speed, volta_pct = _speed_feature(speed_ref, shape_v, duration_sec)

    scored: list[dict[str, Any]] = []
    feature_rows: list[list[float]] = []
    for i, vehicle_id_np in enumerate(agg["vehicle_id"]):
        vehicle_id = int(vehicle_id_np)
        n = int(agg["n_pings_in_window"][i])
        if n < MIN_PINGS_FOR_CANDIDATE and vehicle_id not in prior:
            continue

        ida_dist = float(agg["ida_avg_dist_to_line_m"][i])
        volta_dist = float(agg["volta_avg_dist_to_line_m"][i])
        ida_corr = float(agg["ida_progress_corr"][i])
        ida_start = float(agg["ida_start_proximity_m"][i])
        ida_end = float(agg["ida_end_proximity_m"][i])
        volta_corr = float(agg["volta_progress_corr"][i])
        volta_start = float(agg["volta_start_proximity_m"][i])
        volta_end = float(agg["volta_end_proximity_m"][i])
        dists = [d for d in (ida_dist, volta_dist) if not np.isnan(d)]
        best_dist = min(dists) if dists else None
        best_direction = "I" if dists and best_dist == ida_dist else None
        if dists and best_direction is None:
            best_direction = "V"

        entry = raw[vehicle_id]
        device_id = max(entry["device_ids"], key=lambda k: entry["device_ids"][k])
        scored.append(
            {
                "candidate_vehicle_id": vehicle_id,
                "candidate_device_id": device_id,
                "n_pings_in_window": n,
                "best_avg_dist_to_line_m": _clean(best_dist),
                "best_direction": best_direction,
                "ida": {
                    "avg_dist_to_line_m": _clean(ida_dist),
                    "progress_corr": _clean(ida_corr),
                    "start_proximity_m": _clean(ida_start),
                    "end_proximity_m": _clean(ida_end),
                },
                "volta": {
                    "avg_dist_to_line_m": _clean(volta_dist),
                    "progress_corr": _clean(volta_corr),
                    "start_proximity_m": _clean(volta_start),
                    "end_proximity_m": _clean(volta_end),
                },
                "prior_evidence_trips": prior.get(vehicle_id, 0),
                "pings": [
                    {"t": t.isoformat(), "lon": p[0], "lat": p[1]}
                    for p, t in zip(entry["pings"], entry["ts"], strict=True)
                ],
            }
        )
        feature_rows.append(
            [
                n,
                ida_dist,
                ida_corr,
                ida_start,
                ida_end,
                volta_dist,
                volta_corr,
                volta_start,
                volta_end,
                duration_sec,
                local_open.weekday(),
                local_open.hour,
                local_close.hour,
                route_feats["iv_overlap_m"],
                route_feats["ida_shape_start_end_dist_m"],
                route_feats["volta_shape_start_end_dist_m"],
                ida_speed,
                ida_pct,
                volta_speed,
                volta_pct,
                float(agg["total_distance_m"][i]),
                float(agg["ping_timespan_sec"][i]),
                float(agg["spatial_dispersion_m"][i]),
            ]
        )
    return scored, feature_rows


def _rank_candidates(
    trip: dict[str, Any],
    shapes: dict[tuple[Any, str, str], ShapeGeom],
    conn: psycopg.Connection,
) -> tuple[list[dict[str, Any]], tuple[int, float, str | None, float | None] | None]:
    """Score EVERY AVL vehicle active in the trip window with the model.

    Uses find_candidates.py's aggregate_trip_candidates (numpy/bincount,
    grouped by vehicle_id) instead of calling scoring.compute_direction_metrics
    once per candidate in a Python loop -- a trip window can have up to
    ~1500 distinct active vehicles city-wide. Deliberately doesn't pre-filter
    to a geometrically-plausible handful before scoring: aggregate_trip_
    candidates already computes stats for everyone regardless, so an early
    filter would only silently exclude a candidate that looks better once
    the model weighs distance, correlation, speed, and timing together.

    Two exclusions applied before scoring (see _score_all_candidates), both
    because the model was never shown these as valid options during its own
    training either: store.claimed_vehicle_ids (already confirmed as a
    DIFFERENT bus's real identity this month) and store.succ_* (a confident
    OTHER trip claims this vehicle_id during an overlapping time window,
    find_candidates.py's own exclusion).

    Returns (top MAX_CANDIDATES_SHOWN candidates, overall top pick). The top
    pick is the model's actual #1 choice across every scored candidate,
    captured before prior-evidence forcing/truncation -- feeds the per-bus
    model tally (see _record_model_tally), which needs the real answer even
    for trips where the displayed top-10 got reordered.
    """
    scored, feature_rows = _score_all_candidates(trip, shapes, conn)
    if not scored:
        return [], None

    x_matrix = np.array(feature_rows, dtype=np.float64)
    p_valid = store.validity_model.predict_proba(x_matrix)[:, 1]
    completeness_pred: np.ndarray | None = None
    completeness_prob: np.ndarray | None = None
    if store.completeness_model is not None:
        proba = store.completeness_model.predict_proba(x_matrix)
        classes = store.completeness_model.classes_
        best_i = proba.argmax(axis=1)
        completeness_pred = classes[best_i]
        completeness_prob = proba[np.arange(len(proba)), best_i]

    for j, c in enumerate(scored):
        c["model_valid_probability"] = round(float(p_valid[j]), 4)
        c["model_completeness"] = (
            str(completeness_pred[j]) if completeness_pred is not None else None
        )
        c["model_completeness_probability"] = (
            round(float(completeness_prob[j]), 4)
            if completeness_prob is not None
            else None
        )

    # true overall top pick, captured before prior-evidence forces anything
    # to the front -- this is what feeds the per-bus model tally, since a
    # forced/truncated candidate list could otherwise hide the model's real
    # #1 choice for this trip
    top_j = int(np.argmax(p_valid))
    top_pick = (
        scored[top_j]["candidate_vehicle_id"],
        float(p_valid[top_j]),
        scored[top_j]["model_completeness"],
        scored[top_j]["model_completeness_probability"],
    )

    # prior automatic evidence is always surfaced even if the model ranks it
    # lower; everything else ranks strictly by the model's own probability
    scored.sort(
        key=lambda c: (c["prior_evidence_trips"] == 0, -c["model_valid_probability"])
    )
    forced = [c for c in scored if c["prior_evidence_trips"] > 0]
    rest = [c for c in scored if c["prior_evidence_trips"] == 0]
    return (forced + rest)[:MAX_CANDIDATES_SHOWN], top_pick


def _clean(v: float | None) -> float | None:
    return None if v is None or np.isnan(v) else round(float(v), 2)


def _wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson score interval lower bound for a binomial proportion.

    A raw wins/n fraction overstates confidence at small n (1/1 "agreement"
    looks identical to 100/100) -- this is the standard, purely statistical
    correction (no ML), same idea used for e.g. ranking reviews by a lower
    confidence bound rather than raw average. z=1.96 is the two-sided 95%
    critical value.
    """
    if n == 0:
        return 0.0
    phat = wins / n
    denom = 1 + z**2 / n
    center = phat + z**2 / (2 * n)
    margin = z * math.sqrt(phat * (1 - phat) / n + z**2 / (4 * n**2))
    return max(0.0, (center - margin) / denom)


def _combined_confidence(
    vehicle_number: str, candidate_ids: set[int]
) -> dict[int, dict[str, Any]]:
    """Combine automatic prediction evidence with your manual labels, per bus.

    "Trials" = every confident automatic prediction ever made for this bus
    (across ALL candidates it ever pointed to, not just today's trip) plus
    every trip you've manually reviewed for it. "Wins" for a given candidate
    = automatic predictions that pointed at it, plus manual trips where you
    selected it. This is the exact same agreement ratio
    build_vehicle_identity.py computes from automatic evidence alone,
    generalized to include your labels too.

    total_trials/agreement_pct/confidence_lower_bound are computed over
    EVERY trial, including ones that happened to point at a candidate that
    has since become claimed elsewhere (the 73% dictionary or a
    confirmation) -- that's real evidence that really happened, and
    silently dropping it from the denominator would inflate the remaining
    candidates' numbers. Only the returned candidate SET is filtered to
    currently-unclaimed ones (store.claimed_vehicle_ids), since a claimed
    vehicle_id can't be a live possibility for a different, still-open bus
    anymore -- matching the same exclusion _rank_candidates already applies
    to the live per-trip candidate list.
    """
    automatic = store.prior_evidence(vehicle_number, store.conn)
    automatic_total = sum(automatic.values())
    n_manual_trips, manual_selected = store.manual_evidence(vehicle_number)
    total_trials = automatic_total + n_manual_trips

    out: dict[int, dict[str, Any]] = {}
    for vid in candidate_ids | automatic.keys() | manual_selected.keys():
        if vid in store.claimed_vehicle_ids:
            continue
        wins = automatic.get(vid, 0) + manual_selected.get(vid, 0)
        out[vid] = {
            "automatic_trips": automatic.get(vid, 0),
            "manual_selected_trips": manual_selected.get(vid, 0),
            "total_trials": total_trials,
            "agreement_pct": round(wins / total_trials, 4) if total_trials else None,
            "confidence_lower_bound": round(_wilson_lower_bound(wins, total_trials), 4)
            if total_trials
            else None,
        }
    return out


TALLY_TOP_N = 5
# Same bar build_vehicle_identity.py's own STRONG_MIN_AGREEMENT/
# STRONG_MIN_TRIPS uses to promote a bus into the automatic dictionary --
# reused here so "resolved" means the exact same thing in both places.
RESOLVED_MIN_AGREEMENT = 0.9
RESOLVED_MIN_TRIALS = 3


def _bus_tally(vehicle_number: str, candidate_ids: set[int]) -> dict[str, Any]:
    """Summarize this bus's top few candidates plus an overall status.

    Status is "resolved" once the top candidate clears the same
    90%-agreement/>=3-trials bar the automatic dictionary uses, "ambiguous"
    otherwise (whether that's because nothing has enough trials yet, or
    because trials are split and nothing dominates) -- either way, the
    answer to "am I done with this bus yet?".

    A bus can accumulate automatic evidence for dozens of distinct
    candidates over a month (e.g. a vehicle_number that turns out to be a
    shared/generic AFC code rather than one physical bus) -- capped to the
    top TALLY_TOP_N so this stays a glanceable summary, not a dump.
    """
    n_manual_trips, manual_selected = store.manual_evidence(vehicle_number)
    confidence = _combined_confidence(vehicle_number, candidate_ids)
    ranked = sorted(
        (
            (vid, stats)
            for vid, stats in confidence.items()
            if stats["automatic_trips"] or stats["manual_selected_trips"]
        ),
        key=lambda kv: kv[1]["confidence_lower_bound"] or 0,
        reverse=True,
    )
    top = ranked[0] if ranked else None
    resolved = (
        top is not None
        and top[1]["total_trials"] >= RESOLVED_MIN_TRIALS
        and (top[1]["agreement_pct"] or 0) >= RESOLVED_MIN_AGREEMENT
    )
    top_n = ranked[:TALLY_TOP_N]
    device_ids = store.device_ids_for([vid for vid, _ in top_n])
    return {
        "n_trips_labeled": n_manual_trips,
        "n_candidates_with_evidence": len(ranked),
        "status": "resolved" if resolved else "ambiguous",
        "resolved_candidate_vehicle_id": top[0] if resolved and top else None,
        "candidates": [
            {
                "candidate_vehicle_id": vid,
                "candidate_device_id": device_ids.get(vid),
                "manual_selected_trips": manual_selected.get(vid, 0),
                **stats,
            }
            for vid, stats in top_n
        ],
    }


@app.get("/")
def index() -> FileResponse:
    """Serve the labeling UI."""
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/rapid")
def rapid_index() -> FileResponse:
    """Serve the rapid confirm/deny review UI."""
    return FileResponse(Path(__file__).parent / "static" / "rapid.html")


def _rapid_progress() -> dict[str, int]:
    """Overall "how many buses left" snapshot for the rapid-review header."""
    row = store.rapid_conn.execute(
        """
        WITH all_buses AS (
            SELECT DISTINCT vehicle_number FROM silver.afc_boardings
            WHERE trip_opened_at >= '2023-11-01' AND trip_opened_at < '2023-12-01'
        ),
        resolved AS (
            SELECT vehicle_number FROM scratch.november_2023_vehicle_identity
            UNION
            SELECT vehicle_number FROM scratch.vehicle_identity_confirmed
        )
        SELECT
            (SELECT count(*) FROM all_buses),
            (SELECT count(*) FROM resolved),
            (SELECT count(*) FROM scratch.vehicle_identity_batch_progress)
        """
    ).fetchone()
    if row is None:
        return {
            "total_buses": 0,
            "resolved_buses": 0,
            "remaining_buses": 0,
            "batch_scored_buses": 0,
        }
    total, resolved, batch_scored = row
    return {
        "total_buses": total,
        "resolved_buses": resolved,
        "remaining_buses": total - resolved,
        "batch_scored_buses": batch_scored,
    }


def _render_instances(
    instances: list[dict[str, Any]], vid: int
) -> list[dict[str, Any]]:
    """Render shapes+pings maps for a slice of a candidate's stored instances.

    Shared by /api/rapid/next (first page) and /api/rapid/page (every page
    after) so both pay the same per-trip DB round trips (shapes_latlon_for,
    single_candidate_pings) the same way, just for different slices of the
    stored pool.
    """
    rendered = []
    for inst in instances:
        feed = date.fromisoformat(inst["resolved_feed_version_date"])
        line = inst["line_number"]
        shapes = store.shapes_latlon_for(feed, line, store.rapid_conn)
        opened = datetime.fromisoformat(inst["trip_opened_at"])
        closed = datetime.fromisoformat(inst["trip_closed_at"])
        pings = store.single_candidate_pings(vid, opened, closed, store.rapid_conn)
        rendered.append(
            {
                "line_number": line,
                "trip_opened_at": inst["trip_opened_at"],
                "trip_closed_at": inst["trip_closed_at"],
                "model_valid_probability": inst["model_valid_probability"],
                "model_completeness": inst.get("model_completeness"),
                "model_completeness_probability": inst.get(
                    "model_completeness_probability"
                ),
                "shapes": {
                    shape_id: [{"lat": lat, "lon": lon} for lat, lon in pts]
                    for shape_id, pts in shapes.items()
                },
                "pings": pings,
            }
        )
    return rendered


@app.get("/api/rapid/next")
def api_rapid_next() -> JSONResponse:
    """Return the most-confident (bus, candidate) pair still open, unskipped.

    Ranked FULL-completeness-first: a candidate whose single best stored
    trip is IDA_FULL/VOLTA_FULL (instances is stored sorted that way, see
    _batch_score_bus) is shown before one whose best trip is only PARTIAL,
    since a PARTIAL trip is a weaker, less legible thing to judge on a map
    even at a higher raw probability. Within that, ranked by the Wilson
    score interval lower bound (95% CI, z=1.96 -- same formula as
    _wilson_lower_bound, reimplemented in SQL here since it drives the
    ORDER BY) of n_trips_as_top_pick out of n_trips_scored: a raw win share
    overstates confidence at small n (2/2 looks identical to 30/40 by raw
    percentage alone), so this is what actually keeps a bus with barely any
    scored trips from outranking one with a large, mostly-agreeing sample --
    "more data AND a higher win percentage," not either alone. Ties within
    that are broken by the GAP between this bus's best remaining candidate
    and its best remaining runner-up (as a share of trips scored), not just
    the winner's own share in isolation -- a candidate winning 8/10 trips
    with a runner-up at 1/10 is far stronger, cleaner evidence than one
    winning 8/20 against a runner-up at 7/20, even at a similar Wilson
    bound. Already-rejected/claimed candidates are excluded before
    computing who the "runner-up" even is, so a rejected former #2 doesn't
    count against the gap.

    Fetches the top 50 by that ranking (not just 1) and returns the first
    one not in store.rapid_skipped -- see /api/rapid/skip. Filtering happens
    in Python rather than SQL since skipped is small, in-memory, per-session
    state, not worth a NOT IN(...) over an array of composite pairs.

    Only renders (shapes+pings) the first INSTANCES_PER_PAGE of the winning
    candidate's stored instances -- /api/rapid/page renders more on demand,
    since a candidate can have far more winning trips than are worth paying
    the render cost for up front.

    Works with whatever _batch_worker has scored so far -- the queue simply
    grows as more buses get batch-scored in the background, and this
    always just picks the best of what's currently available rather than
    waiting for the whole pool to finish.
    """
    rows = store.rapid_conn.execute(
        """
        WITH eligible AS (
            SELECT b.*,
                   b.n_trips_as_top_pick::float
                       / NULLIF(b.n_trips_scored, 0) AS phat
            FROM scratch.vehicle_identity_batch_scores b
            WHERE b.vehicle_number NOT IN (
                SELECT vehicle_number FROM scratch.november_2023_vehicle_identity
            )
            AND b.vehicle_number NOT IN (
                SELECT vehicle_number FROM scratch.vehicle_identity_confirmed
            )
            AND NOT EXISTS (
                SELECT 1 FROM scratch.vehicle_identity_batch_rejected r
                WHERE r.vehicle_number = b.vehicle_number
                  AND r.candidate_vehicle_id = b.candidate_vehicle_id
            )
            AND b.candidate_vehicle_id NOT IN (
                SELECT avl_vehicle_id FROM scratch.november_2023_vehicle_identity
                WHERE avl_vehicle_id IS NOT NULL
            )
            AND b.candidate_vehicle_id NOT IN (
                SELECT avl_vehicle_id FROM scratch.vehicle_identity_confirmed
                WHERE avl_vehicle_id IS NOT NULL
            )
        ),
        ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY vehicle_number ORDER BY n_trips_as_top_pick DESC
                   ) AS rn,
                   LEAD(n_trips_as_top_pick, 1, 0) OVER (
                       PARTITION BY vehicle_number ORDER BY n_trips_as_top_pick DESC
                   ) AS runner_up_top_picks,
                   -- Wilson score interval lower bound, 95% CI (z=1.96),
                   -- same formula as _wilson_lower_bound() elsewhere in
                   -- this app: corrects raw win share for sample size so
                   -- e.g. 2/2 scored trips can't outrank 30/40.
                   CASE WHEN n_trips_scored > 0 THEN
                       GREATEST(0, (
                           (phat + 1.96 ^ 2 / (2 * n_trips_scored)
                               - 1.96 * sqrt(
                                   phat * (1 - phat) / n_trips_scored
                                   + 1.96 ^ 2 / (4 * n_trips_scored ^ 2)
                               ))
                           / (1 + 1.96 ^ 2 / n_trips_scored)
                       ))
                   ELSE 0 END AS wilson_lower
            FROM eligible
        )
        SELECT vehicle_number, candidate_vehicle_id, candidate_device_id,
               n_trips_scored, n_trips_as_top_pick, avg_top_probability,
               instances, runner_up_top_picks
        FROM ranked
        WHERE rn = 1
        ORDER BY
            (instances -> 0 ->> 'model_completeness')
                IN ('IDA_FULL', 'VOLTA_FULL') DESC,
            wilson_lower DESC,
            (n_trips_as_top_pick - runner_up_top_picks)::float
                / NULLIF(n_trips_scored, 0) DESC,
            n_trips_as_top_pick DESC
        LIMIT 50
        """
    ).fetchall()

    progress = _rapid_progress()
    row = next((r for r in rows if (r[0], r[1]) not in store.rapid_skipped), None)
    if row is None:
        return JSONResponse({"done": True, "progress": progress})

    (
        vehicle_number,
        vid,
        device_id,
        n_scored,
        n_top,
        avg_prob,
        instances,
        runner_up_top_picks,
    ) = row
    rendered_instances = _render_instances(instances[:INSTANCES_PER_PAGE], vid)

    return JSONResponse(
        {
            "done": False,
            "vehicle_number": vehicle_number,
            "candidate_vehicle_id": vid,
            "candidate_device_id": device_id,
            "n_trips_scored": n_scored,
            "n_trips_as_top_pick": n_top,
            "runner_up_top_picks": runner_up_top_picks,
            "avg_top_probability": round(avg_prob, 4) if avg_prob is not None else None,
            "instances": rendered_instances,
            "total_instances": len(instances),
            "progress": progress,
        }
    )


@app.get("/api/rapid/page")
def api_rapid_page(
    vehicle_number: str, candidate_vehicle_id: int, offset: int
) -> JSONResponse:
    """Render the next INSTANCES_PER_PAGE stored instances for one candidate.

    Paging companion to /api/rapid/next's first page -- offset is how many
    instances the caller has already seen (rapid.html tracks this as
    shownCount). Looks the row back up by (vehicle_number,
    candidate_vehicle_id) rather than trusting a client-held copy of
    instances, since the underlying row could in principle have changed.
    """
    row = store.rapid_conn.execute(
        """
        SELECT instances FROM scratch.vehicle_identity_batch_scores
        WHERE vehicle_number = %(vn)s AND candidate_vehicle_id = %(vid)s
        """,
        {"vn": vehicle_number, "vid": candidate_vehicle_id},
    ).fetchone()
    if row is None:
        return JSONResponse({"instances": [], "total_instances": 0})
    instances = row[0]
    page = instances[offset : offset + INSTANCES_PER_PAGE]
    return JSONResponse(
        {
            "instances": _render_instances(page, candidate_vehicle_id),
            "total_instances": len(instances),
        }
    )


class RapidDenyIn(BaseModel):
    """A "no, this candidate is wrong" decision in the rapid review flow."""

    vehicle_number: str
    candidate_vehicle_id: int


@app.post("/api/rapid/deny")
def api_rapid_deny(payload: RapidDenyIn) -> dict[str, Any]:
    """Reject one (bus, candidate) pair.

    /api/rapid/next never offers it again -- the next call naturally falls
    through to that bus's next-best candidate (if
    scratch.vehicle_identity_batch_scores has one) or a different bus
    entirely. "Yes" doesn't need an equivalent endpoint here -- the rapid
    UI just calls the existing /api/confirm directly.
    """
    store.rapid_conn.execute(
        """
        INSERT INTO scratch.vehicle_identity_batch_rejected
            (vehicle_number, candidate_vehicle_id, rejected_at)
        VALUES (%(vn)s, %(vid)s, %(now)s)
        ON CONFLICT (vehicle_number, candidate_vehicle_id) DO NOTHING
        """,
        {
            "vn": payload.vehicle_number,
            "vid": payload.candidate_vehicle_id,
            "now": datetime.now(UTC),
        },
    )
    return {"ok": True}


class RapidSkipIn(BaseModel):
    """An "I'm not sure" decision in the rapid review flow."""

    vehicle_number: str
    candidate_vehicle_id: int


@app.post("/api/rapid/skip")
def api_rapid_skip(payload: RapidSkipIn) -> dict[str, Any]:
    """Set aside one (bus, candidate) pair for now, without judging it.

    Unlike deny (permanent, scratch.vehicle_identity_batch_rejected), this
    is "ambiguous, come back to it later" -- yes/no mean "definitely right"
    /"definitely wrong"; skip is for anything in between. Recorded only in
    store.rapid_skipped, an in-memory set that resets on app restart, so
    /api/rapid/next simply offers it again on a later pass through the
    queue instead of excluding it forever.
    """
    store.rapid_skipped.add((payload.vehicle_number, payload.candidate_vehicle_id))
    return {"ok": True}


@app.get("/api/next")
def api_next(*, advance_bus: bool = False) -> JSONResponse:
    """Return the next (bus, trip) to review.

    Served from the prefetch cache when it's ready (the common case);
    falls back to scoring inline, right here, when you outrun the
    background worker. Auto-advances the bus queue whenever the current
    bus runs out of qualifying trips, or when the caller explicitly asks
    to move on (advance_bus=true) -- either way, kicks off prefetching for
    whichever bus comes next before returning.
    """
    with _lock:
        if advance_bus and store.current_bus is not None:
            store.skipped_this_session.add(store.current_bus)
            store.current_bus = None

        while True:
            if store.current_bus is None:
                store.current_bus = store.next_bus()
                if store.current_bus is None:
                    raise HTTPException(404, "no buses left to review")
                store.start_prefetch(store.current_bus)

            if store.serve_index < len(store.trip_list):
                break
            store.skipped_this_session.add(store.current_bus)
            store.current_bus = None

        trip = store.trip_list[store.serve_index]
        store.serve_index += 1
        vehicle_number = trip["vehicle_number"]
        cached = store.trip_cache.pop(_trip_key(trip), None)

    if cached is not None:
        payload = cached
    else:
        payload = _build_trip_payload(trip, store.conn)
        with _lock:
            _record_model_tally(_trip_key(trip), payload)

    candidate_ids = {c["candidate_vehicle_id"] for c in payload["candidates"]}
    confidence = _combined_confidence(vehicle_number, candidate_ids)
    for c in payload["candidates"]:
        c["confidence"] = confidence[c["candidate_vehicle_id"]]

    with _lock:
        model_tally_snapshot = {
            "n_trips_scored": store.model_tally_trips_scored,
            "n_trips_total": len(store.trip_list),
            "candidates": sorted(
                store.model_tally.items(), key=lambda kv: kv[1], reverse=True
            )[:TALLY_TOP_N],
        }

    return JSONResponse(
        {
            **payload,
            "bus_tally": _bus_tally(vehicle_number, candidate_ids),
            "model_tally": model_tally_snapshot,
        }
    )


@app.post("/api/label")
def api_label(payload: LabelIn) -> dict[str, Any]:
    """Record a labeling decision for every candidate shown for this trip."""
    now = datetime.now(UTC)
    with store.conn.cursor() as cur:
        for c in payload.candidates:
            cur.execute(
                """
                INSERT INTO scratch.vehicle_identity_labels
                    (vehicle_number, line_number, trip_opened_at, trip_closed_at,
                     candidate_vehicle_id, candidate_device_id, n_pings_in_window,
                     best_avg_dist_to_line_m, best_direction, selected, labeled_at)
                VALUES (%(vn)s, %(ln)s, %(o)s, %(c)s, %(cvid)s, %(cdid)s, %(n)s,
                        %(dist)s, %(dir)s, %(sel)s, %(now)s)
                ON CONFLICT (
                    vehicle_number, trip_opened_at, trip_closed_at, candidate_vehicle_id
                ) DO UPDATE SET selected = EXCLUDED.selected
                """,
                {
                    "vn": payload.vehicle_number,
                    "ln": payload.line_number,
                    "o": payload.trip_opened_at,
                    "c": payload.trip_closed_at,
                    "cvid": c.candidate_vehicle_id,
                    "cdid": c.candidate_device_id,
                    "n": c.n_pings_in_window,
                    "dist": c.best_avg_dist_to_line_m,
                    "dir": c.best_direction,
                    "sel": c.selected,
                    "now": now,
                },
            )
    return {"ok": True}


class ConfirmIn(BaseModel):
    """A user's explicit sign-off on one bus's real vehicle identity."""

    vehicle_number: str
    avl_vehicle_id: int
    device_id: str | None
    agreement_pct: float | None
    confidence_lower_bound: float | None
    total_trials: int | None


@app.post("/api/confirm")
def api_confirm(payload: ConfirmIn) -> dict[str, Any]:
    """Durably record "yes, I'm sure" for this bus and move on.

    Writes to scratch.vehicle_identity_confirmed, not
    scratch.november_2023_vehicle_identity directly -- that table gets
    DROP+CREATE rebuilt from scratch every time build_vehicle_identity.py
    runs, so a row written straight into it here would just be gone the
    next time someone reruns that script. build_vehicle_identity.py was
    updated to fold this table in as a third evidence source on rebuild,
    so a confirmation here does eventually land in the canonical dictionary.

    Clears current_bus so the next /api/next call (even without
    advance_bus) picks a fresh one, same effect as clicking "next bus".
    """
    store.conn.execute(
        """
        INSERT INTO scratch.vehicle_identity_confirmed
            (vehicle_number, avl_vehicle_id, device_id, agreement_pct,
             confidence_lower_bound, total_trials, confirmed_at)
        VALUES (%(vn)s, %(vid)s, %(did)s, %(agr)s, %(conf)s, %(trials)s, %(now)s)
        ON CONFLICT (vehicle_number) DO UPDATE SET
            avl_vehicle_id = EXCLUDED.avl_vehicle_id,
            device_id = EXCLUDED.device_id,
            agreement_pct = EXCLUDED.agreement_pct,
            confidence_lower_bound = EXCLUDED.confidence_lower_bound,
            total_trials = EXCLUDED.total_trials,
            confirmed_at = EXCLUDED.confirmed_at
        """,
        {
            "vn": payload.vehicle_number,
            "vid": payload.avl_vehicle_id,
            "did": payload.device_id,
            "agr": payload.agreement_pct,
            "conf": payload.confidence_lower_bound,
            "trials": payload.total_trials,
            "now": datetime.now(UTC),
        },
    )
    with _lock:
        store.claimed_vehicle_ids.add(payload.avl_vehicle_id)
        if store.current_bus == payload.vehicle_number:
            store.skipped_this_session.add(payload.vehicle_number)
            store.current_bus = None
    return {"ok": True}


if __name__ == "__main__":
    # tailnet-only dev box, matches trip_finder/trip_labeler's own exposure pattern
    uvicorn.run(app, host="0.0.0.0", port=8012)  # noqa: S104
