/* SF Concert Compare -- front end.
 *
 * Two independent SSE streams: /api/scan (direct matches) and /api/adjacent
 * (the optional guesses). Results are held in memory here; nothing persists.
 */

const $ = (id) => document.getElementById(id);

const state = {
  matches: [],
  adjacent: [],
  config: null,
};

const SOURCE_LABELS = {
  top: "top artist",
  following: "followed",
  saved: "saved track",
  albums: "saved album",
  recent: "recently played",
  playlist: "in a playlist",
  history: "listening history",
};

// --- helpers ---------------------------------------------------------------

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

function fmtDate(iso) {
  if (!iso) return { day: "", rest: "" };
  const [y, m, d] = iso.split("-").map(Number);
  const date = new Date(y, m - 1, d);
  return {
    day: date.toLocaleDateString(undefined, { weekday: "short" }),
    rest: date.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
  };
}

function dateHeading(iso) {
  const { day, rest } = fmtDate(iso);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const [y, m, d] = iso.split("-").map(Number);
  const when = new Date(y, m - 1, d);
  const diff = Math.round((when - today) / 86400000);
  if (diff === 0) return `Tonight — ${day} ${rest}`;
  if (diff === 1) return `Tomorrow — ${day} ${rest}`;
  return `${day} ${rest}`;
}

function withinDays(iso, days) {
  if (!days) return true;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const [y, m, d] = iso.split("-").map(Number);
  const when = new Date(y, m - 1, d);
  const diff = Math.round((when - today) / 86400000);
  return diff >= 0 && diff <= days;
}

function artFor(item) {
  if (item.image || item.artist_image) {
    return `<img class="show-art" src="${esc(item.image || item.artist_image)}" alt="" loading="lazy" />`;
  }
  return `<div class="show-art placeholder">♪</div>`;
}

/** Group rows by ISO date and render them under sticky date headings. */
function renderGrouped(container, rows, rowRenderer) {
  const groups = new Map();
  for (const row of rows) {
    if (!groups.has(row.date)) groups.set(row.date, []);
    groups.get(row.date).push(row);
  }
  const html = [];
  for (const [date, items] of [...groups.entries()].sort()) {
    html.push(`<div class="date-head">${esc(dateHeading(date))}</div>`);
    for (const item of items) html.push(rowRenderer(item));
  }
  container.innerHTML = html.join("");
}

// --- direct matches --------------------------------------------------------

function matchRow(match) {
  const { day, rest } = fmtDate(match.date);
  const tags = [];
  if (match.recommended) tags.push(`<span class="tag tag-rec">★ recommended</span>`);
  if (match.sold_out) tags.push(`<span class="tag tag-sold">sold out</span>`);
  if (match.age) tags.push(`<span class="tag">${esc(match.age)}</span>`);
  if (match.price) tags.push(`<span class="tag">${esc(match.price)}</span>`);
  for (const source of (match.sources || []).slice(0, 2)) {
    tags.push(`<span class="tag tag-src">${esc(SOURCE_LABELS[source] || source)}</span>`);
  }

  const name = match.artist_url
    ? `<a href="${esc(match.artist_url)}" target="_blank" rel="noopener">${esc(match.band)}</a>`
    : esc(match.band);

  return `
    <div class="show">
      ${artFor(match)}
      <div class="show-body">
        <div class="show-band">${name}</div>
        <div class="show-venue">${esc(match.venue)}</div>
        <div class="show-tags">${tags.join("")}</div>
      </div>
      <div class="show-when"><span class="day">${esc(day)}</span>${esc(rest)}${
        match.doors ? ` · ${esc(match.doors)}` : ""
      }</div>
    </div>`;
}

function applyFilters() {
  const text = $("filter-text").value.trim().toLowerCase();
  const days = Number($("filter-window").value);
  const hideSold = $("filter-soldout").checked;

  const rows = state.matches.filter((m) => {
    if (!withinDays(m.date, days)) return false;
    if (hideSold && m.sold_out) return false;
    if (text) {
      const hay = `${m.band} ${m.venue}`.toLowerCase();
      if (!hay.includes(text)) return false;
    }
    return true;
  });

  $("match-count").textContent = `${rows.length} of ${state.matches.length} shows`;
  const empty = $("match-empty");
  if (!rows.length) {
    $("match-list").innerHTML = "";
    empty.classList.remove("hidden");
    empty.innerHTML = state.matches.length
      ? `<strong>Nothing in this window.</strong>Try widening the date range or clearing the filter.`
      : `<strong>No direct matches.</strong>None of the bands on The List are in your Spotify account right now. Try the adjacent-artists section below.`;
  } else {
    empty.classList.add("hidden");
    renderGrouped($("match-list"), rows, matchRow);
  }
}

