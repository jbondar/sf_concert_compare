"""Optional "adjacent artists" pass -- bands you have *not* listened to.

This is kept rigorously separate from direct matches, both here and in the UI.
A direct match means you have the artist in your account. An adjacent hit is a
guess, and it is labelled as one everywhere it appears.

Two providers, because Spotify removed the obvious one:

``genre``   Zero config. Looks each candidate up on Spotify, reads the genre
            tags off the artist, and scores the overlap against a genre profile
            built from your own library. Costs one search per candidate, so the
            candidate pool is bounded.

``lastfm``  Needs a free API key. Asks Last.fm for artists similar to *your*
            top artists and intersects that with The List. Higher quality and
            far cheaper -- the cost scales with your library, not the list.

Spotify's own ``/artists/{id}/related-artists`` would have been ideal but was
cut off for new API clients on 2024-11-27, so it is deliberately not used.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import httpx

from .matching import Artist, normalize
from .spotify import SpotifyClient, search_artist

LASTFM_API = "https://ws.audioscrobbler.com/2.0/"


@dataclass
class Adjacent:
    """A band on The List we think is adjacent to your taste."""

    band: str
    score: float
    reason: str
    # The artists of yours that triggered this, for "because you listen to X".
    because_of: List[str] = field(default_factory=list)
    shared_genres: List[str] = field(default_factory=list)
    spotify_id: str = ""
    spotify_url: str = ""
    image: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "band": self.band,
            "score": round(self.score, 3),
            "reason": self.reason,
            "because_of": self.because_of,
            "shared_genres": self.shared_genres,
            "spotify_id": self.spotify_id,
            "spotify_url": self.spotify_url,
            "image": self.image,
        }


def build_genre_profile(artists: Iterable[Artist]) -> Dict[str, float]:
    """Weight each genre by how much of your listening it accounts for."""
    weights: Dict[str, float] = {}
    for artist in artists:
        if not artist.genres:
            continue
        # Dampen so one obsessively-played artist cannot own a genre outright.
        weight = 1.0 + min(artist.play_count, 20) / 10.0
        for genre in artist.genres:
            weights[genre.lower()] = weights.get(genre.lower(), 0.0) + weight

    if not weights:
        return {}
    ceiling = max(weights.values())
    return {genre: value / ceiling for genre, value in weights.items()}


def _artists_by_genre(artists: Iterable[Artist]) -> Dict[str, List[str]]:
    mapping: Dict[str, List[str]] = {}
    for artist in artists:
        for genre in artist.genres:
            mapping.setdefault(genre.lower(), []).append(artist.name)
    return mapping


async def adjacent_by_genre(
    client: SpotifyClient,
    candidates: Sequence[str],
    artists: Sequence[Artist],
    *,
    threshold: float = 0.12,
    concurrency: int = 8,
    progress: Optional[callable] = None,
) -> List[Adjacent]:
    """Score candidates by Spotify genre overlap with your library."""
    profile = build_genre_profile(artists)
    if not profile:
        return []
    by_genre = _artists_by_genre(artists)

    results: List[Adjacent] = []
    semaphore = asyncio.Semaphore(concurrency)
    counter = {"n": 0}
    lock = asyncio.Lock()

    async def handle(name: str) -> None:
        async with semaphore:
            item = await search_artist(client, name)
        async with lock:
            counter["n"] += 1
            if progress and counter["n"] % 10 == 0:
                progress(counter["n"], len(candidates))
        if not item:
            return

        genres = [g.lower() for g in (item.get("genres") or [])]
        shared = [g for g in genres if g in profile]
        if not shared:
            return

        # Strongest shared genre dominates; extra overlap adds a little.
        best = max(profile[g] for g in shared)
        score = min(1.0, best * (1.0 + 0.15 * (len(shared) - 1)))
        if score < threshold:
            return

        shared.sort(key=lambda g: profile[g], reverse=True)
        because: List[str] = []
        for genre in shared[:2]:
            for artist_name in by_genre.get(genre, [])[:3]:
                if artist_name not in because:
                    because.append(artist_name)

        images = item.get("images") or []
        results.append(
            Adjacent(
                band=name,
                score=score,
                reason="genre",
                because_of=because[:4],
                shared_genres=shared[:4],
                spotify_id=item.get("id", ""),
                spotify_url=(item.get("external_urls") or {}).get("spotify", ""),
                image=images[-1]["url"] if images else "",
            )
        )

    await asyncio.gather(*(handle(name) for name in candidates))
    results.sort(key=lambda a: a.score, reverse=True)
    return results


async def adjacent_by_lastfm(
    api_key: str,
    candidates: Sequence[str],
    artists: Sequence[Artist],
    *,
    seed_limit: int = 150,
    similar_limit: int = 60,
    concurrency: int = 4,
    progress: Optional[callable] = None,
) -> List[Adjacent]:
    """Intersect The List with Last.fm's similar-artist graph.

    We walk outward from your artists rather than looking up each candidate,
    so the request count tracks your library size and stays flat as The List
    grows.
    """
    seeds = sorted(artists, key=lambda a: a.play_count, reverse=True)[:seed_limit]
    if not seeds:
        return []

    wanted = {normalize(name): name for name in candidates}
    hits: Dict[str, Adjacent] = {}
    semaphore = asyncio.Semaphore(concurrency)
    counter = {"n": 0}
    lock = asyncio.Lock()

    async with httpx.AsyncClient(timeout=20) as http:

        async def handle(seed: Artist) -> None:
            async with semaphore:
                try:
                    response = await http.get(
                        LASTFM_API,
                        params={
                            "method": "artist.getsimilar",
                            "artist": seed.name,
                            "api_key": api_key,
                            "format": "json",
                            "limit": similar_limit,
                            "autocorrect": 1,
                        },
                    )
                    payload = response.json()
                except (httpx.HTTPError, ValueError):
                    return

            async with lock:
                counter["n"] += 1
                if progress and counter["n"] % 10 == 0:
                    progress(counter["n"], len(seeds))

            similar = ((payload.get("similarartists") or {}).get("artist")) or []
            for entry in similar:
                name = entry.get("name")
                if not name:
                    continue
                key = normalize(name)
                if key not in wanted:
                    continue
                try:
                    match_score = float(entry.get("match") or 0.0)
                except (TypeError, ValueError):
                    match_score = 0.0

                async with lock:
                    existing = hits.get(key)
                    if existing is None:
                        hits[key] = Adjacent(
                            band=wanted[key],
                            score=match_score,
                            reason="lastfm",
                            because_of=[seed.name],
                        )
                    else:
                        # Several of your artists pointing at the same band is
                        # a stronger signal than any single similarity score.
                        existing.score = max(existing.score, match_score)
                        if seed.name not in existing.because_of:
                            existing.because_of.append(seed.name)

        await asyncio.gather(*(handle(seed) for seed in seeds))

    results = list(hits.values())
    for adjacent in results:
        # Reward corroboration across multiple seeds.
        adjacent.score = min(
            1.0, adjacent.score * (1.0 + 0.2 * (len(adjacent.because_of) - 1))
        )
        adjacent.because_of = adjacent.because_of[:4]
    results.sort(key=lambda a: a.score, reverse=True)
    return results
