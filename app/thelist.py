"""Scraper for Steve Koepke's Bay Area concert guide, a.k.a. The List.

The canonical HTML mirror lives at http://www.foopee.com/punk/the-list/ and is
generated from Steve's flat file, so the markup is machine-regular but not
well-formed: ``<LI>`` elements are never closed. Rather than fight a tree
parser over that, we split the raw HTML on ``<LI>`` boundaries and classify
each chunk, which is both simpler and far more tolerant of the nesting.

A by-date page looks like this::

    <H2>Aug 10 - Aug 16</H2>
    <UL>
    <LI><A NAME="aug_16"><B>Sun Aug 16</B></A><UL>
    <LI><B><A HREF="by-club.0.html#...">924 Gilman Street, Berkeley</A></B>
        <A HREF="by-band.1.html#...">Ink And Dagger</A>,
        <A HREF="by-band.2.html#...">Le Shok</A> a/a $25 5:30pm/6pm
    ...
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence

import httpx
from bs4 import BeautifulSoup

BASE_URL = "http://www.foopee.com/punk/the-list/"
USER_AGENT = (
    "sf-concert-compare/2.0 (+https://github.com/jbondar/sf_concert_compare) "
    "python-httpx"
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# <A NAME="aug_16"> anchors mark the start of a day's listings.
_DATE_ANCHOR = re.compile(r'NAME="([a-z]{3})_(\d{1,2})"', re.IGNORECASE)
_UPDATED = re.compile(r"updated\s+(\d{1,2})/(\d{1,2})/(\d{4})", re.IGNORECASE)
_LI_SPLIT = re.compile(r"<LI>", re.IGNORECASE)

# Trailing metadata on a show line: age policy, price, times, and the symbol
# legend documented on the index page.
# No trailing \b: "21+" ends on a non-word char, so a boundary never matches
# against the following space.
_AGE = re.compile(r"(?<!\w)(a/a|21\+|18\+|16\+)", re.IGNORECASE)
_PRICE = re.compile(r"(\$[\d.]+(?:/\$[\d.]+)*|\bfree\b)", re.IGNORECASE)
_TIME = re.compile(r"\b(\d{1,2}(?::\d{2})?\s?[ap]m|noon|midnight)\b", re.IGNORECASE)


@dataclass
class Show:
    """One band playing one venue on one night."""

    band: str
    venue: str
    date: str  # ISO yyyy-mm-dd
    weekday: str = ""
    age: str = ""
    price: str = ""
    doors: str = ""
    notes: str = ""
    sold_out: bool = False
    recommended: bool = False
    url: str = BASE_URL

    def as_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ListSnapshot:
    """Everything we scraped in one pass, plus provenance for the UI."""

    shows: List[Show] = field(default_factory=list)
    fetched_at: float = field(default_factory=time.time)
    updated_label: str = ""
    source: str = BASE_URL
    pages: int = 0

    @property
    def bands(self) -> List[str]:
        seen: Dict[str, str] = {}
        for show in self.shows:
            seen.setdefault(show.band.lower(), show.band)
        return sorted(seen.values(), key=str.lower)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _page_urls(index_html: str) -> List[str]:
    """Pull the by-date page links off the index, in listing order."""
    soup = BeautifulSoup(index_html, "html.parser")
    seen: List[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if href.startswith("by-date.") and href not in seen:
            seen.append(href)
    # The index lists them in chronological order already, but sort on the
    # numeric suffix so we do not depend on that.
    return sorted(seen, key=lambda h: int(re.search(r"by-date\.(\d+)", h).group(1)))


def _start_year(index_html: str) -> int:
    """The index title carries the generation date, e.g. 'updated 8/16/2026'."""
    match = _UPDATED.search(index_html)
    if match:
        return int(match.group(3))
    return date.today().year


def _split_details(text: str) -> Dict[str, object]:
    """Tease apart the free-form tail of a show line."""
    raw = _clean(text.lstrip(", "))
    details: Dict[str, object] = {
        "age": "", "price": "", "doors": "", "notes": "",
        "sold_out": "sold out" in raw.lower(),
        # '*' flags a show Steve recommends; see the legend on the index page.
        "recommended": "*" in raw,
    }

    age = _AGE.search(raw)
    if age:
        details["age"] = age.group(1).lower()

    price = _PRICE.search(raw)
    if price:
        details["price"] = price.group(1)

    times = _TIME.findall(raw)
    if times:
        # Listings give "doors/show"; the last time is usually showtime.
        details["doors"] = times[-1].lower().replace(" ", "")

    # Parenthetical asides are the closest thing the format has to a note.
    notes = re.findall(r"\(([^)]*)\)", raw)
    if notes:
        details["notes"] = "; ".join(_clean(n) for n in notes if _clean(n))

    return details


def _parse_page(html: str, year_hint: int, last_month: Optional[int]) -> tuple:
    """Parse one by-date page.

    Returns ``(shows, year, last_month)`` so the caller can carry the rolling
    year across pages -- the source never states a year anywhere.
    """
    shows: List[Show] = []
    year = year_hint
    current_iso = ""
    current_weekday = ""

    for chunk in _LI_SPLIT.split(html):
        date_match = _DATE_ANCHOR.search(chunk)
        if date_match:
            month = _MONTHS.get(date_match.group(1).lower())
            day = int(date_match.group(2))
            if not month:
                continue
            # The List runs forward from "now" and never states a year, so a
            # month going backwards means we crossed into January.
            if last_month is not None and month < last_month:
                year += 1
            last_month = month
            try:
                current_iso = date(year, month, day).isoformat()
            except ValueError:  # e.g. a malformed Feb 30
                current_iso = ""
            label = BeautifulSoup(chunk, "html.parser").get_text(" ", strip=True)
            current_weekday = label.split()[0] if label else ""
            continue

        if "by-club" not in chunk or not current_iso:
            continue

        soup = BeautifulSoup(chunk, "html.parser")
        anchors = soup.find_all("a", href=True)
        venue = ""
        bands: List[str] = []
        for anchor in anchors:
            text = _clean(anchor.get_text(" ", strip=True))
            if not text:
                continue
            if "by-club" in anchor["href"] and not venue:
                venue = text
            elif "by-band" in anchor["href"]:
                bands.append(text)

        if not bands:
            continue

        # Everything after the final anchor is the price/age/time tail.
        tail = ""
        if anchors:
            tail = "".join(
                str(sibling) for sibling in anchors[-1].next_siblings
            )
        details = _split_details(BeautifulSoup(tail, "html.parser").get_text(" "))

        for band in bands:
            shows.append(
                Show(
                    band=band,
                    venue=venue,
                    date=current_iso,
                    weekday=current_weekday,
                    url=BASE_URL,
                    **details,  # type: ignore[arg-type]
                )
            )

    return shows, year, last_month


async def _get(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    response.raise_for_status()
    # The pages are ancient and served without a charset; latin-1 is the
    # safest superset for the stray curly quotes that show up in band names.
    return response.content.decode("utf-8", errors="replace")


async def fetch_list(
    base_url: str = BASE_URL,
    *,
    max_pages: Optional[int] = None,
    timeout: float = 20.0,
) -> ListSnapshot:
    """Fetch and parse every by-date page of The List."""
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, follow_redirects=True
    ) as client:
        index_html = await _get(client, base_url)
        hrefs = _page_urls(index_html)
        if max_pages is not None:
            hrefs = hrefs[:max_pages]

        year = _start_year(index_html)
        updated = _UPDATED.search(index_html)
        updated_label = updated.group(0) if updated else ""

        # Fetch concurrently but parse in order, since the year inference
        # depends on walking the weeks forward.
        pages = await asyncio.gather(
            *(_get(client, base_url.rstrip("/") + "/" + href) for href in hrefs)
        )

    shows: List[Show] = []
    last_month: Optional[int] = None
    for page in pages:
        page_shows, year, last_month = _parse_page(page, year, last_month)
        shows.extend(page_shows)

    return ListSnapshot(
        shows=shows,
        updated_label=updated_label,
        source=base_url,
        pages=len(hrefs),
    )


def parse_pages(index_html: str, page_htmls: Sequence[str]) -> ListSnapshot:
    """Pure-parse entry point, used by the tests against saved fixtures."""
    year = _start_year(index_html)
    last_month: Optional[int] = None
    shows: List[Show] = []
    for page in page_htmls:
        page_shows, year, last_month = _parse_page(page, year, last_month)
        shows.extend(page_shows)
    updated = _UPDATED.search(index_html)
    return ListSnapshot(
        shows=shows,
        updated_label=updated.group(0) if updated else "",
        pages=len(page_htmls),
    )


class ListCache:
    """In-process TTL cache so we hit foopee once, not once per user."""

    def __init__(self, ttl_seconds: int = 6 * 60 * 60, base_url: str = BASE_URL):
        self.ttl = ttl_seconds
        self.base_url = base_url
        self._snapshot: Optional[ListSnapshot] = None
        self._lock = asyncio.Lock()

    def peek(self) -> Optional[ListSnapshot]:
        return self._snapshot

    async def get(self, force: bool = False) -> ListSnapshot:
        async with self._lock:
            fresh = (
                self._snapshot is not None
                and time.time() - self._snapshot.fetched_at < self.ttl
            )
            if fresh and not force:
                return self._snapshot  # type: ignore[return-value]
            self._snapshot = await fetch_list(self.base_url)
            return self._snapshot
