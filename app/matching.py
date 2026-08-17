"""Name normalization and the match between your Spotify artists and The List.

Matching is deliberately conservative. A false positive here sends you across
the Bay on a weeknight for a band you have never heard, so we only join on an
exact match of the normalized form -- no fuzzy distance, no substring hits.
Normalization absorbs the differences that are genuinely cosmetic (case,
accents, punctuation, a leading "The", ``&`` vs ``and``) and nothing else.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set

# Entries on The List that are events rather than artists. The source has no
# way to mark these, so we pattern-match the obvious ones to keep them out of
# the adjacency lookups (they would waste a Spotify search apiece).
_NON_ARTIST = re.compile(
    r"^(tba|tbd|and more|more tba|open mic|record swap|film screening|movie night|"
    r"karaoke|trivia|comedy( night| show)?|burlesque|dj night|benefit|"
    r"art show|poetry|bingo|drag( show| brunch)?)\b",
    re.IGNORECASE,
)

_PARENTHETICAL = re.compile(r"\([^)]*\)")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_LEADING_THE = re.compile(r"^the\s+")
_WS = re.compile(r"\s+")


def normalize(name: str) -> str:
    """Fold a name to its comparison key.

    >>> normalize("The Locust")
    'locust'
    >>> normalize("Sunn O)))")
    'sunn o'
    >>> normalize("Godspeed You! Black Emperor")
    'godspeed you black emperor'
    """
    text = unicodedata.normalize("NFKD", name)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = _PARENTHETICAL.sub(" ", text)
    text = text.replace("&", " and ")
    text = text.replace("+", " and ")
    text = _PUNCT.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    text = _LEADING_THE.sub("", text)
    return text.strip()


def looks_like_artist(name: str) -> bool:
    """False for the non-band entries The List mixes into its band slots."""
    cleaned = name.strip()
    if len(cleaned) < 2:
        return False
    if _NON_ARTIST.match(cleaned):
        return False
    if not normalize(cleaned):
        return False
    return True


@dataclass
class Artist:
    """An artist drawn from your Spotify account."""

    name: str
    spotify_id: str = ""
    genres: List[str] = field(default_factory=list)
    image: str = ""
    url: str = ""
    # Where in your account we found them, e.g. {"top", "saved", "playlist"}.
    sources: Set[str] = field(default_factory=set)
    play_count: int = 0

    @property
    def key(self) -> str:
        return normalize(self.name)


@dataclass
class Match:
    """One show whose band you already listen to."""

    band: str
    venue: str
    date: str
    weekday: str
    age: str
    price: str
    doors: str
    notes: str
    sold_out: bool
    recommended: bool
    artist: str
    artist_id: str
    artist_url: str
    artist_image: str
    genres: List[str]
    sources: List[str]
    play_count: int

    def as_dict(self) -> Dict[str, object]:
        return self.__dict__.copy()


def index_artists(artists: Iterable[Artist]) -> Dict[str, Artist]:
    """Collapse artists to a normalized-key index, merging duplicates.

    Spotify hands the same artist back from several endpoints; we union the
    provenance and sum the play counts so the UI can rank by familiarity.
    """
    index: Dict[str, Artist] = {}
    for artist in artists:
        key = artist.key
        if not key:
            continue
        existing = index.get(key)
        if existing is None:
            index[key] = artist
            continue
        existing.sources |= artist.sources
        existing.play_count += artist.play_count
        if not existing.spotify_id and artist.spotify_id:
            existing.spotify_id = artist.spotify_id
            existing.url = artist.url
        if not existing.image and artist.image:
            existing.image = artist.image
        if artist.genres and not existing.genres:
            existing.genres = artist.genres
    return index


def find_matches(shows: Sequence, artists: Iterable[Artist]) -> List[Match]:
    """Join The List against your library on the normalized name."""
    index = index_artists(artists)
    matches: List[Match] = []

    for show in shows:
        if not looks_like_artist(show.band):
            continue
        artist = index.get(normalize(show.band))
        if artist is None:
            continue
        matches.append(
            Match(
                band=show.band,
                venue=show.venue,
                date=show.date,
                weekday=show.weekday,
                age=show.age,
                price=show.price,
                doors=show.doors,
                notes=show.notes,
                sold_out=show.sold_out,
                recommended=show.recommended,
                artist=artist.name,
                artist_id=artist.spotify_id,
                artist_url=artist.url,
                artist_image=artist.image,
                genres=artist.genres[:4],
                sources=sorted(artist.sources),
                play_count=artist.play_count,
            )
        )

    matches.sort(key=lambda m: (m.date, m.venue, m.band))
    return matches


def unmatched_bands(shows: Sequence, artists: Iterable[Artist]) -> List[str]:
    """Plausible artists on The List that you do not already listen to.

    This is the candidate pool for the adjacency pass.
    """
    index = index_artists(artists)
    seen: Dict[str, str] = {}
    for show in shows:
        if not looks_like_artist(show.band):
            continue
        key = normalize(show.band)
        if key in index or key in seen:
            continue
        seen[key] = show.band
    return list(seen.values())
