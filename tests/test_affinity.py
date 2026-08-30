"""Tests for the track-level evidence and the affinity score built on it.

The point of these fields is that the UI can say *why* a band matched, so the
tests assert on the human-facing output (evidence phrases, tier names) as much
as on the arithmetic.
"""

import json

import pytest

from app.matching import Artist, find_matches, index_artists
from app.spotify import parse_streaming_history
from app.thelist import Show


def show(band, date="2026-09-01"):
    return Show(
        band=band,
        venue="924 Gilman",
        date=date,
        weekday="tue",
        age="a/a",
        price="$15",
        doors="8pm",
        notes="",
        sold_out=False,
        recommended=False,
        url="",
    )


# --- merging -----------------------------------------------------------------


def test_merge_unions_evidence_from_every_endpoint():
    """Each Spotify endpoint contributes one slice; merging must keep all of it."""
    merged = index_artists(
        [
            Artist(name="Sightless Pit", sources={"top"}, top_rank=3,
                   top_ranges=["all time"]),
            Artist(name="Sightless Pit", sources={"saved"},
                   saved_tracks=["Kingscorpse"]),
            Artist(name="Sightless Pit", sources={"saved"},
                   saved_tracks=["Immersion Dispersal"]),
            Artist(name="Sightless Pit", sources={"following"}, following=True),
            Artist(name="Sightless Pit", sources={"playlist"},
                   playlists=["Loud"], playlist_tracks=1),
        ]
    )["sightless pit"]

    assert merged.sources == {"top", "saved", "following", "playlist"}
    assert merged.saved_tracks == ["Kingscorpse", "Immersion Dispersal"]
    assert merged.following is True
    assert merged.top_rank == 3
    assert merged.playlists == ["Loud"]


def test_merge_keeps_best_rank_and_all_time_ranges():
    merged = index_artists(
        [
            Artist(name="Chat Pile", top_rank=40, top_ranges=["all time"]),
            Artist(name="Chat Pile", top_rank=7, top_ranges=["last 4 weeks"]),
        ]
    )["chat pile"]

    assert merged.top_rank == 7
    assert merged.top_ranges == ["all time", "last 4 weeks"]


def test_merge_dedupes_titles_case_insensitively():
    merged = index_artists(
        [
            Artist(name="Wednesday", saved_tracks=["Bull Believer"]),
            Artist(name="Wednesday", saved_tracks=["bull believer", "Chosen to Deserve"]),
        ]
    )["wednesday"]

    assert merged.saved_tracks == ["Bull Believer", "Chosen to Deserve"]


def test_merge_caps_titles_so_payloads_stay_bounded():
    """A heavy library must not turn one artist into a thousand-title blob."""
    merged = index_artists(
        [Artist(name="Guided by Voices", saved_tracks=[f"Song {i}"]) for i in range(200)]
    )["guided by voices"]

    assert len(merged.saved_tracks) == 24


# --- scoring -----------------------------------------------------------------


def test_deliberate_acts_outrank_incidental_ones():
    """Saving songs and following beats drifting past in someone's playlist."""
    deliberate = Artist(
        name="A",
        following=True,
        saved_tracks=[f"t{i}" for i in range(8)],
        saved_albums=["LP"],
    )
    incidental = Artist(
        name="B",
        playlists=[f"p{i}" for i in range(6)],
        playlist_tracks=6,
    )
    assert deliberate.affinity > incidental.affinity


def test_top_rank_one_beats_top_rank_fifty():
    assert (
        Artist(name="A", top_rank=1, top_ranges=["all time"]).affinity
        > Artist(name="B", top_rank=50, top_ranges=["all time"]).affinity
    )


def test_affinity_stays_in_range_for_a_maximal_artist():
    everything = Artist(
        name="Everything",
        top_rank=1,
        top_ranges=["all time", "last 6 months", "last 4 weeks"],
        following=True,
        saved_tracks=[f"t{i}" for i in range(24)],
        saved_albums=[f"a{i}" for i in range(24)],
        playlists=[f"p{i}" for i in range(24)],
        recent_tracks=[f"r{i}" for i in range(24)],
        history_plays=99999,
    )
    assert everything.affinity == 100
    assert everything.tier == "favorite"


def test_an_artist_with_no_evidence_scores_zero():
    bare = Artist(name="Nobody")
    assert bare.affinity == 0
    assert bare.tier == "passing"
    assert bare.evidence() == []


