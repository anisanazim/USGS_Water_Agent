from __future__ import annotations

from datetime import date

import pytest

from water_agent.temporal import (
    TemporalRequest,
    TemporalResolutionError,
    TemporalSpec,
    resolve_temporal,
)

TODAY_DEFAULT = date(2026, 11, 10)


@pytest.mark.parametrize(
    "req_kwargs,today,exp_start,exp_end,exp_months,exp_needs_mask",
    [
        # Explicit ranges
        (
            {"phrase": "between January 2020 and July 2023", "start": "2020-01", "end": "2023-07"},
            TODAY_DEFAULT,
            date(2020, 1, 1),
            date(2023, 7, 31),
            frozenset(),
            False,
        ),
        (
            {"start": "2021", "end": "2021"},
            TODAY_DEFAULT,
            date(2021, 1, 1),
            date(2021, 12, 31),
            frozenset(),
            False,
        ),
        (
            {"start": "2024-02-10", "end": "2024-02"},
            TODAY_DEFAULT,
            date(2024, 2, 10),
            date(2024, 2, 29),
            frozenset(),
            False,
        ),
        (
            {"start": "2023-02", "end": "2023-02"},
            TODAY_DEFAULT,
            date(2023, 2, 1),
            date(2023, 2, 28),
            frozenset(),
            False,
        ),
        (
            {"start": "2020"},
            TODAY_DEFAULT,
            date(2020, 1, 1),
            None,
            frozenset(),
            False,
        ),
        (
            {"end": "2022-06"},
            TODAY_DEFAULT,
            None,
            date(2022, 6, 30),
            frozenset(),
            False,
        ),
        # Year only
        (
            {"year": 2021},
            TODAY_DEFAULT,
            date(2021, 1, 1),
            date(2021, 12, 31),
            frozenset(),
            False,
        ),
        # Month + year (numeric & text)
        (
            {"month": 3, "year": 2021},
            TODAY_DEFAULT,
            date(2021, 3, 1),
            date(2021, 3, 31),
            frozenset({3}),
            False,
        ),
        (
            {"month": "March", "year": "2021"},
            TODAY_DEFAULT,
            date(2021, 3, 1),
            date(2021, 3, 31),
            frozenset({3}),
            False,
        ),
        (
            {"month": "feb", "year": 2024},
            TODAY_DEFAULT,
            date(2024, 2, 1),
            date(2024, 2, 29),
            frozenset({2}),
            False,
        ),
        # Season + year
        (
            {"phrase": "winter 2022", "season": "winter", "year": 2022},
            TODAY_DEFAULT,
            date(2021, 12, 1),
            date(2022, 2, 28),
            frozenset({12, 1, 2}),
            False,
        ),
        (
            {"phrase": "winter 2022-23", "season": "winter", "year": 2023},
            TODAY_DEFAULT,
            date(2022, 12, 1),
            date(2023, 2, 28),
            frozenset({12, 1, 2}),
            False,
        ),
        (
            {"season": "winter", "year": 2024},
            date(2024, 3, 1),
            date(2023, 12, 1),
            date(2024, 2, 29),
            frozenset({12, 1, 2}),
            False,
        ),
        (
            {"season": "spring", "year": 2022},
            TODAY_DEFAULT,
            date(2022, 3, 1),
            date(2022, 5, 31),
            frozenset({3, 4, 5}),
            False,
        ),
        (
            {"season": "summer", "year": 2022},
            TODAY_DEFAULT,
            date(2022, 6, 1),
            date(2022, 8, 31),
            frozenset({6, 7, 8}),
            False,
        ),
        (
            {"season": "fall", "year": 2022},
            TODAY_DEFAULT,
            date(2022, 9, 1),
            date(2022, 11, 30),
            frozenset({9, 10, 11}),
            False,
        ),
        (
            {"season": "autumn", "year": 2022},
            TODAY_DEFAULT,
            date(2022, 9, 1),
            date(2022, 11, 30),
            frozenset({9, 10, 11}),
            False,
        ),
        # Month / season without year -> needs_month_filter True
        (
            {"month": 3},
            TODAY_DEFAULT,
            None,
            None,
            frozenset({3}),
            True,
        ),
        (
            {"season": "winter"},
            TODAY_DEFAULT,
            None,
            None,
            frozenset({12, 1, 2}),
            True,
        ),
        # Relative rules on today=2026-11-10
        (
            {"relative": "this", "unit": "year"},
            TODAY_DEFAULT,
            date(2026, 1, 1),
            date(2026, 11, 10),
            frozenset(),
            False,
        ),
        (
            {"relative": "last", "unit": "year"},
            TODAY_DEFAULT,
            date(2025, 1, 1),
            date(2025, 12, 31),
            frozenset(),
            False,
        ),
        (
            {"relative": "this", "unit": "month"},
            TODAY_DEFAULT,
            date(2026, 11, 1),
            date(2026, 11, 10),
            frozenset({11}),
            False,
        ),
        (
            {"relative": "last", "unit": "month"},
            TODAY_DEFAULT,
            date(2026, 10, 1),
            date(2026, 10, 31),
            frozenset({10}),
            False,
        ),
        (
            {"relative": "last", "unit": "month"},
            date(2026, 1, 10),
            date(2025, 12, 1),
            date(2025, 12, 31),
            frozenset({12}),
            False,
        ),
        (
            {"relative": "past", "unit": "year", "n": 5},
            TODAY_DEFAULT,
            date(2021, 11, 10),
            date(2026, 11, 10),
            frozenset(),
            False,
        ),
        (
            {"relative": "past", "unit": "month", "n": 3},
            TODAY_DEFAULT,
            date(2026, 8, 10),
            date(2026, 11, 10),
            frozenset(),
            False,
        ),
        (
            {"relative": "past", "unit": "year", "n": 1},
            date(2028, 2, 29),
            date(2027, 2, 28),
            date(2028, 2, 29),
            frozenset(),
            False,
        ),
        (
            {"relative": "last", "month": 3},
            TODAY_DEFAULT,
            date(2026, 3, 1),
            date(2026, 3, 31),
            frozenset({3}),
            False,
        ),
        (
            {"relative": "last", "month": 3},
            date(2026, 3, 15),
            date(2025, 3, 1),
            date(2025, 3, 31),
            frozenset({3}),
            False,
        ),
        (
            {"relative": "this", "month": 3},
            TODAY_DEFAULT,
            date(2026, 3, 1),
            date(2026, 3, 31),
            frozenset({3}),
            False,
        ),
        (
            {"relative": "last", "season": "winter"},
            TODAY_DEFAULT,
            date(2025, 12, 1),
            date(2026, 2, 28),
            frozenset({12, 1, 2}),
            False,
        ),
        (
            {"relative": "last", "season": "summer"},
            TODAY_DEFAULT,
            date(2026, 6, 1),
            date(2026, 8, 31),
            frozenset({6, 7, 8}),
            False,
        ),
        (
            {"relative": "last", "season": "fall"},
            TODAY_DEFAULT,
            date(2025, 9, 1),
            date(2025, 11, 30),
            frozenset({9, 10, 11}),
            False,
        ),
        (
            {"relative": "this", "season": "winter"},
            date(2026, 1, 15),
            date(2025, 12, 1),
            date(2026, 2, 28),
            frozenset({12, 1, 2}),
            False,
        ),
        (
            {"relative": "since", "year": 2020},
            TODAY_DEFAULT,
            date(2020, 1, 1),
            None,
            frozenset(),
            False,
        ),
        (
            {"relative": "this", "unit": "season"},
            date(2026, 1, 15),
            date(2025, 12, 1),
            date(2026, 2, 28),
            frozenset({12, 1, 2}),
            False,
        ),
        (
            {"relative": "last", "unit": "season"},
            TODAY_DEFAULT,
            date(2026, 6, 1),
            date(2026, 8, 31),
            frozenset({6, 7, 8}),
            False,
        ),
        (
            {"relative": "past", "unit": "month", "n": 1},
            date(2026, 3, 31),
            date(2026, 2, 28),
            date(2026, 3, 31),
            frozenset(),
            False,
        ),
        (
            {"start": "2020-01-15", "end": "2023-07-10"},
            TODAY_DEFAULT,
            date(2020, 1, 15),
            date(2023, 7, 10),
            frozenset(),
            False,
        ),
    ],
)
def test_resolve_temporal_valid_cases(
    req_kwargs, today, exp_start, exp_end, exp_months, exp_needs_mask
):
    spec = resolve_temporal(TemporalRequest(**req_kwargs), today=today)
    assert spec.start == exp_start
    assert spec.end == exp_end
    assert spec.months == exp_months
    assert spec.needs_month_filter is exp_needs_mask