function renderStats(result) {
  const bands = new Set(state.matches.map((m) => m.band)).size;
  const soon = state.matches.filter((m) => withinDays(m.date, 30)).length;
  const cards = [
    [bands, "your bands playing"],
    [state.matches.length, "total shows"],
    [soon, "in the next 30 days"],
    [result.artist_count.toLocaleString(), "artists scanned"],
    [result.band_count.toLocaleString(), "bands on The List"],
  ];
  $("stats").innerHTML = cards
    .map(([value, label]) => `
      <div class="stat">
        <div class="stat-value">${esc(value)}</div>
        <div class="stat-label">${esc(label)}</div>
      </div>`)
    .join("");
}

// --- adjacent --------------------------------------------------------------

function adjacentRows(items) {
  // One entry per band, but each band can have several upcoming shows.
  const rows = [];
  for (const item of items) {
    for (const show of item.shows || []) {
      rows.push({ ...item, ...show, _score: item.score });
    }
  }
  return rows.sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : b._score - a._score));
}

function adjacentRow(row) {
  const { day, rest } = fmtDate(row.date);
  const tags = [];
  if (row.because_of?.length) {
    tags.push(`<span class="tag tag-why">like ${esc(row.because_of.slice(0, 2).join(", "))}</span>`);
  }
  if (row.shared_genres?.length) {
    tags.push(`<span class="tag tag-why">${esc(row.shared_genres[0])}</span>`);
  }
  if (row.sold_out) tags.push(`<span class="tag tag-sold">sold out</span>`);
  if (row.age) tags.push(`<span class="tag">${esc(row.age)}</span>`);
  if (row.price) tags.push(`<span class="tag">${esc(row.price)}</span>`);

  const name = row.spotify_url
    ? `<a href="${esc(row.spotify_url)}" target="_blank" rel="noopener">${esc(row.band)}</a>`
    : esc(row.band);

  return `
    <div class="show">
      ${artFor(row)}
      <div class="show-body">
        <div class="show-band">${name}</div>
        <div class="show-venue">${esc(row.venue)}</div>
        <div class="show-tags">${tags.join("")}</div>
      </div>
      <div class="show-when"><span class="day">${esc(day)}</span>${esc(rest)}${
        row.doors ? ` · ${esc(row.doors)}` : ""
      }</div>
    </div>`;
}

// --- SSE plumbing ----------------------------------------------------------

/** Run an SSE request, routing progress/result/error to the given handlers. */
function stream(url, { onProgress, onResult, onError, onDone }) {
  const source = new EventSource(url);
  let settled = false;

  source.addEventListener("progress", (event) => onProgress(JSON.parse(event.data)));
  source.addEventListener("result", (event) => {
    settled = true;
    onResult(JSON.parse(event.data));
    source.close();
    onDone?.();
  });
  source.addEventListener("error", (event) => {
    // A payload means the server reported a problem; no payload means the
    // connection dropped, which after a result is just a normal close.
    if (event.data) {
      settled = true;
      onError(JSON.parse(event.data).detail);
      source.close();
      onDone?.();
    } else if (!settled) {
      settled = true;
      onError("Lost connection to the server.");
      source.close();
      onDone?.();
    }
  });
}

// --- actions ---------------------------------------------------------------

async function runScan() {
  const button = $("btn-scan");
  button.disabled = true;
  $("error").classList.add("hidden");
  $("progress").classList.remove("hidden");
  $("progress-text").textContent = "Starting…";

  const includePlaylists = $("opt-playlists").checked;
  stream(`/api/scan?include_playlists=${includePlaylists}`, {
    onProgress: (data) => {
      const count = data.artists ? ` · ${data.artists.toLocaleString()} artists` : "";
      $("progress-text").textContent = `${data.detail}${count}`;
    },
    onResult: (result) => {
      state.matches = result.matches;
      $("results").classList.remove("hidden");
      renderStats(result);
      applyFilters();
      $("results").scrollIntoView({ behavior: "smooth", block: "start" });
    },
    onError: (detail) => {
      $("error").textContent = detail;
      $("error").classList.remove("hidden");
    },
    onDone: () => {
      button.disabled = false;
      $("progress").classList.add("hidden");
    },
  });
}

