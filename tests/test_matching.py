"""Tests for name normalization and the compare itself."""

import pytest

from app.matching import (
    Artist,
    find_matches,
    index_artists,
    looks_like_artist,
    normalize,
    unmatched_bands,
)
from app.thelist import Show


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("The Locust", "locust"),
        ("the locust", "locust"),
        ("THE LOCUST", "locust"),
        ("Sunn O)))", "sunn o"),
        ("Godspeed You! Black Emperor", "godspeed you black emperor"),
        ("Sigur Rós", "sigur ros"),
        ("Simon & Garfunkel", "simon and garfunkel"),
        ("Simon and Garfunkel", "simon and garfunkel"),
        ("A Tribe Called Quest", "a tribe called quest"),
        ("Foo  Fighters ", "foo fighters"),
        ("Blink-182", "blink 182"),
        ("Blink 182", "blink 182"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_normalize_does_not_collapse_distinct_bands():
    """Guard against normalization getting so aggressive it creates collisions."""
    assert normalize("Wire") != normalize("Wires")
    assert normalize("Girls") != normalize("Girl")
    assert normalize("The Cure") != normalize("Cured")


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Ink And Dagger", True),
        ("dj Lead Teddy", True),
        ("record swap", False),
        ('film screening of "The Order"', False),
        ("TBA", False),
        ("open mic", False),
        ("karaoke", False),
        ("Comedy Night", False),
        ("X", False),  # too short to be a safe match
    ],
)
def test_looks_like_artist(name, expected):
    assert looks_like_artist(name) is expected


def _show(band, date="2026-09-01", venue="Gilman"):
    return Show(band=band, venue=venue, date=date, weekday="Tue")


def test_match_is_case_and_punctuation_insensitive():
    shows = [_show("The Locust"), _show("Sigur Ros")]
    artists = [Artist(name="the locust"), Artist(name="Sigur Rós")]
    matches = find_matches(shows, artists)
    assert {m.band for m in matches} == {"The Locust", "Sigur Ros"}


def test_no_fuzzy_matching():
    """A near-miss must not match -- false positives are the expensive error."""
    shows = [_show("Wires"), _show("The Cured")]
    artists = [Artist(name="Wire"), Artist(name="The Cure")]
    assert find_matches(shows, artists) == []


def test_non_artist_entries_are_skipped():
    shows = [_show("record swap"), _show("Ink And Dagger")]
    artists = [Artist(name="record swap"), Artist(name="Ink and Dagger")]
    matches = find_matches(shows, artists)
    assert [m.band for m in matches] == ["Ink And Dagger"]


def test_matches_are_sorted_by_date():
    shows = [
        _show("Band A", date="2026-10-05"),
        _show("Band B", date="2026-09-01"),
    ]
    artists = [Artist(name="Band A"), Artist(name="Band B")]
    matches = find_matches(shows, artists)
    assert [m.date for m in matches] == ["2026-09-01", "2026-10-05"]


def test_index_merges_duplicate_artists():
    artists = [
        Artist(name="The Locust", sources={"top"}, play_count=3, spotify_id="abc"),
        Artist(name="the locust", sources={"playlist"}, play_count=2),
    ]
    index = index_artists(artists)
    assert len(index) == 1
    merged = index["locust"]
    assert merged.sources == {"top", "playlist"}
    assert merged.play_count == 5
    assert merged.spotify_id == "abc"


def test_unmatched_bands_excludes_matches_and_junk():
    shows = [_show("Ink And Dagger"), _show("Le Shok"), _show("record swap")]
    artists = [Artist(name="Ink and Dagger")]
    assert unmatched_bands(shows, artists) == ["Le Shok"]


def test_one_artist_playing_twice_yields_two_matches():
    shows = [
        _show("Le Shok", date="2026-09-01", venue="Gilman"),
        _show("Le Shok", date="2026-09-14", venue="Bottom of the Hill"),
    ]
    matches = find_matches(shows, [Artist(name="Le Shok")])
    assert len(matches) == 2
