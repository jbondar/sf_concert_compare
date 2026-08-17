# SF Concert Compare

Sign in with Spotify, and find out which of the bands you actually listen to are
playing the Bay Area.

Show data comes from [**The List**](http://www.foopee.com/punk/the-list/) — Steve
Koepke's Bay Area concert guide, running since the mid-90s and still updated by
hand. It currently carries around 2,900 shows across ~2,500 bands, four months
out. This tool scrapes it, reads the artists in your Spotify account, and shows
you the overlap.

> **Heads up if you used v1:** the old `spotify_and_list_compare.py` had a
> hardcoded client secret and a committed `.cache` token file. Both are gone, but
> they are still in the git history — see [Rotating the old credentials](#rotating-the-old-credentials).

---

## What it does

**Direct matches** — bands on The List that are already in your Spotify. It
unions every surface Spotify exposes: top artists across all three time ranges,
artists you follow, saved tracks, saved albums, recently played, and the tracks
in your playlists. Each match is tagged with where it came from, so you can see
*why* it matched.

**Adjacent artists** *(optional, off by default)* — bands you have **never**
listened to that sit close to your taste. This is a separate panel with its own
styling and its own run button, and every row is labelled with the reason it
appeared. It is a guess, and the UI never lets it look like anything else.

**Your full listening history** *(optional)* — Spotify's API cannot return
everything you have ever played; `recently-played` caps at 50 items and top
artists cap at 50 per range. For true lifetime coverage, request your
[data export](https://www.spotify.com/account/privacy/) (Account → Privacy →
Extended streaming history, takes a few days to arrive) and drop the
`Streaming_History_*.json` files into the app. They are parsed in memory and
discarded — nothing is written to disk.

---

## Quick start

### 1. Make a Spotify app

Go to the [Spotify developer dashboard](https://developer.spotify.com/dashboard),
create an app, and add this **exact** redirect URI:

```
http://127.0.0.1:8000/callback
```

Copy the client ID and secret.

> Spotify rejects `localhost` in redirect URIs — it must be `127.0.0.1`.

### 2. Configure

```bash
cp .env.example .env
# fill in SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET
```

### 3. Run it

**With Docker** (nothing to install but Docker):

```bash
docker compose up --build
```

**Without Docker:**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

`.env` is picked up automatically. Real environment variables take precedence,
so exported values and Docker's `env_file` still win.

Either way, open <http://127.0.0.1:8000>.

---

## Configuration

Everything is environment variables; see [`.env.example`](.env.example).

| Variable | Required | Default | Notes |
|---|---|---|---|
| `SPOTIFY_CLIENT_ID` | yes | — | From the developer dashboard |
| `SPOTIFY_CLIENT_SECRET` | yes | — | Never commit this |
| `SPOTIFY_REDIRECT_URI` | yes | `http://127.0.0.1:8000/callback` | Must match the dashboard exactly |
| `SESSION_SECRET` | recommended | random per boot | Set it, or restarts sign everyone out |
| `LASTFM_API_KEY` | no | — | Better adjacent-artist results, [free key](https://www.last.fm/api/account/create) |
| `LIST_URL` | no | foopee.com | Point at a mirror if you have one |
| `LIST_TTL_SECONDS` | no | `21600` | The List is scraped once per this window, for all users |
| `MAX_PLAYLISTS` | no | `60` | Playlist scanning is the slow part |
| `ADJACENCY_CANDIDATE_CAP` | no | `400` | Caps Spotify searches in genre mode |
| `COOKIE_SECURE` | no | `false` | Set `true` when serving over HTTPS |

---

## How matching works

Matching is deliberately **conservative — exact match only, no fuzzy distance**.
A false positive sends you across the Bay on a Tuesday for a band you have never
heard of, which is a much worse outcome than a missed match.

Before comparing, both sides are normalized: lowercased, accents stripped,
punctuation removed, `&` folded to `and`, a leading `The` dropped, whitespace
collapsed. So `The Locust` ↔ `the locust`, `Sigur Rós` ↔ `Sigur Ros`, and
`Blink-182` ↔ `Blink 182` all match — but `Wire` and `Wires` do not.

The List also mixes non-bands into its band slots (`record swap`, `film
screening of "The Order"`, `TBA`). Those are filtered out by pattern.

### Adjacent artists

Spotify's `/artists/{id}/related-artists` was the obvious way to do this, but
Spotify [cut it off for new API clients on 2024-11-27](https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api),
along with `/recommendations` and audio features. So there are two providers:

- **`genre`** (default, zero config) — looks each candidate band up on Spotify,
  reads its genre tags, and scores the overlap against a genre profile built from
  your own library. Costs one search per candidate, hence the candidate cap.
- **`lastfm`** (better, needs a free key) — asks Last.fm for artists similar to
  *your* top artists, then intersects that with The List. Higher quality, and the
  request count scales with your library rather than the size of The List.

Set `LASTFM_API_KEY` and the app picks `lastfm` automatically.

---

## Command line

The web app owns Spotify sign-in. The CLI covers what needs no auth:

```bash
# Dump upcoming shows
python cli.py shows --days 14

# Compare against a plain list of band names
python cli.py compare --artists my_bands.txt

# Compare against a Spotify data export
python cli.py compare --history ~/my_spotify_data/Streaming_History_*.json

# Machine-readable
python cli.py --json shows --days 7
```

---

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
```

The test suite runs against saved copies of the real foopee pages in
`tests/fixtures/`, so it never hits the network. If The List's HTML ever
changes shape, refresh the fixtures and the tests will tell you what broke:

```bash
curl -o tests/fixtures/index.html      http://www.foopee.com/punk/the-list/
curl -o tests/fixtures/by-date.0.html  http://www.foopee.com/punk/the-list/by-date.0.html
curl -o tests/fixtures/by-date.1.html  http://www.foopee.com/punk/the-list/by-date.1.html
```

### Layout

```
app/
  main.py       FastAPI routes, SSE streaming, session cookies
  spotify.py    OAuth + the library scan
  thelist.py    The List scraper and its TTL cache
  matching.py   Name normalization and the compare
  adjacency.py  The optional adjacent-artist providers
  static/       Frontend (no build step, no dependencies)
cli.py          No-auth command line
```

### A note on scaling

Scan results live in process memory keyed by session, which is why the Docker
image runs a single worker. It is a personal tool and that is fine. If you ever
need more than one worker, move the `SCANS` dict in `app/main.py` to Redis.

---

## Privacy

Tokens live in a signed, HTTP-only session cookie. Scan results and any uploaded
history live in memory and are dropped after `SESSION_TTL_SECONDS` (12h default)
or when you sign out. Nothing is written to disk, and the app has no database.
Scopes requested are all read-only.

---

## Rotating the old credentials

Version 1 of this repo committed a Spotify client secret in
`spotify_and_list_compare.py` and a live access/refresh token in `.cache`. Both
files have been removed, **but they remain in the git history and should be
treated as public.** If you have not already:

1. Go to the [dashboard](https://developer.spotify.com/dashboard), open the app,
   and rotate the client secret. This invalidates the leaked tokens too.
2. Optionally scrub the history with
   [`git filter-repo`](https://github.com/newren/git-filter-repo)
   (`git filter-repo --path .cache --path spotify_and_list_compare.py --invert-paths`)
   and force-push. Rotating is what actually matters; scrubbing is tidiness.

---

## Credits

The List is compiled by **Steve Koepke** and HTMLized by
**[Graham Spencer](http://gspencer.net/)**. Please send corrections about show
data to Steve, not here. This project is not affiliated with either of them, or
with Spotify.

Licensed under GPL-3.0 — see [LICENSE](LICENSE).
