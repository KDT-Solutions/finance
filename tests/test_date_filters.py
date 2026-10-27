"""Schweizer Datumsanzeige in der Weboberfläche: chdate / chdatetime."""
from datetime import date, datetime, timezone

from app.routes.ui import chdate, chdatetime, templates


def test_utc_late_evening_is_next_day_in_zurich():
    v = datetime(2026, 10, 26, 23, 30, tzinfo=timezone.utc)
    assert chdate(v) == "27.10.2026"
    assert chdatetime(v) == "27.10.2026 00:30"
    assert chdatetime(v, seconds=True) == "27.10.2026 00:30:00"


def test_date_and_iso_string():
    assert chdate(date(2026, 10, 27)) == "27.10.2026"
    assert chdate("2026-10-27") == "27.10.2026"
    assert chdatetime("2026-10-27T08:05:00+00:00") == "27.10.2026 09:05"
    assert chdatetime("2026-10-27") == "27.10.2026"


def test_naive_datetime_is_shown_as_is():
    assert chdatetime(datetime(2026, 10, 27, 14, 5)) == "27.10.2026 14:05"


def test_empty_and_unparsable():
    assert chdate(None) == "" and chdate("") == ""
    assert chdatetime(None) == ""
    assert chdate("Unsinn") == "Unsinn"
    assert chdatetime("31.02.2026") == "31.02.2026"


def test_filters_registered():
    assert templates.env.filters["chdate"] is chdate
    assert templates.env.filters["chdatetime"] is chdatetime
