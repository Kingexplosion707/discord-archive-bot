"""Movie and show details (posters, episodes, ratings) from Stremio's own metadata service.

Cinemeta needs no API key. Anime IDs from the Kitsu addon fall back to that addon.
"""
from __future__ import annotations

import asyncio
import json
import logging
import unicodedata
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import aiohttp

from .store import Store

log = logging.getLogger(__name__)

CINEMETA = "https://v3-cinemeta.strem.io"
KITSU = "https://anime-kitsu.strem.fun"
_TIMEOUT = aiohttp.ClientTimeout(total=20)
# The anime service's firewall turns away Python's default user agent.
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DiscordWatchTracker/1.0)"}
_MOVIE_CACHE = 14 * 86400
_SERIES_CACHE = 6 * 3600  # Shows get new episodes, so refresh them more often.


@dataclass(frozen=True)
class Episode:
    id: str
    season: int
    number: int
    name: str
    overview: str
    thumbnail: str | None
    released: datetime | None


@dataclass(frozen=True)
class Title:
    id: str
    type: str
    name: str
    poster: str | None = None
    background: str | None = None
    logo: str | None = None
    description: str = ""
    year: str = ""
    genres: tuple[str, ...] = ()
    imdb_rating: str | None = None
    runtime: str | None = None
    cast: tuple[str, ...] = ()
    trailer: str | None = None
    episodes: tuple[Episode, ...] = field(default_factory=tuple)

    @property
    def url(self) -> str | None:
        return f"https://www.imdb.com/title/{self.id}/" if self.id.startswith("tt") else None

    def episode(self, season: int | None, number: int | None) -> Episode | None:
        if season is None or number is None:
            return None
        return next((e for e in self.episodes if e.season == season and e.number == number), None)

    @property
    def runtime_minutes(self) -> int | None:
        digits = "".join(ch for ch in (self.runtime or "") if ch.isdigit())
        return int(digits) if digits else None


def split_video_id(video_id: str) -> tuple[str, int | None, int | None]:
    """'tt0903747:1:7' -> ('tt0903747', 1, 7). Kitsu IDs look like 'kitsu:123:5' (no season)."""
    parts = video_id.split(":")
    if parts[0] == "kitsu":
        base = ":".join(parts[:2])
        episode = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        return base, (1 if episode is not None else None), episode
    if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
        return parts[0], int(parts[1]), int(parts[2])
    return parts[0], None, None