def test_labels_include_computed_dates_and_descriptions():
    w22 = resolve_temporal(
        TemporalRequest(phrase="winter 2022", season="winter", year=2022),
        today=TODAY_DEFAULT,
    )
    assert w22.label == "winter 2022 (2021-12-01 to 2022-02-28)"

    past5 = resolve_temporal(
        TemporalRequest(relative="past", unit="year", n=5, phrase="past 5 years"),
        today=TODAY_DEFAULT,
    )
    assert "2021-11-10" in past5.label
    assert "2026-11-10" in past5.label


@pytest.mark.parametrize(
    "req_kwargs,today,match",
    [
        ({"month": 13, "year": 2022}, TODAY_DEFAULT, "1-12"),
        ({"month": 0, "year": 2022}, TODAY_DEFAULT, "1-12"),
        ({"month": "Marchuary", "year": 2022}, TODAY_DEFAULT, "month"),
        ({"season": "monsoon", "year": 2022}, TODAY_DEFAULT, "season"),
        ({"year": 1799}, TODAY_DEFAULT, "1800"),
        ({"year": 2027}, TODAY_DEFAULT, "2026"),
        ({"relative": "past", "unit": "year", "n": 0}, TODAY_DEFAULT, "at least 1"),
        ({"relative": "past", "unit": "year", "n": -2}, TODAY_DEFAULT, "at least 1"),
        ({"start": "2020", "month": 3}, TODAY_DEFAULT, "Cannot combine explicit start/end"),
        ({"month": 3, "season": "spring", "year": 2022}, TODAY_DEFAULT, "month and a season"),
        ({"relative": "last", "start": "2020"}, TODAY_DEFAULT, "explicit start or end"),
        ({"relative": "since", "start": "2020"}, TODAY_DEFAULT, "explicit start or end"),
        ({"start": "2022-01", "end": "2020-01"}, TODAY_DEFAULT, "after end date"),
        ({"start": "2026-12"}, TODAY_DEFAULT, "future"),
        ({"start": "2023-02-30"}, TODAY_DEFAULT, "Invalid calendar date"),
        ({"start": "January 2020"}, TODAY_DEFAULT, "Unrecognized date format"),
        ({"month": 12, "relative": "this"}, TODAY_DEFAULT, "hasn't happened yet"),
        ({"season": "winter", "relative": "this"}, TODAY_DEFAULT, "last winter"),
        ({"phrase": "back then"}, TODAY_DEFAULT, "back then"),
        ({}, TODAY_DEFAULT, "Empty temporal request"),
    ],
)
def test_resolve_temporal_errors(req_kwargs, today, match):
    with pytest.raises(TemporalResolutionError, match=match):
        resolve_temporal(TemporalRequest(**req_kwargs), today=today)


