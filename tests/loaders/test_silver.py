"""Unit tests for the silver layer's partition-naming helpers."""

import datetime

from opa_database.loaders.silver import daily_partition_name, monthly_partition_name


def test_monthly_partition_name_pads_month():
    assert monthly_partition_name("avl_pings", 2023, 11) == "avl_pings_y2023m11"
    assert monthly_partition_name("afc_boardings", 2024, 3) == "afc_boardings_y2024m03"


def test_daily_partition_name_formats_date():
    date = datetime.date(2024, 3, 28)
    assert daily_partition_name("gtfs_stop_times", date) == "gtfs_stop_times_d20240328"
