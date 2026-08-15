"""Database access for the Trip Validity active learning app.

Every query here only ever reads/writes the `ml` schema: the app's own
`ml.trip_validity_labels`/`ml.trip_validity_model_runs` tables, plus
read-only access to `ml.trip_validity_dataset`,
`ml.trip_validity_route_shapes`, and `ml.trip_validity_trip_positions`
from the notebooks pipeline. Nothing here ever touches `silver.*`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal, LiteralString

import pandas as pd
import psycopg
from features import ALL_FEATURES
from psycopg import sql

from opa_database.config import settings

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

LabelSet = Literal["calibration", "test", "train"]
SelectionSource = Literal["random", "uncertain"]

_FEATURE_COLUMNS_SQL = sql.SQL(", ").join(sql.Identifier(c) for c in ALL_FEATURES)


def get_connection() -> psycopg.Connection:
    """Open a new autocommit connection to the database.

    Returns:
        An open connection, in autocommit mode so a single dropped query
        can't leave the interactive Streamlit session's shared
        connection stuck mid-transaction.

    """
    conn = psycopg.connect(settings.db_dsn)
    conn.autocommit = True
    return conn


def _fetch_frame(
    conn: psycopg.Connection,
    query: sql.Composed | LiteralString,
    params: Sequence[Any] = (),
) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        columns = [d.name for d in cur.description or []]
    return pd.DataFrame.from_records(rows, columns=columns)


def label_set_counts(conn: psycopg.Connection) -> dict[LabelSet, int]:
    """Count current labels per set.

    Args:
        conn: An open connection.

    Returns:
        Counts keyed by "calibration", "test", "train" (0 if empty).

    """
    counts: dict[LabelSet, int] = {"calibration": 0, "test": 0, "train": 0}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT label_set, count(*) FROM ml.trip_validity_labels "
            "GROUP BY label_set;"
        )
        counts.update(dict(cur.fetchall()))
    return counts


def fetch_random_unlabeled_trip_id(
    conn: psycopg.Connection, *, exclude_trip_ids: Collection[int] = ()
) -> int | None:
    """Draw one uniformly random trip that is labelable and not yet labeled.

    "Labelable" means it has at least one actual AVL position row, not
    just `avl_matched = true` (a matched trip can still have zero pings
    inside its time window, which would render an empty, unlabelable map).

    Args:
        conn: An open connection.
        exclude_trip_ids: Additional trip_ids to exclude beyond what's
            already labeled - e.g. this session's skipped trips, which
            are deliberately never written to the database but shouldn't
            resurface within the same run.

    Returns:
        A `trip_id`, or `None` if every trip with AVL positions has been
        labeled or excluded.

    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT d.trip_id FROM ml.trip_validity_dataset d "
            "WHERE d.avl_matched "
            "AND EXISTS ("
            "    SELECT 1 FROM ml.trip_validity_trip_positions p "
            "    WHERE p.trip_id = d.trip_id"
            ") "
            "AND NOT EXISTS ("
            "    SELECT 1 FROM ml.trip_validity_labels l WHERE l.trip_id = d.trip_id"
            ") "
            "AND NOT (d.trip_id = ANY(%s)) "
            "ORDER BY random() LIMIT 1;",
            (list(exclude_trip_ids),),
        )
        row = cur.fetchone()
        return row[0] if row else None


