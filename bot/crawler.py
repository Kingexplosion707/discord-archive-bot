"""Walks every channel and thread the bot can read and queues the media it finds."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import discord

from . import media, pins
from .drive_log import channel_label, log_row
from .config import Config
from .store import Store

log = logging.getLogger(__name__)

_SAVE_EVERY = 200
_ROLE_CACHE_SECONDS = 3600
UNREADABLE_KEY = "unreadable_channels"


def attachment_rows(message: discord.Message) -> list[dict[str, Any]]:
    return [
        {
            "id": attachment.id,
            "message_id": message.id,
            "channel_id": message.channel.id,
            "filename": attachment.filename,
            "content_type": attachment.content_type,
            "size": attachment.size,
            "url": attachment.url,
            "author_name": message.author.display_name,
            "author_handle": message.author.name,
        }
        for attachment in message.attachments
        if media.classify(attachment.filename, attachment.content_type) is not None
    ]


@dataclass
class CrawlReport:
    channels_read: int = 0
    messages_read: int = 0
    attachments_found: int = 0
    unreadable: list[str] = field(default_factory=list)


class Crawler:
    def __init__(
        self,
        client: discord.Client,
        config: Config,
        store: Store,
        on_found: Callable[[], None],
    ) -> None:
        self._client = client
        self._config = config
        self._store = store
        self._on_found = on_found
        self._lock = asyncio.Lock()
        self._role_cache: dict[int, tuple[float, bool]] = {}
        # True during a fresh start's first pass: record what's there without uploading it.
        self.baseline = False

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def allowed(self, channel: Any) -> bool:
        """Whether settings permit scanning this channel. Threads follow their parent."""
        skip = self._config.skip_channel_ids
        if channel.id in skip or getattr(channel, "parent_id", None) in skip:
            return False
        is_nsfw = getattr(channel, "is_nsfw", None)
        if is_nsfw is None:
            return False  # Not cached, so its settings are unknown. The next crawl will get it.
        return self._config.scan_nsfw or not is_nsfw()

    async def excluded(self, message: discord.Message) -> bool:
        """Whether the author has the role whose posts shouldn't be saved."""
        guild = message.guild
        wanted = self._config.skip_role_name.casefold()
        if guild is None or not any(role.name.casefold() == wanted for role in guild.roles):
            return False
        cached = self._role_cache.get(message.author.id)
        if cached is not None and time.monotonic() - cached[0] < _ROLE_CACHE_SECONDS:
            return cached[1]
        # History doesn't say what roles an author has, so ask once per person.
        try:
            member = await guild.fetch_member(message.author.id)
            result = any(role.name.casefold() == wanted for role in member.roles)
        except discord.NotFound:  # Left the server, or a webhook.
            result = False
        except discord.HTTPException as exc:
            log.warning("Couldn't look up roles for %s: %s", message.author.id, exc)
            return False
        self._role_cache[message.author.id] = (time.monotonic(), result)
        return result

    @staticmethod
    def _can_read(channel: Any) -> bool:
        try:
            permissions = channel.permissions_for(channel.guild.me)
        except discord.ClientException:
            return False
        return permissions.view_channel and permissions.read_message_history

    async def crawl(self) -> CrawlReport:
        async with self._lock:
            report = CrawlReport()
            guild = self._client.get_guild(self._config.guild_id)
            if guild is None:
                log.error("The bot isn't in server %s.", self._config.guild_id)
                return report
            for channel in (*guild.text_channels, *guild.voice_channels, *guild.stage_channels):
                await self._crawl_channel(channel, report)
            for thread in await self._all_threads(guild, report):
                await self._crawl_channel(thread, report)
            await self._store.kv_set(UNREADABLE_KEY, json.dumps(sorted(set(report.unreadable))))
            log.info(
                "Crawl finished: %s channels, %s messages, %s attachments, %s unreadable",
                report.channels_read,
                report.messages_read,
                report.attachments_found,
                len(set(report.unreadable)),
            )
            return report

    async def _all_threads(self, guild: discord.Guild, report: CrawlReport) -> list[discord.Thread]:
        threads: dict[int, discord.Thread] = {}
        try:
            for thread in await guild.active_threads():
                threads[thread.id] = thread
        except discord.HTTPException as exc:
            log.warning("Couldn't list active threads: %s", exc)
        for parent in (*guild.text_channels, *guild.forums):
            if not self.allowed(parent):
                continue
            if not self._can_read(parent):
                report.unreadable.append(parent.name)
                continue
            await self._collect_archived(parent, threads, private=False)
            if isinstance(parent, discord.TextChannel) and parent.permissions_for(guild.me).manage_threads:
                await self._collect_archived(parent, threads, private=True)
        return list(threads.values())

    @staticmethod
    async def _collect_archived(parent: Any, threads: dict[int, discord.Thread], *, private: bool) -> None:
        options = {"private": True} if private else {}
        try:
            async for thread in parent.archived_threads(limit=None, **options):
                threads[thread.id] = thread
        except discord.HTTPException as exc:
            log.warning("Couldn't list archived threads in %s: %s", parent.id, exc)

    async def collect_pins(self, channel: Any) -> None:
        """Queue any pinned messages with text. Ones already saved are ignored."""
        if not self.allowed(channel) or not self._can_read(channel):
            return
        await self._store.set_channel_label(channel.id, channel_label(channel))
        rows = []
        try:
            async for message in channel.pins(limit=None):
                row = pins.row_for(message)
                if row is not None and not await self.excluded(message):
                    rows.append(row)
        except discord.HTTPException as exc:
            log.warning("Couldn't read pins in %s: %s", channel.id, exc)
        if rows:
            await self._store.add_attachments(rows, already_saved=self.baseline)
            if not self.baseline:
                self._on_found()

    async def _crawl_channel(self, channel: Any, report: CrawlReport) -> None:
        if not self.allowed(channel):
            return
        if not self._can_read(channel):
            report.unreadable.append(channel.name)
            return
        await self.collect_pins(channel)
        await self._store.set_channel_label(channel.id, channel_label(channel))
        cursor = await self._store.cursor(channel.id)
        newest_known = channel.last_message_id
        if cursor and newest_known and cursor >= newest_known:
            return

        report.channels_read += 1
        after = discord.Object(id=cursor) if cursor else None
        pending: list[dict[str, Any]] = []
        logs: list[dict[str, Any]] = []
        newest, unsaved = cursor, 0

        async def save() -> None:
            nonlocal pending, logs, unsaved
            if newest:
                await self._store.record_batch(
                    channel.id, pending, newest, logs, already_saved=self.baseline
                )
                report.attachments_found += len(pending)
                if pending and not self.baseline:
                    self._on_found()
            pending, logs, unsaved = [], [], 0

        try:
            async for message in channel.history(limit=None, after=after, oldest_first=True):
                report.messages_read += 1
                newest = message.id
                unsaved += 1
                if message.author.id != self._client.user.id and not await self.excluded(message):
                    pending.extend(attachment_rows(message))
                    entry = log_row(message)
                    if entry is not None:
                        logs.append(entry)
                if unsaved >= _SAVE_EVERY:
                    await save()
        except discord.Forbidden:
            report.unreadable.append(channel.name)
        except discord.HTTPException as exc:
            log.warning("Stopped reading channel %s early: %s", channel.id, exc)
        await save()
