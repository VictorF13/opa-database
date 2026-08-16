"""Canonical feature column list for the Trip Validity model.

`CATEGORICAL_FEATURES` + `NUMERIC_FEATURES` together are every column of
`ml.trip_validity_dataset` at or after `trip_duration_seconds` (its 16th
column), plus columns added after the original 80-column build (see
`05_final_dataset.ipynb`): `gtfs_route_has_both_directions`,
`weekday_number`, `is_weekend`, `company_id`,
`trip_start_distance_to_nearest_garage_meters`,
`trip_end_distance_to_nearest_garage_meters`,
`route_i_straight_line_meters`, `route_i_straight_line_ratio`,
`route_v_straight_line_meters`, and `route_v_straight_line_ratio`.
Every identifier and the `avl_matched`/`avl_match_source` columns are
deliberately excluded: AVL matching only decides which trips can be
*shown* on the labeling map (it gates
`ml.trip_validity_trip_positions`), it is not itself a model input -
the model must never see AVL data, by design.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

CATEGORICAL_FEATURES: list[str] = [
    "gtfs_route_has_both_directions",
    # Treated as categorical (not a linear 1-7 ordinal) so a tree split
    # can group non-contiguous days (e.g. {Sat, Sun}) in one step,
    # instead of needing multiple threshold splits to approximate it.
    "weekday_number",
    "is_weekend",
    # The operator registry code (e.g. "02", "67") - text, not a number
    # (leading zero significant), and has no meaningful ordering, so a
    # tree split needs to be able to group arbitrary subsets of
    # companies rather than threshold a fake numeric ID.
    "company_id",
]

NUMERIC_FEATURES: list[str] = [
    "trip_duration_seconds",
    "route_avg_trip_duration_seconds_loo",
    "route_avg_trip_duration_n_trips_loo",
    "trip_duration_ratio_to_route_avg",
    "route_direction_avg_trip_duration_seconds_loo",
    "route_direction_avg_trip_duration_n_trips_loo",
    "trip_duration_ratio_to_route_direction_avg",
    "route_hour_avg_trip_duration_seconds_loo",
    "route_hour_avg_trip_duration_n_trips_loo",
    "trip_duration_ratio_to_route_hour_avg",
    "route_direction_hour_avg_trip_duration_seconds_loo",
    "route_direction_hour_avg_trip_duration_n_trips_loo",
    "trip_duration_ratio_to_route_direction_hour_avg",
    "route_reverse_direction_avg_trip_duration_seconds",
    "route_reverse_direction_n_trips",
    "trip_duration_ratio_to_reverse_direction_avg",
    "route_reverse_direction_hour_avg_trip_duration_seconds",
    "route_reverse_direction_hour_n_trips",
    "trip_duration_ratio_to_reverse_direction_hour_avg",
    "route_i_scheduled_duration_avg_seconds",
    "route_i_scheduled_duration_n_trips",
    "trip_duration_ratio_to_scheduled_i",
    "route_v_scheduled_duration_avg_seconds",
    "route_v_scheduled_duration_n_trips",
    "trip_duration_ratio_to_scheduled_v",
    "route_i_scheduled_duration_avg_seconds_at_hour",
    "route_i_scheduled_duration_n_trips_at_hour",
    "trip_duration_ratio_to_scheduled_i_at_hour",
    "route_v_scheduled_duration_avg_seconds_at_hour",
    "route_v_scheduled_duration_n_trips_at_hour",
    "trip_duration_ratio_to_scheduled_v_at_hour",
    "trip_fare_count",
    "fare_gap_avg_seconds",
    "fare_gap_stddev_seconds",
    "fare_gap_coefficient_of_variation",
    "fare_gap_avg_ratio_to_duration",
    "fare_span_seconds",
    "fare_span_ratio_to_duration",
    "trip_distance_meters",
    "route_i_length_meters",
    "trip_distance_ratio_to_route_i",
    "route_v_length_meters",
    "trip_distance_ratio_to_route_v",
    "trip_points_standard_distance_meters",
    "trip_cohesion_ratio_to_route_i",
    "trip_cohesion_ratio_to_route_v",
    "trip_start_distance_to_i_start_meters",
    "trip_start_offset_ratio_to_i",
    "trip_start_distance_to_v_start_meters",
    "trip_start_offset_ratio_to_v",
    "trip_end_distance_to_i_end_meters",
    "trip_end_offset_ratio_to_i",
    "trip_end_distance_to_v_end_meters",
    "trip_end_offset_ratio_to_v",
    "path_frechet_distance_to_i_meters",
    "path_match_score_frechet_i",
    "path_frechet_distance_to_v_meters",
    "path_match_score_frechet_v",
    "path_hausdorff_distance_to_i_meters",
    "path_match_score_hausdorff_i",
    "path_hausdorff_distance_to_v_meters",
    "path_match_score_hausdorff_v",
    "trip_progress_correlation_to_i",
    "trip_progress_correlation_to_v",
    "trip_progress_correlation_n_points",
    # Distance (meters) from this trip's first/last geo-tagged AFC fare
    # tap to the nearest garage of its operating company - deliberately
    # built from fare taps, never AVL (see module docstring). NULL for
    # trips with zero geo-tagged fares; LightGBM handles NaN natively.
    "trip_start_distance_to_nearest_garage_meters",
    "trip_end_distance_to_nearest_garage_meters",
    # Straight-line (not road-following) distance between a route's own
    # start/end point, and that distance as a fraction of the route's
    # actual length (bounded [0, 1] by the triangle inequality) - a
    # measure of how direct vs. winding/loop-shaped the route is. NULL
    # for a direction with no matched GTFS shape.
    "route_i_straight_line_meters",
    "route_i_straight_line_ratio",
    "route_v_straight_line_meters",
    "route_v_straight_line_ratio",
]

ALL_FEATURES: list[str] = [*CATEGORICAL_FEATURES, *NUMERIC_FEATURES]


def cast_feature_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """Cast a raw feature frame to the dtypes LightGBM expects.

    Args:
        df: Any frame containing a subset of `ALL_FEATURES` as columns,
            with values as returned by psycopg (Python `None` for SQL
            `NULL`).

    Returns:
        The same frame with categorical columns cast to pandas
        `category` dtype and numeric columns cast to `float64`, both of
        which represent missing values as NaN natively rather than
        needing imputation.

    """
    df = df.copy()
    for col in CATEGORICAL_FEATURES:
        if col in df.columns:
            df[col] = df[col].astype("category")
    for col in NUMERIC_FEATURES:
        if col in df.columns:
            df[col] = df[col].astype("float64")
    return df
