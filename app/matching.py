"""Name normalization and the match between your Spotify artists and The List.

Matching is deliberately conservative. A false positive here sends you across
the Bay on a weeknight for a band you have never heard, so we only join on an
exact match of the normalized form -- no fuzzy distance, no substring hits.
Normalization absorbs the differences that are genuinely cosmetic (case,
accents, punctuation, a leading "The", ``&`` vs ``and``) and nothing else.
"""

from __future__ import annotations

import math
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


# How many titles we keep per artist. Enough to show the user *which* songs
# drove the match without turning the payload into a copy of their library.
_EVIDENCE_CAP = 24


@dataclass
class Artist:
    """An artist drawn from your Spotify account, with the evidence for it.

    ``play_count`` is a coarse weight summed across the endpoints an artist
    turned up in. The fields under it are the real story: the actual songs you
    saved, the albums, the playlists they sit in, how high they rank in your
    top artists. Those are what the UI shows, because "4 of your saved songs"
    is an answer and "weight 17" is not.
    """

    name: str
    spotify_id: str = ""
    genres: List[str] = field(default_factory=list)
    image: str = ""
    url: str = ""
    # Where in your account we found them, e.g. {"top", "saved", "playlist"}.
    sources: Set[str] = field(default_factory=set)
    play_count: int = 0

    # --- Track-level evidence ------------------------------------------------
    saved_tracks: List[str] = field(default_factory=list)
    saved_albums: List[str] = field(default_factory=list)
    playlists: List[str] = field(default_factory=list)
    playlist_tracks: int = 0
    recent_tracks: List[str] = field(default_factory=list)
    # Best (lowest) position across the three top-artist time ranges, 1-based.
    top_rank: Optional[int] = None
    top_ranges: List[str] = field(default_factory=list)
    following: bool = False
    history_plays: int = 0
    # Most-played titles from an uploaded streaming-history export.
    history_tracks: List[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return normalize(self.name)

    @property
    def saved_track_count(self) -> int:
        return len(self.saved_tracks)

    @property
    def affinity(self) -> int:
        """How well you know this artist, 0-100.

        Deliberately weighted toward deliberate acts. Saving a song and
        following an artist are choices; a track drifting past in a playlist
        someone else made is barely evidence at all.
        """
        score = 0.0

        if self.top_rank is not None:
            # Rank 1 is worth the full 46, rank 50 about 9. Holding a rank in
            # more than one time range is strong corroboration -- your #1 all
            # time who is also your #1 this month is not a passing phase, and
            # that alone should be enough to read as a favorite.
            score += 46 * max(0.2, 1 - (self.top_rank - 1) / 60)
            score += 10 * (len(self.top_ranges) - 1)

        # The first saved song is the expensive one: it is a deliberate act,
        # where the tenth is mostly confirmation. Same shape for albums.
        if self.saved_tracks:
            score += 8 + 2.6 * min(self.saved_track_count - 1, 9)
        if self.saved_albums:
            score += 7 + 4.0 * min(len(self.saved_albums) - 1, 3)
        if self.following:
            score += 14

        score += 2.0 * min(len(self.playlists), 6)
        score += 1.5 * min(len(self.recent_tracks), 4)

        if self.history_plays:
            # Play counts are heavily long-tailed, so compress them.
            score += min(30.0, 9.0 * math.log10(1 + self.history_plays))

        return max(0, min(100, round(score)))

    @property
    def tier(self) -> str:
        score = self.affinity
        if score >= 65:
            return "favorite"
        if score >= 25:
            return "regular"
        if score >= 10:
            return "familiar"
        return "passing"

    def evidence(self) -> List[str]:
        """Short human phrases explaining why this artist is in your library."""
        parts: List[str] = []
        if self.top_rank is not None:
            ranges = ", ".join(self.top_ranges)
            parts.append(f"#{self.top_rank} top artist ({ranges})")
        if self.following:
            parts.append("you follow them")
        if self.saved_tracks:
            n = len(self.saved_tracks)
            parts.append(f"{n} saved song{'s' if n != 1 else ''}")
        if self.saved_albums:
            n = len(self.saved_albums)
            parts.append(f"{n} saved album{'s' if n != 1 else ''}")
        if self.playlists:
            n = len(self.playlists)
            parts.append(
                f"{self.playlist_tracks} track{'s' if self.playlist_tracks != 1 else ''}"
                f" across {n} playlist{'s' if n != 1 else ''}"
            )
        if self.recent_tracks:
            parts.append("played recently")
        if self.history_plays:
            n = self.history_plays
            parts.append(f"{n:,} lifetime play{'s' if n != 1 else ''}")
        return parts


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
    # Why this artist counts as yours -- see Artist.evidence().
    affinity: int
    tier: str
    evidence: List[str]
    saved_tracks: List[str]
    saved_track_count: int
    saved_albums: List[str]
    playlists: List[str]
    top_rank: Optional[int]
    following: bool
    history_plays: int
    history_tracks: List[str]

    def as_dict(self) -> Dict[str, object]:
        return self.__dict__.copy()


def _merge_titles(into: List[str], extra: Iterable[str]) -> None:
    """Append unseen titles, case-insensitively, up to the evidence cap."""
    seen = {t.casefold() for t in into}
    for title in extra:
        if len(into) >= _EVIDENCE_CAP:
            return
        folded = title.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        into.append(title)


def index_artists(artists: Iterable[Artist]) -> Dict[str, Artist]:
    """Collapse artists to a normalized-key index, merging duplicates.

    Spotify hands the same artist back from several endpoints, each carrying a
    different slice of the evidence -- the saved songs from one call, the
    playlists from another. Merging unions all of it onto one record.
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

        _merge_titles(existing.saved_tracks, artist.saved_tracks)
        _merge_titles(existing.saved_albums, artist.saved_albums)
        _merge_titles(existing.playlists, artist.playlists)
        _merge_titles(existing.recent_tracks, artist.recent_tracks)
        _merge_titles(existing.history_tracks, artist.history_tracks)
        existing.playlist_tracks += artist.playlist_tracks
        existing.following = existing.following or artist.following
        existing.history_plays += artist.history_plays
        if artist.top_rank is not None:
            existing.top_rank = (
                artist.top_rank
                if existing.top_rank is None
                else min(existing.top_rank, artist.top_rank)
            )
        for label in artist.top_ranges:
            if label not in existing.top_ranges:
                existing.top_ranges.append(label)
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
                affinity=artist.affinity,
                tier=artist.tier,
                evidence=artist.evidence(),
                saved_tracks=artist.saved_tracks[:8],
                saved_track_count=artist.saved_track_count,
                saved_albums=artist.saved_albums[:4],
                playlists=artist.playlists[:5],
                top_rank=artist.top_rank,
                following=artist.following,
                history_plays=artist.history_plays,
                history_tracks=artist.history_tracks[:6],
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