def is_unlabeled(conn: psycopg.Connection, trip_id: int) -> bool:
    """Check whether a trip has not been labeled yet.

    Args:
        conn: An open connection.
        trip_id: The trip to check.

    Returns:
        `True` if `trip_id` has no row in `ml.trip_validity_labels`.

    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ml.trip_validity_labels WHERE trip_id = %s;", (trip_id,)
        )
        return cur.fetchone() is None


def fetch_trip_row(conn: psycopg.Connection, trip_id: int) -> dict[str, Any] | None:
    """Fetch one trip's identifiers plus every model feature column.

    Args:
        conn: An open connection.
        trip_id: The trip to fetch.

    Returns:
        A dict of column name to value, or `None` if `trip_id` doesn't exist.

    """
    query = sql.SQL(
        "SELECT trip_id, bus_id, route_id, route_direction, trip_date, "
        "trip_hour, trip_opening_timestamp, trip_closing_timestamp, "
        "gtfs_feed_version_date, gtfs_shape_id_i, gtfs_shape_id_v, "
        "{features} FROM ml.trip_validity_dataset WHERE trip_id = %s;"
    ).format(features=_FEATURE_COLUMNS_SQL)
    with conn.cursor() as cur:
        cur.execute(query, (trip_id,))
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d.name for d in cur.description or []]
        return dict(zip(columns, row, strict=True))


def fetch_trip_map_data(conn: psycopg.Connection, trip_id: int) -> dict[str, Any]:
    """Fetch the GTFS shape(s) and AVL positions used to render a trip's map.

    Args:
        conn: An open connection.
        trip_id: The trip to fetch.

    Returns:
        Dict with keys "shape_i", "shape_v" (each a `[lon, lat]`
        coordinate list, or `None` if that direction has no matched
        shape) and "positions" (a list of `(lon, lat, iso_timestamp)`
        tuples ordered by time).

    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT "
            "  (SELECT ST_AsGeoJSON(rs.shape_geom) "
            "   FROM ml.trip_validity_route_shapes rs "
            "   WHERE rs.feed_version_date = d.gtfs_feed_version_date "
            "     AND rs.shape_id = d.gtfs_shape_id_i) AS shape_i_geojson, "
            "  (SELECT ST_AsGeoJSON(rs.shape_geom) "
            "   FROM ml.trip_validity_route_shapes rs "
            "   WHERE rs.feed_version_date = d.gtfs_feed_version_date "
            "     AND rs.shape_id = d.gtfs_shape_id_v) AS shape_v_geojson "
            "FROM ml.trip_validity_dataset d WHERE d.trip_id = %s;",
            (trip_id,),
        )
        shape_row = cur.fetchone()
        if shape_row is None:
            msg = f"trip_id {trip_id} not found in ml.trip_validity_dataset"
            raise ValueError(msg)
        shape_i_geojson, shape_v_geojson = shape_row

        cur.execute(
            "SELECT metric_timestamp, ST_X(geom), ST_Y(geom) "
            "FROM ml.trip_validity_trip_positions "
            "WHERE trip_id = %s ORDER BY metric_timestamp;",
            (trip_id,),
        )
        positions = [(lon, lat, ts.isoformat()) for ts, lon, lat in cur.fetchall()]

    def _coords(geojson_text: str | None) -> list[list[float]] | None:
        if geojson_text is None:
            return None
        return json.loads(geojson_text)["coordinates"]

    return {
        "shape_i": _coords(shape_i_geojson),
        "shape_v": _coords(shape_v_geojson),
        "positions": positions,
    }


