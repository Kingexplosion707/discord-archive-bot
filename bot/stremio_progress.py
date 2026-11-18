"""Live progress from people's Stremio accounts: where they are in an episode or movie, and when they finish.

Stremio syncs each library item's position (timeOffset) and length (duration) to its account
servers. With someone's own sign-in key (never their password), the bot reads that and updates
their "is watching" post.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
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


class StremioProgress:
    def __init__(self, client: "ArchiveClient", tracker: "Tracker", api: str = STREMIO_API) -> None:
        self.client = client
        self.tracker = tracker
        self.store = tracker.store
        self.api = api.rstrip("/")
        self._last_edit: dict[tuple[int, str], float] = {}

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

    async def poll_all(self) -> None:
        for row in await self.store.fetch_all("SELECT user_id, auth_key FROM stremio_account"):
            try:
                auth_key = self.client.vault.unseal(row["auth_key"]).decode()
                await self.poll_user(row["user_id"], auth_key)
            except StremioError as exc:
                log.info("Stremio progress for %s: %s", row["user_id"], exc)
            except Exception:
                log.exception("Stremio progress failed for %s", row["user_id"])

    async def poll_user(self, user_id: int, auth_key: str) -> None:
        items = await self._call(
            "datastoreGet", {"authKey": auth_key, "collection": "libraryItem", "ids": [], "all": True}
        )
        now = time.time()
        for item in items or []:
            if not isinstance(item, dict) or item.get("removed") and not item.get("temp"):
                continue
            state = item.get("state") or {}
            if now - _parse_time(state.get("lastWatched")) > ACTIVE_WINDOW:
                continue
            kind = "series" if item.get("type") == "series" else "movie"
            video_id = state.get("video_id") or (item.get("_id") if kind == "movie" else None)
            if not video_id:
                continue
            offset = int(state.get("timeOffset") or 0)
            duration = int(state.get("duration") or 0)
            if offset <= 0 and not state.get("flaggedWatched"):
                continue
            await self.tracker.record_play(user_id, kind, video_id)
            finished = bool(duration and offset >= duration * FINISHED_AT)
            await self.update_post(user_id, kind, video_id, offset, duration, finished)

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
