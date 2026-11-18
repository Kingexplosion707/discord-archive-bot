"""Live progress from people's Stremio accounts: where they are in an episode or movie, and when they finish.

Stremio syncs each library item's position (timeOffset) and length (duration) to its account
servers. With someone's own sign-in key (never their password), the bot reads that and updates
their "is watching" post.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiohttp
import discord

from .meta import split_video_id

if TYPE_CHECKING:
    from .discord_app import ArchiveClient
    from .tracker import Tracker

log = logging.getLogger(__name__)

STREMIO_API = "https://api.strem.io"
_TIMEOUT = aiohttp.ClientTimeout(total=20)
ACTIVE_WINDOW = 15 * 60       # Watched this recently counts as watching now.
PAUSED_AFTER = 3 * 60         # No position change for this long reads as paused.
FINISHED_AT = 0.9             # 90% through counts as finished.
MIN_EDIT_GAP = 15.0


class StremioError(Exception):
    pass


def clock(ms: int | None) -> str:
    seconds = max(0, int((ms or 0) / 1000))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def progress_bar(offset: int, duration: int, width: int = 14) -> str:
    share = min(1.0, offset / duration) if duration else 0.0
    filled = round(share * width)
    return "▰" * filled + "▱" * (width - filled)


def _parse_time(raw: Any) -> float:
    if not isinstance(raw, str) or not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def new_library_item(item_id: str, name: str, kind: str, poster: str | None, stamp: str) -> dict[str, Any]:
    """A library entry in the shape Stremio's own apps save."""
    return {
        "_id": item_id, "name": name, "type": kind, "poster": poster, "posterShape": "poster",
        "removed": False, "temp": False, "_ctime": stamp, "_mtime": stamp,
        "state": {
            "lastWatched": None, "timeWatched": 0, "timeOffset": 0, "overallTimeWatched": 0,
            "timesWatched": 0, "flaggedWatched": 0, "duration": 0, "video_id": None,
            "watched": None, "noNotif": False,
        },
        "behaviorHints": {
            "defaultVideoId": item_id if kind == "movie" else None,
            "featuredVideoId": None, "hasScheduledVideos": False,
        },
    }


