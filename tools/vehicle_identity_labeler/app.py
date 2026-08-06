"""Manual, statistics-only labeler for buses build_vehicle_identity.py couldn't resolve.

No machine learning anywhere in this app -- every ranking number shown is
computed the same way tools/trip_finder/scoring.py already does for its own
spot-check (distance-to-line, start/end proximity, progress correlation),
reused directly here rather than reimplemented. The only thing this app adds
is a human in the loop: for one bus number at a time, it shows one of that
bus's real November trips (>=15 minutes, so there's enough GPS signal to
judge from) plotted against its GTFS route, alongside every AVL vehicle_id
active during that exact trip window, ranked by how well each one's own GPS
track follows the route. You look at the map and decide which candidate (if
any) is plausibly the real bus.

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
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

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
from trip_finder.find_candidates import aggregate_trip_candidates
from trip_finder.scoring import UTM_24S, ShapeGeom, load_shapes, project_pings

DSN = "postgresql://opa:opa@localhost:5432/opa"
MIN_TRIP_MINUTES = 15
MIN_PINGS_FOR_CANDIDATE = 3
MAX_CANDIDATES_SHOWN = 10

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
        self.transformer = pyproj.Transformer.from_crs(
            "EPSG:4326", f"EPSG:{UTM_24S}", always_xy=True
        )
        self.shape_cache: dict[
            tuple[date, str], dict[tuple[date, str, str], ShapeGeom]
        ] = {}
        self.current_bus: str | None = None
        self.skipped_this_session: set[str] = set()
        self.trip_list: list[dict[str, Any]] = []
        self.serve_index = 0
        self.trip_cache: dict[tuple[str, str, datetime, datetime], dict[str, Any]] = {}
        self.prefetch_generation = 0
        self._ensure_schema()
        self.claimed_vehicle_ids = self._load_claimed_vehicle_ids()

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

    def qualifying_trips(self, vehicle_number: str) -> list[dict[str, Any]]:
        """Return this bus's not-yet-labeled >=15min November trips.

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
        and sentinel-written immediately, so start_prefetch and the
        background worker never have to special-case them.
        """
        trip_rows = self.conn.execute(
            """
            SELECT DISTINCT vehicle_number, line_number, trip_opened_at, trip_closed_at
            FROM silver.afc_boardings b
            WHERE vehicle_number = %(vn)s
              AND trip_opened_at >= '2023-11-01' AND trip_opened_at < '2023-12-01'
              AND trip_closed_at - trip_opened_at >= interval '%(min)s minutes'
              AND NOT EXISTS (
                  SELECT 1 FROM scratch.vehicle_identity_labels l
                  WHERE l.vehicle_number = b.vehicle_number
                    AND l.trip_opened_at = b.trip_opened_at
                    AND l.trip_closed_at = b.trip_closed_at
              )
            """,
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
        feed_rows = self.conn.execute(
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
            length_rows = self.conn.execute(
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
                self.write_sentinel(trip)
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
        """Map avl_vehicle_id -> its most-common device_id, city-wide.

        Not scoped to any one trip -- used for the bus-level tally, which
        can rank a candidate the current trip's own window never even saw
        pings for. vehicle_id<->device_id pairing is highly stable (only
        9/1484 November vehicle_ids ever showed >1 device_id), so "most
        common overall" is a safe stand-in for "the one right now".
        """
        if not avl_vehicle_ids:
            return {}
        rows = self.conn.execute(
            """
            SELECT DISTINCT ON (vehicle_id) vehicle_id, device_id
            FROM silver.avl_pings
            WHERE vehicle_id = ANY(%(vids)s)
            GROUP BY vehicle_id, device_id
            ORDER BY vehicle_id, count(*) DESC
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

    def write_sentinel(self, trip: dict[str, Any]) -> None:
        """Mark a trip reviewed-and-unusable (no GTFS shape in any feed).

        candidate_vehicle_id=-1 is not a real vehicle_id; it's a sentinel
        _bus_tally/manual_evidence explicitly exclude.
        """
        self.conn.execute(
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
        resolved every trip's feed/shape-length in one batched query, so
        this just orders the list (longest GTFS route first -- more of the
        road to judge a candidate against; duration is only a tiebreak now)
        and hands it to the background worker, which stays PREFETCH_AHEAD
        trips ahead of serve_index rather than racing through the whole
        list at once -- a bus can have hundreds of qualifying trips, and
        letting the worker fire off that many heavy avl_pings scans back to
        back saturates Postgres badly enough to stall the very request
        that's waiting on the current trip.
        """
        self.prefetch_generation += 1
        generation = self.prefetch_generation
        trips = self.qualifying_trips(vehicle_number)
        trips.sort(
            key=lambda t: (
                t["_shape_length_m"],
                t["trip_closed_at"] - t["trip_opened_at"],
            ),
            reverse=True,
        )
        self.trip_list = trips
        self.serve_index = 0
        self.trip_cache = {}
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
    candidates = _rank_candidates(trip, shapes, conn)
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
    }


PREFETCH_AHEAD = 3


def _prefetch_worker(generation: int) -> None:
    """Background thread: stay PREFETCH_AHEAD trips ahead of serve_index.

    Deliberately bounded, not "score the whole bus as fast as possible" --
    see start_prefetch's docstring for why. Stops the moment the bus
    changes again (generation bumped) so a stale worker never writes
    results for a bus that's no longer current; idles (short sleep) once
    it's caught up, waking back up as serve_index advances.
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
            done = store.serve_index >= len(store.trip_list)
        if done:
            return
        if trip is None:
            time.sleep(0.2)
            continue
        payload = _build_trip_payload(trip, store.prefetch_conn)
        with _lock:
            if generation != store.prefetch_generation:
                return
            store.trip_cache[_trip_key(trip)] = payload


def _rank_candidates(
    trip: dict[str, Any],
    shapes: dict[tuple[Any, str, str], ShapeGeom],
    conn: psycopg.Connection,
) -> list[dict[str, Any]]:
    """Score every AVL vehicle active in the trip window and rank them.

    Uses find_candidates.py's aggregate_trip_candidates (numpy/bincount,
    grouped by vehicle_id) instead of calling scoring.compute_direction_metrics
    once per candidate in a Python loop -- a trip window can have up to
    ~1500 distinct active vehicles city-wide, and the per-candidate scalar
    path was the actual bottleneck this function used to have.

    Vehicles already claimed by a different, already-resolved bus
    (store.claimed_vehicle_ids) are dropped before scoring -- they can't
    also be this bus's real vehicle.
    """
    raw = store.candidates_for_trip(
        trip["trip_opened_at"], trip["trip_closed_at"], conn
    )
    raw = {
        vid: entry for vid, entry in raw.items() if vid not in store.claimed_vehicle_ids
    }
    prior = store.prior_evidence(trip["vehicle_number"], conn)
    shape_i = shapes.get((trip["resolved_feed_version_date"], trip["line_number"], "I"))
    shape_v = shapes.get((trip["resolved_feed_version_date"], trip["line_number"], "V"))

    if not raw:
        return []

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

    scored: list[dict[str, Any]] = []
    for i, vehicle_id_np in enumerate(agg["vehicle_id"]):
        vehicle_id = int(vehicle_id_np)
        n = int(agg["n_pings_in_window"][i])
        if n < MIN_PINGS_FOR_CANDIDATE and vehicle_id not in prior:
            continue

        ida_dist = float(agg["ida_avg_dist_to_line_m"][i])
        volta_dist = float(agg["volta_avg_dist_to_line_m"][i])
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
                    "progress_corr": _clean(float(agg["ida_progress_corr"][i])),
                    "start_proximity_m": _clean(float(agg["ida_start_proximity_m"][i])),
                    "end_proximity_m": _clean(float(agg["ida_end_proximity_m"][i])),
                },
                "volta": {
                    "avg_dist_to_line_m": _clean(volta_dist),
                    "progress_corr": _clean(float(agg["volta_progress_corr"][i])),
                    "start_proximity_m": _clean(
                        float(agg["volta_start_proximity_m"][i])
                    ),
                    "end_proximity_m": _clean(float(agg["volta_end_proximity_m"][i])),
                },
                "prior_evidence_trips": prior.get(vehicle_id, 0),
                "pings": [
                    {"t": t.isoformat(), "lon": p[0], "lat": p[1]}
                    for p, t in zip(entry["pings"], entry["ts"], strict=True)
                ],
            }
        )

    # rank by geometric closeness, but always surface anything with prior
    # automatic evidence even if its live geometry ranks it lower
    scored.sort(
        key=lambda c: (
            c["prior_evidence_trips"] == 0,
            c["best_avg_dist_to_line_m"]
            if c["best_avg_dist_to_line_m"] is not None
            else float("inf"),
        )
    )
    forced = [c for c in scored if c["prior_evidence_trips"] > 0]
    rest = [c for c in scored if c["prior_evidence_trips"] == 0]
    return (forced + rest)[:MAX_CANDIDATES_SHOWN]


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
    """
    automatic = store.prior_evidence(vehicle_number, store.conn)
    automatic_total = sum(automatic.values())
    n_manual_trips, manual_selected = store.manual_evidence(vehicle_number)
    total_trials = automatic_total + n_manual_trips

    out: dict[int, dict[str, Any]] = {}
    for vid in candidate_ids | automatic.keys() | manual_selected.keys():
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

    payload = cached if cached is not None else _build_trip_payload(trip, store.conn)

    candidate_ids = {c["candidate_vehicle_id"] for c in payload["candidates"]}
    confidence = _combined_confidence(vehicle_number, candidate_ids)
    for c in payload["candidates"]:
        c["confidence"] = confidence[c["candidate_vehicle_id"]]

    return JSONResponse(
        {**payload, "bus_tally": _bus_tally(vehicle_number, candidate_ids)}
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
