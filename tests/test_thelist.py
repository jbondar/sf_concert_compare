"""Parser tests against saved copies of the real foopee pages."""

from pathlib import Path

import pytest

from app.thelist import _split_details, parse_pages

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def snapshot():
    index = (FIXTURES / "index.html").read_text(encoding="utf-8", errors="replace")
    pages = [
        (FIXTURES / name).read_text(encoding="utf-8", errors="replace")
        for name in ("by-date.0.html", "by-date.1.html")
    ]
    return parse_pages(index, pages)


def test_finds_shows(snapshot):
    assert len(snapshot.shows) > 100
    assert len(snapshot.bands) > 100


def test_every_show_is_populated(snapshot):
    for show in snapshot.shows:
        assert show.band.strip()
        assert show.venue.strip()
        # Dates are ISO and the year is inferred, never blank.
        assert len(show.date) == 10 and show.date[4] == "-"


def test_parses_a_known_listing(snapshot):
    """The Gilman show on the first page, verbatim from the fixture."""
    gilman = [
        s
        for s in snapshot.shows
        if s.venue.startswith("924 Gilman") and s.date == "2026-08-16"
    ]
    bands = {s.band for s in gilman}
    assert "Ink And Dagger" in bands
    assert "Le Shok" in bands
    assert "X-Acto" in bands

    show = next(s for s in gilman if s.band == "Le Shok")
    assert show.age == "a/a"
    assert show.price == "$25"
    assert show.weekday == "Sun"


def test_year_is_inferred_from_the_index(snapshot):
    # The index says "updated 8/16/2026", so August shows are 2026.
    assert all(s.date.startswith("2026-08") for s in snapshot.shows if "-08-" in s.date)


def test_dates_are_ordered_and_contiguous(snapshot):
    dates = sorted({s.date for s in snapshot.shows})
    assert dates == sorted(dates)
    assert dates[0] < dates[-1]


def test_sold_out_is_detected(snapshot):
    sold = [s for s in snapshot.shows if s.sold_out]
    assert sold, "the fixture contains at least one sold-out show"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("a/a $25 5:30pm/6pm", {"age": "a/a", "price": "$25"}),
        ("21+ $15/$18 2pm/3pm", {"age": "21+", "price": "$15/$18"}),
        ("a/a free 2pm", {"age": "a/a", "price": "free"}),
    ],
)
def test_detail_parsing(text, expected):
    details = _split_details(text)
    for key, value in expected.items():
        assert details[key] == value


def test_sold_out_flag_from_details():
    assert _split_details("a/a $36.91 7:30pm @ (sold out)")["sold_out"] is True
    assert _split_details("a/a $36.91 7:30pm")["sold_out"] is False