class StremioProgress:
    def __init__(self, client: "ArchiveClient", tracker: "Tracker", api: str = STREMIO_API) -> None:
        self.client = client
        self.tracker = tracker
        self.store = tracker.store
        self.api = api.rstrip("/")
        self._last_edit: dict[tuple[int, str], float] = {}
        # Last position seen per (user, library item), to spot playback even if lastWatched lags.
        self._seen: dict[tuple[int, str], int] = {}
        self._moved_at: dict[tuple[int, str], float] = {}
        # Last check per user: (time, problem or None, what they're watching or None).
        self.last_check: dict[int, tuple[float, str | None, str | None]] = {}

    async def _call(self, method: str, body: dict[str, Any]) -> Any:
        try:
            async with self.client.web.post(f"{self.api}/api/{method}", json=body, timeout=_TIMEOUT) as response:
                data = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise StremioError(f"Couldn't reach Stremio ({exc})") from exc
        if not isinstance(data, dict):
            raise StremioError("Stremio sent an unexpected reply")
        if data.get("error"):
            error = data["error"]
            raise StremioError(error.get("message", str(error)) if isinstance(error, dict) else str(error))
        return data.get("result")

    # ---- accounts --------------------------------------------------------------

    async def link(self, user_id: int, email: str, password: str) -> None:
        """Signs in once and keeps only the sign-in key, encrypted. The password isn't stored."""
        result = await self._call(
            "login", {"type": "Login", "email": email.strip(), "password": password, "facebook": False}
        )
        auth_key = (result or {}).get("authKey")
        if not auth_key:
            raise StremioError("Stremio didn't accept that email and password")
        await self.store.execute(
            "INSERT INTO stremio_account (user_id, auth_key, email, added_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET auth_key = excluded.auth_key, email = excluded.email, "
            "added_at = excluded.added_at",
            (user_id, self.client.vault.seal(auth_key.encode()), email.strip(), time.time()),
        )

    async def unlink(self, user_id: int) -> bool:
        return bool(await self.store.execute("DELETE FROM stremio_account WHERE user_id = ?", (user_id,)))

    async def is_linked(self, user_id: int) -> bool:
        return bool(await self.store.fetch_one("SELECT 1 FROM stremio_account WHERE user_id = ?", (user_id,)))

    # ---- polling -----------------------------------------------------------------

    async def _auth_key(self, user_id: int) -> str | None:
        row = await self.store.fetch_one("SELECT auth_key FROM stremio_account WHERE user_id = ?", (user_id,))
        return self.client.vault.unseal(row["auth_key"]).decode() if row else None

    async def poll_all(self) -> None:
        for row in await self.store.fetch_all("SELECT user_id, auth_key FROM stremio_account"):
            user_id = row["user_id"]
            try:
                auth_key = self.client.vault.unseal(row["auth_key"]).decode()
                watching = await self.poll_user(user_id, auth_key)
                self.last_check[user_id] = (time.time(), None, watching)
            except StremioError as exc:
                self.last_check[user_id] = (time.time(), str(exc), None)
                log.info("Stremio progress for %s: %s", user_id, exc)
            except Exception as exc:
                self.last_check[user_id] = (time.time(), f"unexpected problem: {exc}", None)
                log.exception("Stremio progress failed for %s", user_id)

    async def library_items(self, auth_key: str, ids: list[str] | None = None) -> list[dict[str, Any]]:
        items = await self._call(
            "datastoreGet",
            {"authKey": auth_key, "collection": "libraryItem", "ids": ids or [], "all": not ids},
        )
        return [item for item in items or [] if isinstance(item, dict)]

    async def poll_user(self, user_id: int, auth_key: str) -> str | None:
        """Updates posts for whatever this person is watching. Returns its name, if anything."""
        items = await self.library_items(auth_key)
        now = time.time()
        watching = None
        for item in items:
            state = item.get("state") or {}
            kind = "series" if item.get("type") == "series" else "movie"
            video_id = state.get("video_id") or (item.get("_id") if kind == "movie" else None)
            if not video_id:
                continue
            offset = int(state.get("timeOffset") or 0)
            duration = int(state.get("duration") or 0)
            key = (user_id, str(item.get("_id")))
            before = self._seen.get(key)
            self._seen[key] = offset
            if before is not None and offset != before:
                self._moved_at[key] = now
            recent = now - max(_parse_time(state.get("lastWatched")), self._moved_at.get(key, 0.0)) <= ACTIVE_WINDOW
            if offset <= 0 or not recent:
                continue
            watching = str(item.get("name") or video_id)
            await self.tracker.record_play(user_id, kind, video_id)
            finished = bool(duration and offset >= duration * FINISHED_AT)
            await self.update_post(user_id, kind, video_id, offset, duration, finished)
        return watching

    # ---- Stremio library ----------------------------------------------------------

    async def mirror_to_library(self, user_id: int, kind: str, title_id: str, *, add: bool) -> bool:
        """Adds a title to (or removes it from) the person's Stremio library, if they're connected."""
        try:
            auth_key = await self._auth_key(user_id)
            if auth_key is None:
                return False
            existing = await self.library_items(auth_key, [title_id])
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            if existing:
                item = existing[0]
                if bool(item.get("removed")) == (not add) and not item.get("temp"):
                    return True
                item.update({"removed": not add, "temp": False, "_mtime": stamp})
            elif add:
                title = await self.tracker.meta.title(kind, title_id)
                if title is None:
                    return False
                item = new_library_item(title.id, title.name, title.type, title.poster, stamp)
            else:
                return True
            await self._call("datastorePut", {"authKey": auth_key, "collection": "libraryItem", "changes": [item]})
            return True
        except StremioError as exc:
            log.info("Couldn't update Stremio library for %s: %s", user_id, exc)
            return False

    async def library(self, user_id: int) -> list[dict[str, Any]] | None:
        auth_key = await self._auth_key(user_id)
        if auth_key is None:
            return None
        items = [i for i in await self.library_items(auth_key) if not i.get("removed") and not i.get("temp")]
        items.sort(key=lambda i: max(_parse_time((i.get("state") or {}).get("lastWatched")),
                                     _parse_time(i.get("_mtime"))), reverse=True)
        return items

    async def update_post(
        self, user_id: int, kind: str, video_id: str, offset: int, duration: int, finished: bool
    ) -> None:
        post = await self.store.fetch_one(
            "SELECT * FROM watch_post WHERE user_id = ? AND video_id = ?", (user_id, video_id)
        )
        if post is None:
            return  # Not posted (private, or no tracker channel).
        if post["state"] == "finished":
            return
        now = time.time()
        moved = post["offset_ms"] is None or abs(offset - post["offset_ms"]) >= 1000
        if finished:
            state = "finished"
        elif moved:
            state = "watching"
        elif now - post["updated_at"] > PAUSED_AFTER:
            state = "paused"
        else:
            state = post["state"]
        key = (user_id, video_id)
        if state == post["state"] and not moved:
            return
        if state == post["state"] and now - self._last_edit.get(key, 0) < MIN_EDIT_GAP:
            return
        self._last_edit[key] = now
        await self.store.execute(
            "UPDATE watch_post SET offset_ms = ?, duration_ms = ?, state = ?, updated_at = ? "
            "WHERE user_id = ? AND video_id = ?",
            (offset, duration, state, now if moved or state != post["state"] else post["updated_at"],
             user_id, video_id),
        )
        await self._edit(post, user_id, kind, video_id, offset, duration, state)

    async def _edit(
        self, post: Any, user_id: int, kind: str, video_id: str, offset: int, duration: int, state: str
    ) -> None:
        channel = self.client.get_channel(post["channel_id"])
        if not isinstance(channel, discord.abc.Messageable):
            return
        title_id, season, number = split_video_id(video_id)
        title = await self.tracker.meta.title(kind, title_id)
        if title is None:
            return
        embed = await self.tracker.watch_embed(user_id, title, season, number)
        name = self.tracker.member_name(user_id)
        verb = {"watching": "is watching", "paused": "paused", "finished": "finished"}[state]
        embed.set_author(name=f"{name} {verb}", icon_url=self.tracker.member_avatar(user_id))
        if state == "finished":
            value = f"✅ Finished · {clock(duration)}"
            embed.color = discord.Color.green()
        else:
            icon = "⏸️" if state == "paused" else "▶️"
            value = f"{icon} {progress_bar(offset, duration)} `{clock(offset)} / {clock(duration)}`"
            if duration:
                value += f" · {round(100 * offset / duration)}%"
        embed.insert_field_at(0, name="Progress", value=value, inline=False)
        try:
            message = channel.get_partial_message(post["message_id"])  # type: ignore[union-attr]
            await message.edit(embed=embed)
        except discord.HTTPException as exc:
            log.info("Couldn't update watch post: %s", exc)