def insert_label(
    conn: psycopg.Connection,
    *,
    trip_id: int,
    label: bool,
    label_set: LabelSet,
    selection_source: SelectionSource,
    predicted_probability: float | None,
) -> None:
    """Record one label.

    Args:
        conn: An open connection.
        trip_id: The labeled trip.
        label: `True` = valid trip, `False` = invalid trip.
        label_set: Which frozen/training set this label belongs to.
        selection_source: Whether this row was drawn at random or picked
            for being the model's most uncertain prediction.
        predicted_probability: The current model's calibrated probability
            for this trip at label time, or `None` if no model exists yet.

    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ml.trip_validity_labels "
            "(trip_id, label, label_set, selection_source, "
            " predicted_probability_at_label_time) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (trip_id) DO NOTHING;",
            (trip_id, label, label_set, selection_source, predicted_probability),
        )


def fetch_label_set(conn: psycopg.Connection, label_set: LabelSet) -> pd.DataFrame:
    """Fetch every labeled row in one set, joined to its feature columns.

    Args:
        conn: An open connection.
        label_set: "calibration", "test", or "train".

    Returns:
        A frame with `trip_id`, `label` (bool), and every column in
        `ALL_FEATURES`.

    """
    query = sql.SQL(
        "SELECT d.trip_id, l.label, {features} "
        "FROM ml.trip_validity_labels l "
        "JOIN ml.trip_validity_dataset d ON d.trip_id = l.trip_id "
        "WHERE l.label_set = %s;"
    ).format(features=_FEATURE_COLUMNS_SQL)
    return _fetch_frame(conn, query, (label_set,))


def fetch_unlabeled_pool(
    conn: psycopg.Connection,
    *,
    labelable_only: bool,
    exclude_trip_ids: Collection[int] = (),
) -> pd.DataFrame:
    """Fetch every not-yet-labeled row's feature columns.

    Args:
        conn: An open connection.
        labelable_only: If `True`, restrict to trips that can actually be
            shown on the labeling map (the active-learning candidate
            pool) - `avl_matched` plus at least one real AVL position row,
            since a matched trip can still have zero pings inside its
            time window. If `False`, cover the full remaining dataset
            (used for the interim/final confidence count over the whole
            ~1M-row table).
        exclude_trip_ids: Additional trip_ids to exclude beyond what's
            already labeled - e.g. this session's skipped trips, so they
            can't be ranked back into the active-learning candidate pool.

    Returns:
        A frame with `trip_id` and every column in `ALL_FEATURES`.

    """
    filter_clause = (
        sql.SQL(
            "AND d.avl_matched AND EXISTS ("
            "    SELECT 1 FROM ml.trip_validity_trip_positions p "
            "    WHERE p.trip_id = d.trip_id"
            ")"
        )
        if labelable_only
        else sql.SQL("")
    )
    query = sql.SQL(
        "SELECT d.trip_id, {features} FROM ml.trip_validity_dataset d "
        "WHERE NOT EXISTS ("
        "    SELECT 1 FROM ml.trip_validity_labels l WHERE l.trip_id = d.trip_id"
        ") {filter} AND NOT (d.trip_id = ANY(%s));"
    ).format(features=_FEATURE_COLUMNS_SQL, filter=filter_clause)
    return _fetch_frame(conn, query, (list(exclude_trip_ids),))


def insert_model_run(conn: psycopg.Connection, run: dict[str, Any]) -> int:
    """Persist one training run's config, metrics, and artifact location.

    Args:
        conn: An open connection.
        run: Keys matching `ml.trip_validity_model_runs`'s columns
            (`run_type`, `n_train_labels`, `hyperparameters`,
            `selected_features`, `calibration_params`, `cv_brier_score`,
            `test_auc`, `test_brier`, `test_log_loss`, `test_ece`,
            `confident_90_count`, `confident_90_total`, `artifact_path`).

    Returns:
        The new row's `run_id`.

    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ml.trip_validity_model_runs "
            "(run_type, n_train_labels, hyperparameters, selected_features, "
            " calibration_params, cv_brier_score, test_auc, test_brier, "
            " test_log_loss, test_ece, confident_90_count, confident_90_total, "
            " artifact_path) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING run_id;",
            (
                run["run_type"],
                run["n_train_labels"],
                json.dumps(run["hyperparameters"]),
                json.dumps(run["selected_features"]),
                json.dumps(run["calibration_params"])
                if run.get("calibration_params") is not None
                else None,
                run.get("cv_brier_score"),
                run.get("test_auc"),
                run.get("test_brier"),
                run.get("test_log_loss"),
                run.get("test_ece"),
                run.get("confident_90_count"),
                run.get("confident_90_total"),
                run["artifact_path"],
            ),
        )
        result = cur.fetchone()
        if result is None:
            msg = "INSERT ... RETURNING run_id unexpectedly returned no row"
            raise RuntimeError(msg)
        return result[0]


def fetch_model_runs(conn: psycopg.Connection) -> pd.DataFrame:
    """Fetch every model run, oldest first, for charting metrics over time.

    Args:
        conn: An open connection.

    Returns:
        A frame with one row per run, columns matching
        `ml.trip_validity_model_runs`.

    """
    return _fetch_frame(
        conn, "SELECT * FROM ml.trip_validity_model_runs ORDER BY created_at;"
    )


def fetch_latest_model_run(conn: psycopg.Connection) -> dict[str, Any] | None:
    """Fetch the most recent model run's full row.

    Args:
        conn: An open connection.

    Returns:
        A dict of column name to value, or `None` if no run exists yet.

    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM ml.trip_validity_model_runs "
            "ORDER BY created_at DESC LIMIT 1;"
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d.name for d in cur.description or []]
        return dict(zip(columns, row, strict=True))
