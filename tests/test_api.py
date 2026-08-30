"""End-to-end tests for the HTTP layer, with Spotify stubbed out.

These drive the real routes -- session cookie, SSE framing, scan pipeline and
the match join -- against a fake Spotify so they need no credentials and no
network.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.thelist import ListSnapshot, Show


# --- fakes -------------------------------------------------------------------


class FakeSpotify:
    """Implements just the surface `scan_library` and `enrich_genres` use."""

    def __init__(self, artists_by_endpoint=None, profile=None):
        self.pages = artists_by_endpoint or {}
        self.profile = profile or {"id": "tester", "display_name": "Tester"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def me(self):
        return self.profile

    async def get(self, url, params=None):
        if url == "/me":
            return self.profile
        if url == "/artists":
            ids = (params or {}).get("ids", "").split(",")
            return {
                "artists": [
                    {"id": i, "genres": ["east bay punk"], "images": []}
                    for i in ids
                    if i
                ]
            }
        return {"items": []}

    async def paginate(self, url, params=None, limit_pages=200):
        for item in self.pages.get(url, []):
            yield item


def _artist(name, artist_id):
    return {
        "id": artist_id,
        "name": name,
        "genres": ["east bay punk"],
        "images": [],
        "external_urls": {"spotify": f"https://open.spotify.com/artist/{artist_id}"},
    }


def _track(*artists, title=""):
    return {"track": {"name": title, "artists": list(artists)}}


SHOWS = [
    Show(band="Le Shok", venue="924 Gilman Street", date="2099-01-02", weekday="Sat"),
    Show(band="The Locust", venue="Bottom of the Hill", date="2099-01-03", weekday="Sun"),
    Show(band="Some Band You Do Not Know", venue="Ivy Room", date="2099-01-04", weekday="Mon"),
    Show(band="record swap", venue="Faction Brewing", date="2099-01-05", weekday="Tue"),
]


@pytest.fixture
def client(monkeypatch):
    snapshot = ListSnapshot(shows=SHOWS, updated_label="updated 1/1/2099")

    async def fake_get(force=False):
        return snapshot

    monkeypatch.setattr(main.list_cache, "get", fake_get)

    fake = FakeSpotify(
        {
            "/me/top/artists": [_artist("Le Shok", "id-leshok")],
            "/me/tracks": [
                _track({"id": "id-locust", "name": "the locust"}, title="Wet Dream War Machine"),
                _track({"id": "id-locust", "name": "the locust"}, title="Skin Graft at 75 Miles"),
            ],
        }
    )
    monkeypatch.setattr(main, "client_for", lambda session: fake)

    with TestClient(main.app) as test_client:
        # Forge a signed session so we do not need a real OAuth round trip.
        cookie = main.serializer.dumps(
            {
                "sid": "test-session",
                "tokens": {
                    "access_token": "fake",
                    "refresh_token": "fake",
                    "expires_at": time.time() + 3600,
                },
            }
        )
        test_client.cookies.set(main.COOKIE_NAME, cookie)
        yield test_client


def _events(body: str):
    """Parse an SSE body into (event, payload) pairs."""
    out = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            out.append((name, data))
    return out


# --- tests -------------------------------------------------------------------


def test_health_and_config():
    with TestClient(main.app) as client:
        assert client.get("/healthz").json()["ok"] is True
        assert client.get("/api/config").json()["signed_in"] is False


def test_scan_requires_auth():
    with TestClient(main.app) as client:
        assert client.get("/api/scan").status_code == 401
        assert client.get("/api/me").status_code == 401


def test_scan_streams_progress_then_result(client):
    body = client.get("/api/scan?include_playlists=false").text
    events = _events(body)
    names = [name for name, _ in events]

    assert "progress" in names
    assert names[-1] == "result", f"expected a result event, got {names}"

    result = events[-1][1]
    bands = {m["band"] for m in result["matches"]}

    # Both of the user's artists are on The List, one via a case difference.
    assert bands == {"Le Shok", "The Locust"}
    # And the bands they do not listen to stay out.
    assert "Some Band You Do Not Know" not in bands
    assert "record swap" not in bands


def test_scan_result_carries_provenance(client):
    result = _events(client.get("/api/scan?include_playlists=false").text)[-1][1]
    by_band = {m["band"]: m for m in result["matches"]}
    assert "top" in by_band["Le Shok"]["sources"]
    assert "saved" in by_band["The Locust"]["sources"]
    assert by_band["Le Shok"]["venue"] == "924 Gilman Street"
    assert by_band["Le Shok"]["date"] == "2099-01-02"


def test_scan_result_carries_track_level_evidence(client):
    """The whole point of the scan: not just that a band matched, but why."""
    result = _events(client.get("/api/scan?include_playlists=false").text)[-1][1]
    by_band = {m["band"]: m for m in result["matches"]}

    locust = by_band["The Locust"]
    assert locust["saved_track_count"] == 2
    assert locust["saved_tracks"] == ["Wet Dream War Machine", "Skin Graft at 75 Miles"]
    assert "2 saved songs" in locust["evidence"]

    # The fake returns the same page for all three time ranges, so Le Shok is
    # the #1 top artist in each of them.
    leshok = by_band["Le Shok"]
    assert leshok["top_rank"] == 1
    assert leshok["evidence"][0] == (
        "#1 top artist (all time, last 6 months, last 4 weeks)"
    )
    assert leshok["tier"] == "favorite"
    assert leshok["affinity"] > locust["affinity"]


def test_adjacent_requires_a_scan_first(client):
    main.SCANS.pop("test-session", None)
    events = _events(client.get("/api/adjacent").text)
    assert events[-1][0] == "error"
    assert "scan" in events[-1][1]["detail"].lower()


def test_history_upload_is_parsed(client):
    payload = json.dumps(
        [
            {"master_metadata_album_artist_name": "Le Shok", "ms_played": 200000},
            {"master_metadata_album_artist_name": "Le Shok", "ms_played": 200000},
            # Under 30s counts as a skip, not a listen.
            {"master_metadata_album_artist_name": "Skipped Band", "ms_played": 1000},
        ]
    ).encode()

    response = client.post(
        "/api/history", files={"files": ("Streaming_History_0.json", payload, "application/json")}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["artists"] == 1
    assert data["top"][0] == {"name": "Le Shok", "plays": 2}


def test_logout_clears_the_session(client):
    assert client.post("/api/logout").json()["ok"] is True
    assert main.SCANS.get("test-session") is None