def _date(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    try:  # Cinemeta dates look like 2008-03-10T05:00:00.000Z, always UTC.
        return datetime.fromisoformat(raw[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _norm(text: str) -> str:
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in plain).split())


def name_match(query: str, name: str) -> float:
    wanted, have = _norm(query), _norm(name)
    if not wanted or not have:
        return 0.0
    if have == wanted:
        return 3.0
    if have.startswith(wanted) or wanted.startswith(have):
        return 2.0
    if wanted in have:
        return 1.0
    return 0.0


def _rating(title: Title) -> float:
    try:
        return float(title.imdb_rating or 0)
    except ValueError:
        return 0.0


def _https(url: Any) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://") else None


def parse_title(raw: dict[str, Any], kind: str) -> Title:
    episodes = []
    for video in raw.get("videos") or []:
        season = video.get("season")
        number = video.get("episode", video.get("number"))
        if not isinstance(season, int) or not isinstance(number, int) or season == 0:
            continue
        episodes.append(Episode(
            id=str(video.get("id", "")),
            season=season,
            number=number,
            name=str(video.get("name") or video.get("title") or f"Episode {number}"),
            overview=str(video.get("overview") or video.get("description") or ""),
            thumbnail=_https(video.get("thumbnail")),
            released=_date(video.get("released") or video.get("firstAired")),
        ))
    trailers = raw.get("trailers") or []
    trailer = None
    if trailers and isinstance(trailers[0], dict) and trailers[0].get("source"):
        trailer = f"https://www.youtube.com/watch?v={trailers[0]['source']}"
    poster = _https(raw.get("poster"))
    if poster and "/poster/small/" in poster:
        poster = poster.replace("/poster/small/", "/poster/medium/")
    return Title(
        id=str(raw.get("id") or raw.get("imdb_id")),
        type=kind,
        name=str(raw.get("name") or "Unknown title"),
        poster=poster,
        background=_https(raw.get("background")),
        logo=_https(raw.get("logo")),
        description=str(raw.get("description") or ""),
        year=str(raw.get("releaseInfo") or raw.get("year") or ""),
        genres=tuple(str(g) for g in (raw.get("genres") or raw.get("genre") or [])),
        imdb_rating=str(raw["imdbRating"]) if raw.get("imdbRating") else None,
        runtime=str(raw["runtime"]) if raw.get("runtime") else None,
        cast=tuple(str(c) for c in (raw.get("cast") or [])[:5]),
        trailer=trailer,
        episodes=tuple(sorted(episodes, key=lambda e: (e.season, e.number))),
    )


class MetaClient:
    def __init__(
        self, session: aiohttp.ClientSession, store: Store, cinemeta: str = CINEMETA, kitsu: str = KITSU
    ) -> None:
        self._session = session
        self._store = store
        self._cinemeta = cinemeta.rstrip("/")
        self._kitsu = kitsu.rstrip("/")

    async def _get_json(self, url: str) -> dict[str, Any] | None:
        try:
            async with self._session.get(url, timeout=_TIMEOUT, headers=_HEADERS) as response:
                if response.status != 200:
                    return None
                return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            log.info("Metadata request failed for %s: %s", url, exc)
            return None

    async def title(self, kind: str, title_id: str, *, fresh: bool = False) -> Title | None:
        kind = "series" if kind == "series" else "movie"
        cached = await self._store.fetch_one(
            "SELECT json, fetched_at FROM meta_cache WHERE id = ?", (f"{kind}:{title_id}",)
        )
        max_age = _SERIES_CACHE if kind == "series" else _MOVIE_CACHE
        if cached and not fresh and time.time() - cached["fetched_at"] < max_age:
            return parse_title(json.loads(cached["json"]), kind)
        base = self._kitsu if title_id.startswith("kitsu:") else self._cinemeta
        data = await self._get_json(f"{base}/meta/{kind}/{quote(title_id, safe=':')}.json")
        raw = (data or {}).get("meta")
        if not raw:
            # Stremio sometimes calls a show a movie or the other way round.
            other = "movie" if kind == "series" else "series"
            data = await self._get_json(f"{base}/meta/{other}/{quote(title_id, safe=':')}.json")
            raw = (data or {}).get("meta")
            if raw:
                kind = other
        if not raw:
            return parse_title(json.loads(cached["json"]), kind) if cached else None
        await self._store.execute(
            "INSERT INTO meta_cache (id, json, fetched_at) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET json = excluded.json, fetched_at = excluded.fetched_at",
            (f"{kind}:{title_id}", json.dumps(raw), time.time()),
        )
        return parse_title(raw, kind)

    async def search(self, query: str, limit: int = 12) -> list[Title]:
        query = query.strip()
        if len(query) < 2:
            return []
        found: list[tuple[int, Title]] = []
        kinds = ("movie", "series")
        replies = await asyncio.gather(*(
            self._get_json(f"{self._cinemeta}/catalog/{kind}/top/search={quote(query)}.json") for kind in kinds
        ))
        for kind, data in zip(kinds, replies):
            metas = [raw for raw in (data or {}).get("metas", []) if raw.get("id")]
            found.extend((rank, parse_title(raw, kind)) for rank, raw in enumerate(metas[:limit]))
        # Closest names first; within the same closeness, keep each list's own order, shows and
        # movies alternating.
        def score(item: tuple[int, Title]) -> tuple[float, int]:
            rank, title = item
            return (name_match(query, title.name), -rank)

        ranked = sorted(found, key=score, reverse=True)
        seen: set[str] = set()
        results = []
        for _, title in ranked:
            if title.id not in seen:
                seen.add(title.id)
                results.append(title)
        return results[:limit]

    async def best_match(self, query: str) -> Title | None:
        """The title someone most likely means when they type a name: close name, well rated."""
        found = await self.search(query, limit=12)
        candidates = [t for t in found if name_match(query, t.name) > 0][:6] or found[:2]
        if not candidates:
            return None
        full = await asyncio.gather(*(self.title(t.type, t.id) for t in candidates))
        titles = [f or t for f, t in zip(full, candidates)]
        def likely(title: Title) -> float:
            # Ratings of obscure entries are unreliable; a real, known title has a poster,
            # a summary and usually a trailer.
            score = name_match(query, title.name) + _rating(title) / 5
            if not title.poster:
                score -= 1.5
            if not title.description:
                score -= 1.0
            if title.trailer:
                score += 0.3
            return score

        return max(titles, key=likely)