@pytest.mark.parametrize(
    "artist,tier",
    [
        (Artist(name="a", top_rank=2, top_ranges=["all time"], following=True,
                saved_tracks=["x", "y", "z"], saved_albums=["lp"]), "favorite"),
        (Artist(name="b", saved_tracks=["x", "y", "z"], following=True), "regular"),
        (Artist(name="c", saved_tracks=["x", "y"]), "familiar"),
        (Artist(name="e", following=True), "familiar"),
        (Artist(name="d", playlists=["p"], playlist_tracks=1), "passing"),
        # Your #1 artist in every time range is a favorite on that alone.
        (Artist(name="f", top_rank=1,
                top_ranges=["all time", "last 6 months", "last 4 weeks"]), "favorite"),
        # In only one range it is strong, but not yet a favorite.
        (Artist(name="g", top_rank=1, top_ranges=["all time"]), "regular"),
        (Artist(name="h", top_rank=40, top_ranges=["all time"]), "familiar"),
    ],
)
def test_tiers(artist, tier):
    assert artist.tier == tier


def test_history_plays_are_compressed_not_linear():
    """Play counts are long-tailed; 10x the plays must not mean 10x the score."""
    few = Artist(name="a", history_plays=10).affinity
    many = Artist(name="b", history_plays=1000).affinity
    assert few < many < few * 4


# --- human-facing evidence ---------------------------------------------------


def test_evidence_reads_as_english():
    artist = Artist(
        name="Militarie Gun",
        top_rank=4,
        top_ranges=["all time", "last 4 weeks"],
        following=True,
        saved_tracks=["Do It Faster", "Very High"],
        saved_albums=["Life Under the Gun"],
        playlists=["Loud", "2024"],
        playlist_tracks=5,
        history_plays=1234,
    )
    assert artist.evidence() == [
        "#4 top artist (all time, last 4 weeks)",
        "you follow them",
        "2 saved songs",
        "1 saved album",
        "5 tracks across 2 playlists",
        "1,234 lifetime plays",
    ]


def test_evidence_singularizes():
    artist = Artist(name="X", saved_tracks=["one"], saved_albums=["lp"],
                    playlists=["p"], playlist_tracks=1, history_plays=1)
    assert "1 saved song" in artist.evidence()
    assert "1 saved album" in artist.evidence()
    assert "1 track across 1 playlist" in artist.evidence()
    assert "1 lifetime play" in artist.evidence()


# --- the join carries it through ---------------------------------------------


def test_matches_carry_the_evidence():
    artists = [
        Artist(name="Sightless Pit", sources={"saved"},
               saved_tracks=["Kingscorpse", "Immersion Dispersal", "Sundial"]),
        Artist(name="Sightless Pit", sources={"following"}, following=True),
    ]
    (match,) = find_matches([show("Sightless Pit")], artists)

    assert match.saved_track_count == 3
    assert match.saved_tracks == ["Kingscorpse", "Immersion Dispersal", "Sundial"]
    assert match.following is True
    assert match.tier == "regular"
    assert "3 saved songs" in match.evidence
    # as_dict is what the SSE payload serializes.
    assert "affinity" in match.as_dict()


def test_match_evidence_survives_json_round_trip():
    (match,) = find_matches(
        [show("Chat Pile")],
        [Artist(name="Chat Pile", saved_tracks=["Why"], top_rank=9,
                top_ranges=["all time"])],
    )
    payload = json.loads(json.dumps(match.as_dict()))
    assert payload["top_rank"] == 9
    assert payload["saved_tracks"] == ["Why"]


# --- streaming-history export ------------------------------------------------


def test_history_export_captures_track_titles_and_plays():
    rows = [
        {"master_metadata_album_artist_name": "Turnstile",
         "master_metadata_track_name": "Blackout", "ms_played": 180000},
        {"master_metadata_album_artist_name": "Turnstile",
         "master_metadata_track_name": "Blackout", "ms_played": 180000},
        {"master_metadata_album_artist_name": "Turnstile",
         "master_metadata_track_name": "Holiday", "ms_played": 120000},
        # A skip: under 30s does not count as a listen.
        {"master_metadata_album_artist_name": "Turnstile",
         "master_metadata_track_name": "Alien Love Call", "ms_played": 4000},
    ]
    (artist,) = parse_streaming_history([json.dumps(rows).encode()])

    assert artist.history_plays == 3
    assert artist.play_count == 3
    # Most played first, and the skipped track never made it in.
    assert artist.history_tracks == ["Blackout", "Holiday"]


def test_history_export_reads_the_basic_shape_too():
    rows = [{"artistName": "Fugazi", "trackName": "Waiting Room"}]
    (artist,) = parse_streaming_history([json.dumps(rows).encode()])
    assert artist.name == "Fugazi"
    assert artist.history_tracks == ["Waiting Room"]