function runAdjacent() {
  const button = $("btn-adjacent");
  button.disabled = true;
  $("adj-error").classList.add("hidden");
  $("adj-progress").classList.remove("hidden");
  $("adj-progress-text").textContent = "Starting…";

  const provider = $("adj-provider").value;
  const days = $("adj-days").value;
  stream(`/api/adjacent?provider=${provider}&days=${days}`, {
    onProgress: (data) => {
      $("adj-progress-text").textContent = data.detail;
    },
    onResult: (result) => {
      state.adjacent = result.adjacent;
      const rows = adjacentRows(result.adjacent);
      if (!rows.length) {
        $("adj-list").innerHTML = `<div class="empty"><strong>No adjacent artists found.</strong>Nothing on The List scored close enough to your taste in this window.</div>`;
      } else {
        renderGrouped($("adj-list"), rows, adjacentRow);
      }
      $("adj-progress-text").textContent = "";
    },
    onError: (detail) => {
      $("adj-error").textContent = detail;
      $("adj-error").classList.remove("hidden");
    },
    onDone: () => {
      button.disabled = false;
      $("adj-progress").classList.add("hidden");
    },
  });
}

async function uploadHistory(files) {
  if (!files.length) return;
  const status = $("history-status");
  status.textContent = `Parsing ${files.length} file(s)…`;
  const form = new FormData();
  for (const file of files) form.append("files", file);
  try {
    const response = await fetch("/api/history", { method: "POST", body: form });
    if (!response.ok) throw new Error(await response.text());
    const data = await response.json();
    const top = data.top.map((t) => t.name).join(", ");
    status.textContent = data.artists
      ? `Loaded ${data.artists.toLocaleString()} artists. Most played: ${top}. Run the scan to include them.`
      : "No listening data found in those files.";
  } catch (err) {
    status.textContent = `Could not read those files: ${err.message}`;
  }
}

// --- boot ------------------------------------------------------------------

async function boot() {
  const config = await fetch("/api/config").then((r) => r.json());
  state.config = config;

  if (!config.signed_in) {
    $("landing").classList.remove("hidden");
    if (!config.spotify_configured) {
      $("signin-box").classList.add("hidden");
      $("setup-warning").classList.remove("hidden");
    }
    const error = new URLSearchParams(location.search).get("error");
    if (error) {
      const box = $("setup-warning");
      box.classList.remove("hidden");
      box.innerHTML = `<strong>Sign-in failed.</strong> ${esc(error)}`;
    }
    return;
  }

  $("app").classList.remove("hidden");

  fetch("/api/me")
    .then((r) => r.json())
    .then((me) => {
      $("account").innerHTML = `
        <div class="account">
          ${me.image ? `<img src="${esc(me.image)}" alt="" />` : ""}
          <span>${esc(me.name)}</span>
          <button id="btn-logout">Sign out</button>
        </div>`;
      $("btn-logout").addEventListener("click", async () => {
        await fetch("/api/logout", { method: "POST" });
        location.href = "/";
      });
    })
    .catch(() => {});

  fetch("/api/list")
    .then((r) => r.json())
    .then((list) => {
      const first = fmtDate(list.first_date);
      const last = fmtDate(list.last_date);
      $("list-meta").textContent =
        `${list.shows.toLocaleString()} shows · ${list.bands.toLocaleString()} bands · ${first.rest} – ${last.rest}`;
    })
    .catch(() => {
      $("list-meta").textContent = "Could not reach The List";
    });

  $("btn-scan").addEventListener("click", runScan);
  $("btn-adjacent").addEventListener("click", runAdjacent);
  $("history-files").addEventListener("change", (event) => uploadHistory(event.target.files));
  for (const id of ["filter-text", "filter-window", "filter-soldout"]) {
    $(id).addEventListener("input", applyFilters);
  }
}

boot();
