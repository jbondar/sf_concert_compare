"""FastAPI app: Spotify sign-in, library scan, and the compare against The List."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer

from .adjacency import adjacent_by_genre, adjacent_by_lastfm
from .config import get_settings
from .matching import Artist, find_matches, index_artists, unmatched_bands
from .spotify import (
    SpotifyClient,
    SpotifyError,
    Tokens,
    build_auth_url,
    enrich_genres,
    exchange_code,
    parse_streaming_history,
    scan_library,
)
from .thelist import ListCache

STATIC_DIR = Path(__file__).parent / "static"
COOKIE_NAME = "sfcc_session"

settings = get_settings()
serializer = URLSafeSerializer(settings.session_secret, salt="sfcc")
list_cache = ListCache(settings.list_ttl_seconds, settings.list_url)

# Where the browser thinks we live. Empty at a domain root; "/sfconcert" when a
# reverse proxy mounts us at a subpath. The proxy strips the prefix before we
# see it, so routes below stay unprefixed -- this is only for URLs we hand out.
BASE = settings.base_path
# Confine the session cookie to our own subtree so a subpath deploy cannot
# collide with anything else on the domain.
COOKIE_PATH = f"{BASE}/" if BASE else "/"

# Deliberately no root_path=BASE here. Starlette strips root_path from the
# path before routing into a Mount, which 404s every /static/* request and
# leaves the page loading with no CSS or JS. We prefix the handful of URLs we
# emit ourselves (the <base> tag and the auth redirects), so root_path would
# buy nothing and only break things.
app = FastAPI(title="SF Concert Compare", docs_url=None, redoc_url=None)

# Scan results are far too big for a cookie, so they live here keyed by session
# id. This is intentionally in-process: run one worker, or swap in Redis if you
# ever need more.
SCANS: Dict[str, Dict] = {}


def _prune_scans() -> None:
    cutoff = time.time() - settings.session_ttl_seconds
    for sid in [k for k, v in SCANS.items() if v.get("at", 0) < cutoff]:
        SCANS.pop(sid, None)


def read_session(request: Request) -> Optional[Dict]:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        return serializer.loads(raw)
    except BadSignature:
        return None


def write_session(response, data: Dict) -> None:
    response.set_cookie(
        COOKIE_NAME,
        serializer.dumps(data),
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        max_age=settings.session_ttl_seconds,
        path=COOKIE_PATH,
    )


def require_session(request: Request) -> Dict:
    session = read_session(request)
    if not session or "tokens" not in session:
        raise HTTPException(status_code=401, detail="Sign in with Spotify first.")
    return session


def client_for(session: Dict) -> SpotifyClient:
    tokens = Tokens.from_dict(session["tokens"])
    return SpotifyClient(tokens, settings.client_id, settings.client_secret)


# --- Pages -------------------------------------------------------------------


@lru_cache(maxsize=1)
def _index_html() -> str:
    """index.html with the deploy's base path baked into its <base> tag.

    Every other URL in the page and in app.js is relative, so setting this one
    tag is what makes the whole frontend work at "/" and at "/sfconcert" alike.
    """
    raw = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return raw.replace("__BASE__", BASE)


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    return HTMLResponse(_index_html())


@app.get("/healthz", include_in_schema=False)
async def healthz() -> Dict[str, object]:
    snapshot = list_cache.peek()
    return {
        "ok": True,
        "spotify_configured": settings.spotify_configured,
        "list_cached": snapshot is not None,
        "list_shows": len(snapshot.shows) if snapshot else 0,
    }


# --- Auth --------------------------------------------------------------------


@app.get("/login", include_in_schema=False)
async def login(request: Request) -> RedirectResponse:
    if not settings.spotify_configured:
        raise HTTPException(
            status_code=500,
            detail="Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET first.",
        )
    state = secrets.token_urlsafe(16)
    url = build_auth_url(settings.client_id, settings.redirect_uri, state)
    response = RedirectResponse(url, status_code=302)
    session = read_session(request) or {}
    session["state"] = state
    write_session(response, session)
    return response


@app.get("/callback", include_in_schema=False)
async def callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    home = f"{BASE}/"
    if error:
        return RedirectResponse(f"{home}?error={error}", status_code=302)
    session = read_session(request) or {}
    if not code or not state or state != session.get("state"):
        return RedirectResponse(f"{home}?error=state_mismatch", status_code=302)

    try:
        tokens = await exchange_code(
            code, settings.client_id, settings.client_secret, settings.redirect_uri
        )
    except SpotifyError as exc:
        return RedirectResponse(f"{home}?error={exc}", status_code=302)

    sid = session.get("sid") or secrets.token_urlsafe(16)
    response = RedirectResponse(home, status_code=302)
    write_session(response, {"sid": sid, "tokens": tokens.to_dict()})
    return response


@app.post("/api/logout")
async def logout(request: Request) -> JSONResponse:
    session = read_session(request) or {}
    SCANS.pop(session.get("sid", ""), None)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return response


# --- API ---------------------------------------------------------------------


@app.get("/api/config")
async def api_config(request: Request) -> Dict[str, object]:
    session = read_session(request) or {}
    return {
        "spotify_configured": settings.spotify_configured,
        "lastfm_configured": settings.lastfm_configured,
        "signed_in": "tokens" in session,
        "list_url": settings.list_url,
        "adjacency_cap": settings.adjacency_candidate_cap,
    }


@app.get("/api/me")
async def api_me(request: Request) -> Dict[str, object]:
    session = require_session(request)
    async with client_for(session) as client:
        try:
            profile = await client.me()
        except SpotifyError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
    images = profile.get("images") or []
    return {
        "id": profile.get("id"),
        "name": profile.get("display_name") or profile.get("id"),
        "image": images[0]["url"] if images else "",
        "url": (profile.get("external_urls") or {}).get("spotify", ""),
    }


@app.get("/api/list")
async def api_list(refresh: bool = False) -> Dict[str, object]:
    snapshot = await list_cache.get(force=refresh)
    dates = sorted({s.date for s in snapshot.shows if s.date})
    return {
        "shows": len(snapshot.shows),
        "bands": len(snapshot.bands),
        "pages": snapshot.pages,
        "updated_label": snapshot.updated_label,
        "source": snapshot.source,
        "fetched_at": snapshot.fetched_at,
        "first_date": dates[0] if dates else "",
        "last_date": dates[-1] if dates else "",
    }


def _sse(event: str, payload: Dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


@app.get("/api/scan")
async def api_scan(
    request: Request,
    include_playlists: bool = Query(True),
) -> StreamingResponse:
    """Stream the library scan, then the finished comparison, over SSE."""
    session = require_session(request)
    sid = session.get("sid") or secrets.token_urlsafe(16)

    async def stream():
        try:
            yield _sse("progress", {"stage": "list", "detail": "Fetching The List"})
            snapshot = await list_cache.get()
            yield _sse(
                "progress",
                {
                    "stage": "list",
                    "detail": f"{len(snapshot.shows)} shows, {len(snapshot.bands)} bands",
                },
            )

            async with client_for(session) as client:
                artists: List[Artist] = []
                async for progress, final in scan_library(
                    client,
                    include_playlists=include_playlists,
                    max_playlists=settings.max_playlists,
                ):
                    yield _sse(
                        "progress",
                        {
                            "stage": progress.stage,
                            "detail": progress.detail,
                            "artists": progress.artists,
                        },
                    )
                    if final is not None:
                        artists = final
                    await asyncio.sleep(0)

                # Fold in any streaming-history upload from this session.
                stored = SCANS.get(sid, {})
                history: List[Artist] = stored.get("history", [])
                if history:
                    yield _sse(
                        "progress",
                        {
                            "stage": "history",
                            "detail": f"Merging {len(history)} artists from your export",
                        },
                    )
                    artists = artists + history

                merged = list(index_artists(artists).values())
                yield _sse(
                    "progress",
                    {
                        "stage": "matching",
                        "detail": f"Comparing {len(merged)} artists against The List",
                    },
                )

                matches = find_matches(snapshot.shows, merged)

                # Only the artists that actually matched need genre lookups.
                matched_names = {m.artist for m in matches}
                to_enrich = [a for a in merged if a.name in matched_names]
                try:
                    await enrich_genres(client, to_enrich)
                except SpotifyError:
                    pass
                # Re-run so the matches carry the genres we just fetched.
                matches = find_matches(snapshot.shows, merged)

            _prune_scans()
            SCANS[sid] = {
                "at": time.time(),
                "artists": merged,
                "shows": snapshot.shows,
                "history": history,
            }

            yield _sse(
                "result",
                {
                    "matches": [m.as_dict() for m in matches],
                    "artist_count": len(merged),
                    "show_count": len(snapshot.shows),
                    "band_count": len(snapshot.bands),
                    "updated_label": snapshot.updated_label,
                    "history_artists": len(history),
                },
            )
        except HTTPException as exc:
            yield _sse("error", {"detail": exc.detail})
        except SpotifyError as exc:
            yield _sse("error", {"detail": str(exc)})
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            yield _sse("error", {"detail": f"{type(exc).__name__}: {exc}"})

    response = StreamingResponse(stream(), media_type="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    write_session(response, {**session, "sid": sid})
    return response


@app.get("/api/adjacent")
async def api_adjacent(
    request: Request,
    provider: str = Query("auto", pattern="^(auto|genre|lastfm)$"),
    days: int = Query(60, ge=1, le=400),
    limit: int = Query(120, ge=1, le=500),
) -> StreamingResponse:
    """Stream the optional adjacent-artist pass.

    Requires a completed scan in this session, since it works off the artists
    we found and the bands they did *not* match.
    """
    session = require_session(request)
    sid = session.get("sid", "")
    stored = SCANS.get(sid)

    async def stream():
        if not stored:
            yield _sse("error", {"detail": "Run a scan first."})
            return
        try:
            artists: List[Artist] = stored["artists"]
            shows = stored["shows"]

            cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() + days * 86400))
            today = time.strftime("%Y-%m-%d")
            window = [s for s in shows if today <= s.date <= cutoff]

            candidates = unmatched_bands(window, artists)
            chosen = provider
            if chosen == "auto":
                chosen = "lastfm" if settings.lastfm_configured else "genre"

            if chosen == "genre":
                candidates = candidates[: settings.adjacency_candidate_cap]

            yield _sse(
                "progress",
                {
                    "stage": "adjacent",
                    "detail": f"Checking {len(candidates)} bands via {chosen}",
                    "provider": chosen,
                },
            )

            updates: List[tuple] = []

            def note(done: int, total: int) -> None:
                updates.append((done, total))

            if chosen == "lastfm":
                if not settings.lastfm_configured:
                    yield _sse("error", {"detail": "LASTFM_API_KEY is not set."})
                    return
                task = asyncio.create_task(
                    adjacent_by_lastfm(
                        settings.lastfm_api_key, candidates, artists, progress=note
                    )
                )
            else:
                client = client_for(session)
                async def run_genre():
                    async with client as c:
                        return await adjacent_by_genre(
                            c, candidates, artists, progress=note
                        )

                task = asyncio.create_task(run_genre())

            while not task.done():
                await asyncio.sleep(0.4)
                if updates:
                    done, total = updates[-1]
                    updates.clear()
                    yield _sse(
                        "progress",
                        {
                            "stage": "adjacent",
                            "detail": f"Checked {done}/{total}",
                            "provider": chosen,
                        },
                    )

            found = await task
            found = found[:limit]

            # Attach the upcoming shows for each adjacent band.
            by_band = {}
            for adjacent in found:
                by_band[adjacent.band] = []
            for show in window:
                if show.band in by_band:
                    by_band[show.band].append(
                        {
                            "venue": show.venue,
                            "date": show.date,
                            "weekday": show.weekday,
                            "age": show.age,
                            "price": show.price,
                            "doors": show.doors,
                            "sold_out": show.sold_out,
                        }
                    )

            payload = []
            for adjacent in found:
                item = adjacent.as_dict()
                item["shows"] = sorted(by_band.get(adjacent.band, []), key=lambda s: s["date"])
                if item["shows"]:
                    payload.append(item)

            yield _sse(
                "result",
                {"adjacent": payload, "provider": chosen, "checked": len(candidates)},
            )
        except SpotifyError as exc:
            yield _sse("error", {"detail": str(exc)})
        except Exception as exc:  # noqa: BLE001
            yield _sse("error", {"detail": f"{type(exc).__name__}: {exc}"})

    response = StreamingResponse(stream(), media_type="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.post("/api/history")
async def api_history(
    request: Request, files: List[UploadFile] = File(...)
) -> Dict[str, object]:
    """Accept a Spotify data export for true lifetime listening coverage.

    The files never leave this process -- they are parsed to artist names and
    play counts, and the raw JSON is discarded.
    """
    session = require_session(request)
    sid = session.get("sid") or secrets.token_urlsafe(16)

    blobs = []
    for upload in files:
        blobs.append(await upload.read())
    artists = parse_streaming_history(blobs)

    _prune_scans()
    entry = SCANS.setdefault(sid, {"at": time.time()})
    entry["at"] = time.time()
    entry["history"] = artists

    response = JSONResponse(
        {
            "artists": len(artists),
            "files": len(blobs),
            "top": [
                {"name": a.name, "plays": a.play_count}
                for a in sorted(artists, key=lambda a: a.play_count, reverse=True)[:5]
            ],
        }
    )
    write_session(response, {**session, "sid": sid})
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
