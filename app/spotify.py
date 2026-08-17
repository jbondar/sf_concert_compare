"""Spotify OAuth and the library scan that answers "who have I listened to?".

Spotify has no endpoint for your complete listening history, so we approximate
it by unioning every surface that implies you know an artist: your top artists
across all three time ranges, artists you follow, saved tracks and albums,
recently played, and the tracks in your playlists. That is a good proxy but it
is still a proxy -- for true lifetime coverage the UI also accepts a Spotify
"extended streaming history" export, which is the only complete record.

Note on scopes: everything here uses endpoints that survived the November 2024
Web API cuts. Related Artists and Recommendations did not, which is why
adjacency lives in :mod:`app.adjacency` and never calls Spotify for similarity.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

import httpx

from .matching import Artist

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

SCOPES = [
    "user-top-read",
    "user-follow-read",
    "user-library-read",
    "user-read-recently-played",
    "user-read-private",
    "playlist-read-private",
    "playlist-read-collaborative",
]


class SpotifyError(RuntimeError):
    """An API call failed in a way the user needs to hear about."""


@dataclass
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at - 60

    def to_dict(self) -> Dict[str, object]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "Tokens":
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token", "")),
            expires_at=float(data.get("expires_at", 0)),
        )


def build_auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
            # Force the consent screen so switching accounts actually works.
            "show_dialog": "true",
        }
    )
    return f"{AUTH_URL}?{query}"


def _basic_auth(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


async def exchange_code(
    code: str, client_id: str, client_secret: str, redirect_uri: str
) -> Tokens:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Authorization": _basic_auth(client_id, client_secret)},
        )
    if response.status_code != 200:
        raise SpotifyError(f"Token exchange failed: {response.text[:300]}")
    payload = response.json()
    return Tokens(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token", ""),
        expires_at=time.time() + payload.get("expires_in", 3600),
    )


async def refresh_tokens(
    tokens: Tokens, client_id: str, client_secret: str
) -> Tokens:
    if not tokens.refresh_token:
        raise SpotifyError("Session expired and no refresh token is available.")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
            },
            headers={"Authorization": _basic_auth(client_id, client_secret)},
        )
    if response.status_code != 200:
        raise SpotifyError(f"Token refresh failed: {response.text[:300]}")
    payload = response.json()
    return Tokens(
        access_token=payload["access_token"],
        # A refresh response may omit the refresh token; keep the old one.
        refresh_token=payload.get("refresh_token") or tokens.refresh_token,
        expires_at=time.time() + payload.get("expires_in", 3600),
    )


class SpotifyClient:
    """Thin async client with retry, refresh, and pagination helpers."""

    def __init__(
        self,
        tokens: Tokens,
        client_id: str,
        client_secret: str,
        on_refresh: Optional[Callable[[Tokens], None]] = None,
    ):
        self.tokens = tokens
        self.client_id = client_id
        self.client_secret = client_secret
        self.on_refresh = on_refresh
        self._client = httpx.AsyncClient(timeout=25)

    async def __aenter__(self) -> "SpotifyClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _ensure_token(self) -> None:
        if self.tokens.expired:
            self.tokens = await refresh_tokens(
                self.tokens, self.client_id, self.client_secret
            )
            if self.on_refresh:
                self.on_refresh(self.tokens)

    async def get(self, url: str, params: Optional[Dict] = None) -> Dict:
        await self._ensure_token()
        if url.startswith("/"):
            url = API_BASE + url

        for attempt in range(4):
            response = await self._client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self.tokens.access_token}"},
            )
            if response.status_code == 429:
                # Spotify tells us exactly how long to wait; obey it.
                delay = int(response.headers.get("Retry-After", "2")) + 1
                await asyncio.sleep(min(delay, 30))
                continue
            if response.status_code == 401 and attempt == 0:
                self.tokens.expires_at = 0
                await self._ensure_token()
                continue
            if response.status_code in (502, 503, 504):
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code >= 400:
                raise SpotifyError(
                    f"Spotify {response.status_code} on {url}: {response.text[:200]}"
                )
            return response.json()
        raise SpotifyError(f"Spotify kept failing on {url}")

    async def paginate(
        self, url: str, params: Optional[Dict] = None, limit_pages: int = 200
    ) -> AsyncIterator[Dict]:
        """Walk a paged collection, yielding each item."""
        page = await self.get(url, params)
        pages = 0
        while page is not None and pages < limit_pages:
            # Some endpoints nest the page under a key (e.g. {"artists": {...}}).
            body = page
            if "items" not in body:
                for value in body.values():
                    if isinstance(value, dict) and "items" in value:
                        body = value
                        break
            for item in body.get("items", []) or []:
                if item is not None:
                    yield item
            pages += 1
            nxt = body.get("next")
            if not nxt:
                break
            page = await self.get(nxt)

    async def me(self) -> Dict:
        return await self.get("/me")


def _artist_from_obj(obj: Dict, source: str, plays: int = 1) -> Optional[Artist]:
    name = (obj or {}).get("name")
    if not name:
        return None
    images = obj.get("images") or []
    return Artist(
        name=name,
        spotify_id=obj.get("id", "") or "",
        genres=list(obj.get("genres") or []),
        image=images[0]["url"] if images else "",
        url=(obj.get("external_urls") or {}).get("spotify", ""),
        sources={source},
        play_count=plays,
    )


@dataclass
class ScanProgress:
    stage: str
    detail: str = ""
    artists: int = 0
    done: bool = False


async def scan_library(
    client: SpotifyClient,
    *,
    include_playlists: bool = True,
    max_playlists: int = 60,
) -> AsyncIterator[Tuple[ScanProgress, Optional[List[Artist]]]]:
    """Yield progress updates, then a final tuple carrying the artist list.

    Structured as a generator so the web layer can stream progress over SSE --
    a full scan of a heavy account takes a while and a silent spinner is worse
    than a slow one.
    """
    artists: List[Artist] = []

    def add(obj: Dict, source: str, plays: int = 1) -> None:
        artist = _artist_from_obj(obj, source, plays)
        if artist:
            artists.append(artist)

    # --- Top artists: the highest-signal source, and it carries genres. ---
    for term, label in (
        ("long_term", "all time"),
        ("medium_term", "last 6 months"),
        ("short_term", "last 4 weeks"),
    ):
        yield ScanProgress("top", f"Top artists ({label})", len(artists)), None
        try:
            async for item in client.paginate(
                "/me/top/artists", {"limit": 50, "time_range": term}, limit_pages=4
            ):
                add(item, "top", plays=5)
        except SpotifyError:
            # A missing scope should not sink the whole scan.
            pass

    # --- Followed artists: an explicit statement of taste. ---
    yield ScanProgress("following", "Artists you follow", len(artists)), None
    try:
        async for item in client.paginate(
            "/me/following", {"type": "artist", "limit": 50}, limit_pages=40
        ):
            add(item, "following", plays=4)
    except SpotifyError:
        pass

    # --- Saved tracks. ---
    yield ScanProgress("saved", "Saved tracks", len(artists)), None
    try:
        async for item in client.paginate(
            "/me/tracks", {"limit": 50}, limit_pages=100
        ):
            for artist in ((item.get("track") or {}).get("artists") or []):
                add(artist, "saved", plays=3)
    except SpotifyError:
        pass

    # --- Saved albums. ---
    yield ScanProgress("albums", "Saved albums", len(artists)), None
    try:
        async for item in client.paginate(
            "/me/albums", {"limit": 50}, limit_pages=40
        ):
            for artist in ((item.get("album") or {}).get("artists") or []):
                add(artist, "albums", plays=3)
    except SpotifyError:
        pass

    # --- Recently played (Spotify caps this at the last 50 items). ---
    yield ScanProgress("recent", "Recently played", len(artists)), None
    try:
        async for item in client.paginate(
            "/me/player/recently-played", {"limit": 50}, limit_pages=2
        ):
            for artist in ((item.get("track") or {}).get("artists") or []):
                add(artist, "recent", plays=2)
    except SpotifyError:
        pass

    # --- Playlists: the long tail, and by far the slowest step. ---
    if include_playlists:
        playlists: List[Dict] = []
        try:
            async for item in client.paginate(
                "/me/playlists", {"limit": 50}, limit_pages=20
            ):
                playlists.append(item)
        except SpotifyError:
            playlists = []

        playlists = playlists[:max_playlists]
        for idx, playlist in enumerate(playlists, start=1):
            name = playlist.get("name") or "playlist"
            yield (
                ScanProgress(
                    "playlists",
                    f"Playlist {idx}/{len(playlists)}: {name}",
                    len(artists),
                ),
                None,
            )
            try:
                async for item in client.paginate(
                    f"/playlists/{playlist['id']}/tracks",
                    {"limit": 100, "fields": "items(track(artists(id,name))),next"},
                    limit_pages=20,
                ):
                    track = item.get("track") or {}
                    for artist in track.get("artists") or []:
                        add(artist, "playlist", plays=1)
            except SpotifyError:
                continue

    yield ScanProgress("done", "Scan complete", len(artists), done=True), artists


async def enrich_genres(
    client: SpotifyClient, artists: Sequence[Artist]
) -> None:
    """Fill in genres/images for artists we only saw as track credits.

    Called for the small set that actually matched, not the whole library.
    """
    missing = [a for a in artists if a.spotify_id and not a.genres]
    for start in range(0, len(missing), 50):
        batch = missing[start : start + 50]
        ids = ",".join(a.spotify_id for a in batch)
        try:
            payload = await client.get("/artists", {"ids": ids})
        except SpotifyError:
            continue
        by_id = {
            item["id"]: item for item in (payload.get("artists") or []) if item
        }
        for artist in batch:
            item = by_id.get(artist.spotify_id)
            if not item:
                continue
            artist.genres = list(item.get("genres") or [])
            images = item.get("images") or []
            if images and not artist.image:
                artist.image = images[0]["url"]


async def search_artist(client: SpotifyClient, name: str) -> Optional[Dict]:
    """Look up one artist by name, returning the best exact-ish hit."""
    from .matching import normalize

    try:
        payload = await client.get(
            "/search", {"q": name, "type": "artist", "limit": 5}
        )
    except SpotifyError:
        return None
    items = ((payload.get("artists") or {}).get("items")) or []
    target = normalize(name)
    for item in items:
        if normalize(item.get("name", "")) == target:
            return item
    return None


# --- Extended streaming history ---------------------------------------------

_HISTORY_ARTIST_KEYS = (
    "master_metadata_album_artist_name",  # endsong_*.json (extended export)
    "artistName",                          # StreamingHistory*.json (basic export)
    "artist_name",
)


def parse_streaming_history(blobs: Iterable[bytes]) -> List[Artist]:
    """Parse Spotify data-export JSON into artists with real play counts.

    Accepts both the extended (``endsong_*.json``) and basic
    (``StreamingHistory*.json``) export shapes.
    """
    counts: Dict[str, int] = {}
    display: Dict[str, str] = {}

    for blob in blobs:
        try:
            payload = json.loads(blob.decode("utf-8", errors="replace"))
        except (ValueError, AttributeError):
            continue
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            continue
        for row in payload:
            if not isinstance(row, dict):
                continue
            name = None
            for key in _HISTORY_ARTIST_KEYS:
                value = row.get(key)
                if value:
                    name = str(value)
                    break
            if not name:
                continue
            # Skip skips: under 30s of playback is not really a listen.
            ms = row.get("ms_played")
            if isinstance(ms, (int, float)) and ms < 30000:
                continue
            key = name.lower()
            counts[key] = counts.get(key, 0) + 1
            display.setdefault(key, name)

    return [
        Artist(name=display[key], sources={"history"}, play_count=count)
        for key, count in counts.items()
    ]