def test_temporal_spec_serializers_and_month_filter():
    # Open start
    since = TemporalSpec(start=date(2020, 1, 1), end=None, months=frozenset(), label="from 2020")
    assert since.to_samples_params() == {"activityStartDateLower": "2020-01-01"}
    assert since.to_ogc_datetime() == "2020-01-01/.."
    assert since.needs_month_filter is False

    # Open end
    until = TemporalSpec(start=None, end=date(2023, 7, 31), months=frozenset(), label="to 2023")
    assert until.to_samples_params() == {"activityStartDateUpper": "2023-07-31"}
    assert until.to_ogc_datetime() == "../2023-07-31"

    # Both unbounded
    none_spec = TemporalSpec(start=None, end=None, months=frozenset({3}), label="March")
    assert none_spec.to_samples_params() == {}
    assert none_spec.to_ogc_datetime() is None
    assert none_spec.needs_month_filter is True

    # Range spanning non-matching months triggers needs_month_filter
    multi_year_march = TemporalSpec(
        start=date(2020, 1, 1),
        end=date(2023, 12, 31),
        months=frozenset({3}),
        label="March 2020-2023",
    )
    assert multi_year_march.needs_month_filter is True

    # Season range contained completely inside months does not need filter
    winter = TemporalSpec(
        start=date(2021, 12, 1),
        end=date(2022, 2, 28),
        months=frozenset({12, 1, 2}),
        label="winter 2022",
    )
    assert winter.needs_month_filter is False
