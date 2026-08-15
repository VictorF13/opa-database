"""Canonical feature column list for the Trip Validity model.

`CATEGORICAL_FEATURES` + `NUMERIC_FEATURES` together are every column of
`ml.trip_validity_dataset` at or after `trip_duration_seconds` (its 16th
column), plus `gtfs_route_has_both_directions`, `weekday_number`, and
`is_weekend` (all three added after the original 80-column build - see
`05_final_dataset.ipynb`'s "Adding weekday/weekend features" section for
the latter two). Every identifier and the `avl_matched`/`avl_match_source`
columns are deliberately excluded: AVL matching only decides which trips
can be *shown* on the labeling map (it gates
`ml.trip_validity_trip_positions`), it is not itself a model input.
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
