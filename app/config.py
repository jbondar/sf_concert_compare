"""Runtime configuration, all of it from the environment.

Nothing secret is ever committed. See ``.env.example`` for the full set.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from functools import lru_cache


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    redirect_uri: str
    session_secret: str
    lastfm_api_key: str
    list_url: str
    list_ttl_seconds: int
    max_playlists: int
    adjacency_candidate_cap: int
    session_ttl_seconds: int
    cookie_secure: bool

    @property
    def spotify_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def lastfm_configured(self) -> bool:
        return bool(self.lastfm_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        client_id=os.getenv("SPOTIFY_CLIENT_ID", "").strip(),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", "").strip(),
        redirect_uri=os.getenv(
            "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8000/callback"
        ).strip(),
        # A generated secret is fine for a single process; set it explicitly
        # if you run more than one worker or want sessions to survive deploys.
        session_secret=os.getenv("SESSION_SECRET", "").strip()
        or secrets.token_urlsafe(32),
        lastfm_api_key=os.getenv("LASTFM_API_KEY", "").strip(),
        list_url=os.getenv(
            "LIST_URL", "http://www.foopee.com/punk/the-list/"
        ).strip(),
        list_ttl_seconds=int(os.getenv("LIST_TTL_SECONDS", str(6 * 60 * 60))),
        max_playlists=int(os.getenv("MAX_PLAYLISTS", "60")),
        adjacency_candidate_cap=int(os.getenv("ADJACENCY_CANDIDATE_CAP", "400")),
        session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", str(12 * 60 * 60))),
        cookie_secure=_flag("COOKIE_SECURE", False),
    )
