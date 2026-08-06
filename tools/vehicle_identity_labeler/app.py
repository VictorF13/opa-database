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
never shown. Among the rest, buses with more prior automatic evidence
(confident-but-not-quite-90%-agreement trips from trip_labeler/trip_finder)
come first -- they're the ones statistically closest to being resolved, so
your labels should go furthest fastest there. Buses with zero prior evidence
sort last. Move on explicitly with the "next bus" button whenever you're
satisfied (or stuck) -- there's no automatic promotion out of this app; that
happens later, by re-aggregating scratch.vehicle_identity_labels the same
way build_vehicle_identity.py aggregates the automatic sources.

Writes only to a BRAND NEW table, scratch.vehicle_identity_labels. Never
touches trip_match_predictions, trip_finder_predictions,
november_2023_vehicle_identity, or any other existing table.

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
from typing import Any

import numpy as np
import psycopg
import pyproj
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))
from trip_finder.scoring import (
    UTM_24S,
    ShapeGeom,
    compute_direction_metrics,
    load_shapes,
    project_pings,
)

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

    Tracks which bus is currently being reviewed, and which buses have been
    explicitly skipped/exhausted this session so "next bus" doesn't loop
    back to them immediately.
    """

    def __init__(self, dsn: str) -> None:
        """Connect to Postgres and set up the WGS84->UTM24S ping projector."""
        self.conn = psycopg.connect(dsn, autocommit=True)
        self.transformer = pyproj.Transformer.from_crs(
            "EPSG:4326", f"EPSG:{UTM_24S}", always_xy=True
        )
        self.shape_cache: dict[
            tuple[date, str], dict[tuple[date, str, str], ShapeGeom]
        ] = {}
        self.current_bus: str | None = None
        self.skipped_this_session: set[str] = set()
        self._ensure_schema()

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

    def next_bus(self) -> str | None:
        """Pick the not-yet-resolved bus with the most prior automatic evidence.

        "Prior automatic evidence" mirrors build_vehicle_identity.py's own
        vehicle_evidence/device_evidence CTEs: confident (high_confidence_valid)
        trips from trip_labeler and trip_finder, pooled per bus number,
        regardless of whether they agreed enough to already be "strong".
        """
        row = self.conn.execute(
            """
            WITH vehicle_evidence AS (
                SELECT vehicle_number, count(*) AS n
                FROM scratch.trip_match_predictions
                WHERE source = 'vehicle' AND trust_tier = 'high_confidence_valid'
                GROUP BY vehicle_number
            ),
            trip_finder_evidence AS (
                SELECT vehicle_number, count(*) AS n
                FROM scratch.trip_finder_predictions
                WHERE trust_tier = 'high_confidence_valid'
                  AND predicted_candidate_vehicle_id IS NOT NULL
                GROUP BY vehicle_number
            ),
            device_evidence AS (
                SELECT vehicle_number, count(*) AS n
                FROM scratch.trip_match_predictions
                WHERE source = 'device' AND trust_tier = 'high_confidence_valid'
                GROUP BY vehicle_number
            ),
            manual_evidence AS (
                SELECT vehicle_number, count(*) AS n
                FROM scratch.vehicle_identity_labels
                GROUP BY vehicle_number
            ),
            all_buses AS (
                SELECT DISTINCT vehicle_number FROM silver.afc_boardings
                WHERE trip_opened_at >= '2023-11-01' AND trip_opened_at < '2023-12-01'
            )
            SELECT a.vehicle_number,
                   COALESCE(v.n, 0) + COALESCE(t.n, 0) + COALESCE(d.n, 0)
                       + COALESCE(m.n, 0) AS score
            FROM all_buses a
            LEFT JOIN vehicle_evidence v USING (vehicle_number)
            LEFT JOIN trip_finder_evidence t USING (vehicle_number)
            LEFT JOIN device_evidence d USING (vehicle_number)
            LEFT JOIN manual_evidence m USING (vehicle_number)
            WHERE a.vehicle_number NOT IN (
                SELECT vehicle_number FROM scratch.november_2023_vehicle_identity
            )
            AND a.vehicle_number != ALL(%(skipped)s)
            ORDER BY score DESC, a.vehicle_number
            LIMIT 1
            """,
            {"skipped": list(self.skipped_this_session)},
        ).fetchone()
        return row[0] if row else None

    def next_trip(self, vehicle_number: str) -> dict[str, Any] | None:
        """Return the longest not-yet-labeled >=15min November trip for this bus."""
        row = self.conn.execute(
            """
            SELECT b.vehicle_number, b.line_number, b.trip_opened_at, b.trip_closed_at
            FROM (
                SELECT DISTINCT
                    vehicle_number, line_number, trip_opened_at, trip_closed_at
                FROM silver.afc_boardings
                WHERE vehicle_number = %(vn)s
                  AND trip_opened_at >= '2023-11-01' AND trip_opened_at < '2023-12-01'
                  AND trip_closed_at - trip_opened_at >= interval '%(min)s minutes'
            ) b
            WHERE NOT EXISTS (
                SELECT 1 FROM scratch.vehicle_identity_labels l
                WHERE l.vehicle_number = b.vehicle_number
                  AND l.trip_opened_at = b.trip_opened_at
                  AND l.trip_closed_at = b.trip_closed_at
            )
            ORDER BY b.trip_closed_at - b.trip_opened_at DESC
            LIMIT 1
            """,
            {"vn": vehicle_number, "min": MIN_TRIP_MINUTES},
        ).fetchone()
        if row is None:
            return None
        return {
            "vehicle_number": row[0],
            "line_number": row[1],
            "trip_opened_at": row[2],
            "trip_closed_at": row[3],
        }

    def resolve_feed(self, line_number: str, trip_date: date) -> date | None:
        """Resolve a trip's GTFS feed the same way find_candidates.py does.

        Prefers scratch.trip_feed_resolution; falls back to the nearest
        route_shape_geoms feed by date when this (line, date) has no row there.
        """
        row = self.conn.execute(
            """
            WITH r AS (
                SELECT resolved_feed_version_date
                FROM scratch.trip_feed_resolution
                WHERE line_number = %(line)s AND trip_date = %(date)s
            )
            SELECT COALESCE(
                (SELECT resolved_feed_version_date FROM r),
                (
                    SELECT g.feed_version_date
                    FROM scratch.route_shape_geoms g
                    WHERE g.line_number = %(line)s
                    ORDER BY abs(g.feed_version_date - %(date)s)
                    LIMIT 1
                )
            )
            """,
            {"line": line_number, "date": trip_date},
        ).fetchone()
        return row[0] if row and row[0] else None

    def shapes_for(
        self, feed_version_date: date, line_number: str
    ) -> dict[tuple[date, str, str], ShapeGeom]:
        """Load (and cache) a (feed, line)'s UTM-projected ida/volta shapes.

        For scoring only (compute_direction_metrics needs meters, not
        degrees) -- see shapes_latlon_for for the map-display version.
        """
        key = (feed_version_date, line_number)
        if key not in self.shape_cache:
            self.shape_cache[key] = load_shapes(self.conn, [key])
        return self.shape_cache[key]

    def shapes_latlon_for(
        self, feed_version_date: date, line_number: str
    ) -> dict[str, list[tuple[float, float]]]:
        """Return shape_id -> [(lat, lon), ...] in WGS84, for map display only.

        scoring.load_shapes returns line_geom_proj (UTM 24S, meters) --
        plotting that directly on a lat/lon map would place the route
        thousands of kilometers off. This queries the plain line_geom
        column instead, same as trip_finder/app.py's own _fetch_shapes.
        """
        rows = self.conn.execute(
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

    def prior_evidence(self, vehicle_number: str) -> dict[int, int]:
        """Map candidate vehicle_id -> supporting confident-trip count for this bus.

        Drawn from the automatic sources (trip_labeler/trip_finder), used to
        force weakly-geometric but evidence-backed candidates into the shown list.
        """
        rows = self.conn.execute(
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
        self, trip_opened_at: datetime, trip_closed_at: datetime
    ) -> dict[int, dict[str, Any]]:
        """Return every AVL vehicle active in the trip window with its pings.

        Keyed by vehicle_id; each entry also tracks the device_id(s) seen so
        the most-common one in this window can be reported per candidate.
        """
        rows = self.conn.execute(
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


store = IdentityStore(DSN)
_lock = threading.Lock()


def _rank_candidates(
    trip: dict[str, Any], shapes: dict[tuple[Any, str, str], ShapeGeom]
) -> list[dict[str, Any]]:
    raw = store.candidates_for_trip(trip["trip_opened_at"], trip["trip_closed_at"])
    prior = store.prior_evidence(trip["vehicle_number"])
    shape_i = shapes.get((trip["resolved_feed_version_date"], trip["line_number"], "I"))
    shape_v = shapes.get((trip["resolved_feed_version_date"], trip["line_number"], "V"))

    scored: list[dict[str, Any]] = []
    for vehicle_id, entry in raw.items():
        n = len(entry["pings"])
        if n < MIN_PINGS_FOR_CANDIDATE and vehicle_id not in prior:
            continue
        lon = np.array([p[0] for p in entry["pings"]])
        lat = np.array([p[1] for p in entry["pings"]])
        x, y = project_pings(lon, lat, store.transformer)
        epoch = np.array([t.timestamp() for t in entry["ts"]])

        m_i = compute_direction_metrics(x, y, epoch, shape_i)
        m_v = compute_direction_metrics(x, y, epoch, shape_v)
        dists = [
            d
            for d in (m_i.avg_dist_to_line_m, m_v.avg_dist_to_line_m)
            if not np.isnan(d)
        ]
        best_dist = min(dists) if dists else None
        best_direction = None
        if dists:
            best_direction = "I" if best_dist == m_i.avg_dist_to_line_m else "V"

        device_id = max(entry["device_ids"], key=lambda k: entry["device_ids"][k])
        scored.append(
            {
                "candidate_vehicle_id": vehicle_id,
                "candidate_device_id": device_id,
                "n_pings_in_window": n,
                "best_avg_dist_to_line_m": best_dist,
                "best_direction": best_direction,
                "ida": {
                    "avg_dist_to_line_m": _clean(m_i.avg_dist_to_line_m),
                    "progress_corr": _clean(m_i.progress_corr),
                    "start_proximity_m": _clean(m_i.start_proximity_m),
                    "end_proximity_m": _clean(m_i.end_proximity_m),
                },
                "volta": {
                    "avg_dist_to_line_m": _clean(m_v.avg_dist_to_line_m),
                    "progress_corr": _clean(m_v.progress_corr),
                    "start_proximity_m": _clean(m_v.start_proximity_m),
                    "end_proximity_m": _clean(m_v.end_proximity_m),
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


def _clean(v: float) -> float | None:
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
    automatic = store.prior_evidence(vehicle_number)
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


def _bus_tally(vehicle_number: str, candidate_ids: set[int]) -> dict[str, Any]:
    """Summarize this bus's top few candidates by combined confidence.

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
    return {
        "n_trips_labeled": n_manual_trips,
        "n_candidates_with_evidence": len(ranked),
        "candidates": [
            {
                "candidate_vehicle_id": vid,
                "manual_selected_trips": manual_selected.get(vid, 0),
                **stats,
            }
            for vid, stats in ranked[:TALLY_TOP_N]
        ],
    }


@app.get("/")
def index() -> FileResponse:
    """Serve the labeling UI."""
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/next")
def api_next(*, advance_bus: bool = False) -> JSONResponse:
    """Return the next (bus, trip) to review.

    Auto-advances the bus queue whenever the current bus runs out of
    qualifying trips, or when the caller explicitly asks to move on
    (advance_bus=true).
    """
    with _lock:
        if advance_bus and store.current_bus is not None:
            store.skipped_this_session.add(store.current_bus)
            store.current_bus = None

        if store.current_bus is None:
            store.current_bus = store.next_bus()
        if store.current_bus is None:
            raise HTTPException(404, "no buses left to review")

        trip = store.next_trip(store.current_bus)
        while trip is None:
            store.skipped_this_session.add(store.current_bus)
            store.current_bus = store.next_bus()
            if store.current_bus is None:
                raise HTTPException(404, "no buses left with qualifying trips")
            trip = store.next_trip(store.current_bus)

        feed = store.resolve_feed(trip["line_number"], trip["trip_opened_at"].date())
        if feed is None:
            # this line has no shape in any feed at all -- skip straight to
            # another trip for the same bus rather than showing an empty map
            store.conn.execute(
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
            return api_next(advance_bus=False)

        trip["resolved_feed_version_date"] = feed
        shapes = store.shapes_for(feed, trip["line_number"])
        candidates = _rank_candidates(trip, shapes)
        shapes_latlon = store.shapes_latlon_for(feed, trip["line_number"])

        candidate_ids = {c["candidate_vehicle_id"] for c in candidates}
        confidence = _combined_confidence(trip["vehicle_number"], candidate_ids)
        for c in candidates:
            c["confidence"] = confidence[c["candidate_vehicle_id"]]

        return JSONResponse(
            {
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
                "bus_tally": _bus_tally(trip["vehicle_number"], candidate_ids),
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


if __name__ == "__main__":
    # tailnet-only dev box, matches trip_finder/trip_labeler's own exposure pattern
    uvicorn.run(app, host="0.0.0.0", port=8012)  # noqa: S104
