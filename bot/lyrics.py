"""Live lyrics for whatever someone plays on Spotify, read from their Discord status.

Lyrics come from the Spicy Lyrics API (word-synced, needs a free client key) and fall
back to LRCLIB (line-synced, no key). Ported from the Spicy Lyrics Plus providers.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import aiohttp
import discord
from discord import app_commands

from .store import Store

if TYPE_CHECKING:
    from .discord_app import ArchiveClient

log = logging.getLogger(__name__)

SPICY_API = "https://api.spicylyrics.org"
LRCLIB_API = "https://lrclib.net"
CHANNEL_KEY = "lyrics_channel"
SPICY_KEY = "lyrics_spicy_key"
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_HEADERS = {"User-Agent": "DiscordLyricsBot/1.0 (self-hosted)"}
MIN_EDIT_GAP = 1.2  # Discord limits message edits, so lines that go by faster are skipped.
STOP_AFTER = 300  # Seconds without Spotify before a listening session ends.
COLOR = 0x1DB954
_LRC_LINE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


@dataclass
class Line:
    start: float
    end: float
    text: str
    words: list[tuple[float, float, str]] = field(default_factory=list)


@dataclass
class Lyrics:
    lines: list[Line]
    synced: bool
    source: str


def _even_words(line: Line) -> list[tuple[float, float, str]]:
    """Spread a line's time over its words by length, for lyrics only timed per line."""
    tokens = line.text.split()
    if not tokens:
        return []
    total = sum(len(t) + 1 for t in tokens)
    span = max(0.3, line.end - line.start)
    words, cursor = [], line.start
    for token in tokens:
        length = span * (len(token) + 1) / total
        words.append((round(cursor, 3), round(cursor + length, 3), token))
        cursor += length
    return words


def _finish(lines: list[Line], source: str, synced: bool = True) -> Lyrics | None:
    lines = [l for l in lines if l.text.strip()]
    if not lines:
        return None
    lines.sort(key=lambda l: l.start)
    for index, line in enumerate(lines):
        following = lines[index + 1].start if index + 1 < len(lines) else line.start + 6
        if line.end <= line.start or line.end > following + 0.5:
            line.end = min(following, line.start + 8)
        if synced and not line.words:
            line.words = _even_words(line)
    return Lyrics(lines, synced, source)


def parse_spicy(payload: dict[str, Any]) -> Lyrics | None:
    """Spicy Lyrics API body: syllable-timed, line-timed, or plain ("Static")."""
    if payload.get("Status") != 200:
        return None
    body = payload.get("Body") or {}
    lines: list[Line] = []
    for item in body.get("Content") or []:
        if not isinstance(item, dict) or str(item.get("Type", "")).lower() == "interlude":
            continue
        lead = item.get("Lead")
        if isinstance(lead, dict):
            syllables = [s for s in (lead.get("Syllables") or []) if isinstance(s, dict) and "Text" in s]
            if not syllables:  # A line can be background vocals alone.
                for group in item.get("Background") or []:
                    syllables += [s for s in (group.get("Syllables") or []) if isinstance(s, dict) and "Text" in s]
            if not syllables:
                continue
            syllables.sort(key=lambda s: s.get("StartTime", 0))
            words: list[tuple[float, float, str]] = []
            text = ""
            joined = False
            for syl in syllables:
                piece = str(syl["Text"])
                start, end = float(syl.get("StartTime", 0)), float(syl.get("EndTime", 0))
                if joined and words:
                    s0, _, t0 = words[-1]
                    words[-1] = (s0, end, t0 + piece)
                    text += piece
                else:
                    words.append((start, end, piece))
                    text += ("" if not text else " ") + piece
                joined = bool(syl.get("IsPartOfWord"))
            start = float(lead.get("StartTime", words[0][0]))
            end = float(lead.get("EndTime", words[-1][1]))
            lines.append(Line(start, end, text, words))
        elif item.get("Text") and "StartTime" in item:
            lines.append(Line(float(item["StartTime"]), float(item.get("EndTime", 0)), str(item["Text"])))
    if lines:
        return _finish(lines, "Spicy Lyrics")
    plain = [str(l.get("Text", "")) for l in body.get("Lines") or [] if isinstance(l, dict)]
    if any(t.strip() for t in plain):
        return Lyrics([Line(0, 0, t) for t in plain if t.strip()], False, "Spicy Lyrics")
    return None


def parse_lrc(text: str, source: str = "LRCLIB") -> Lyrics | None:
    lines: list[Line] = []
    for raw in text.splitlines():
        stamps = list(_LRC_LINE.finditer(raw))
        if not stamps:
            continue
        words_text = _LRC_LINE.sub("", raw)
        words_text = re.sub(r"<\d+:\d+(?:\.\d+)?>", "", words_text).strip()
        for stamp in stamps:
            start = int(stamp.group(1)) * 60 + float(stamp.group(2))
            lines.append(Line(start, 0, words_text))
    return _finish(lines, source)


class LyricsFetcher:
    def __init__(self, session: aiohttp.ClientSession, spicy_api: str = SPICY_API, lrclib_api: str = LRCLIB_API) -> None:
        self._session = session
        self._spicy = spicy_api.rstrip("/")
        self._lrclib = lrclib_api.rstrip("/")
        self._cache: dict[str, Lyrics | None] = {}

    async def _json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        try:
            async with self._session.get(url, headers={**_HEADERS, **(headers or {})}, timeout=_TIMEOUT) as response:
                if response.status not in (200, 404):
                    log.info("Lyrics request to %s returned %s", url.split("?")[0], response.status)
                    return None
                return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            log.info("Lyrics request failed: %s", exc)
            return None

    async def get(self, track_id: str, title: str, artist: str, album: str, duration: float, spicy_key: str | None) -> Lyrics | None:
        if track_id in self._cache:
            return self._cache[track_id]
        found: Lyrics | None = None
        if spicy_key and track_id:
            data = await self._json(f"{self._spicy}/v1/lyrics/{track_id}", {"Authorization": f"Bearer {spicy_key}"})
            if isinstance(data, dict):
                found = parse_spicy(data)
        if found is None or not found.synced:
            first_artist = artist.split(";")[0].strip()
            query = {"track_name": title, "artist_name": first_artist, "album_name": album}
            if duration:
                query["duration"] = str(round(duration))
            data = await self._json(f"{self._lrclib}/api/get?{urlencode(query)}")
            if not (isinstance(data, dict) and (data.get("syncedLyrics") or data.get("plainLyrics"))):
                results = await self._json(f"{self._lrclib}/api/search?{urlencode({'track_name': title, 'artist_name': first_artist})}")
                data = next((r for r in results or [] if isinstance(r, dict) and r.get("syncedLyrics")), None) if isinstance(results, list) else None
            if isinstance(data, dict):
                synced = parse_lrc(data["syncedLyrics"]) if data.get("syncedLyrics") else None
                if synced:
                    found = synced
                elif found is None and data.get("plainLyrics"):
                    found = Lyrics([Line(0, 0, t) for t in str(data["plainLyrics"]).splitlines() if t.strip()], False, "LRCLIB")
        if len(self._cache) > 300:
            self._cache.clear()
        self._cache[track_id] = found
        return found


@dataclass
class Session:
    user_id: int
    token: str
    track_id: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    cover: str | None = None
    duration: float = 0.0
    start: float = 0.0  # Wall-clock time when the song was at 0:00.
    playing: bool = False
    lyrics: Lyrics | None = None
    message: discord.Message | None = None
    last_edit: float = 0.0
    shown_line: int | None = -2
    stopped_at: float | None = None
    task: asyncio.Task[None] | None = None

    def position(self, now: float | None = None) -> float:
        return max(0.0, (now or time.time()) - self.start)

    def line_index(self, position: float) -> int | None:
        if not self.lyrics or not self.lyrics.synced:
            return None
        current = None
        for index, line in enumerate(self.lyrics.lines):
            if line.start <= position:
                current = index
            else:
                break
        return current


def _mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class LyricsService:
    def __init__(self, client: "ArchiveClient", store: Store, fetcher: LyricsFetcher) -> None:
        self.client = client
        self.store = store
        self.fetcher = fetcher
        self.sessions: dict[int, Session] = {}
        self.by_token: dict[str, Session] = {}
        self._enabled: dict[int, bool] = {}

    async def enabled(self, user_id: int) -> bool:
        if user_id not in self._enabled:
            row = await self.store.fetch_one("SELECT enabled FROM lyrics_user WHERE user_id = ?", (user_id,))
            self._enabled[user_id] = bool(row and row["enabled"])
        return self._enabled[user_id]

    async def set_enabled(self, user_id: int, on: bool) -> None:
        await self.store.execute(
            "INSERT INTO lyrics_user (user_id, enabled) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled",
            (user_id, 1 if on else 0),
        )
        self._enabled[user_id] = on
        if not on and user_id in self.sessions:
            await self._end(self.sessions[user_id], "Lyrics turned off")

    async def channel(self) -> discord.abc.Messageable | None:
        raw = await self.store.kv_get(CHANNEL_KEY)
        channel = self.client.get_channel(int(raw)) if raw else None
        return channel if isinstance(channel, discord.abc.Messageable) else None

    def page_url(self, session: Session) -> str | None:
        base = self.client.config.public_url
        return f"{base}/lyrics/{session.token}" if base else None

    # ---- Discord status changes ----------------------------------------------

    async def on_presence(self, member: discord.Member) -> None:
        if member.guild.id != self.client.config.guild_id or member.bot or not await self.enabled(member.id):
            return
        spotify = next((a for a in member.activities if isinstance(a, discord.Spotify)), None)
        session = self.sessions.get(member.id)
        if spotify is None:
            if session and session.playing:
                session.playing = False
                session.stopped_at = time.time()
                await self._render(session, force=True)
            return
        if await self.channel() is None:
            return
        start = spotify.start.timestamp() if spotify.start else time.time()
        if session is None:
            session = Session(member.id, secrets.token_urlsafe(12))
            self.sessions[member.id] = session
            self.by_token[session.token] = session
        new_track = spotify.track_id != session.track_id
        session.playing = True
        session.stopped_at = None
        if new_track:
            session.track_id = spotify.track_id
            session.title, session.artist, session.album = spotify.title, spotify.artist, spotify.album
            session.cover = spotify.album_cover_url
            session.duration = spotify.duration.total_seconds() if spotify.duration else 0
            session.start = start
            session.shown_line = -2
            spicy_key = await self.store.kv_get(SPICY_KEY)
            session.lyrics = await self.fetcher.get(
                spotify.track_id, spotify.title, spotify.artist, spotify.album, session.duration, spicy_key
            )
            await self._render(session, force=True)
        elif abs(start - session.start) > 1.5:  # Skipped ahead or back.
            session.start = start
            await self._render(session, force=True)
        if session.task is None or session.task.done():
            session.task = self.client._spawn(self._follow(session))

    async def _follow(self, session: Session) -> None:
        """Keep the message on the current line until the person stops listening."""
        while True:
            if not session.playing:
                if session.stopped_at and time.time() - session.stopped_at > STOP_AFTER:
                    await self._end(session, "Stopped listening")
                    return
                await asyncio.sleep(2)
                continue
            await self._render(session)
            position = session.position()
            wait = 2.0
            if session.lyrics and session.lyrics.synced:
                upcoming = [l.start for l in session.lyrics.lines if l.start > position + 0.05]
                if upcoming:
                    wait = upcoming[0] - position
            gap = MIN_EDIT_GAP - (time.time() - session.last_edit)
            await asyncio.sleep(min(max(wait, gap, 0.25), 5.0))

    def _embed(self, session: Session) -> discord.Embed:
        member_name, avatar = "Someone", None
        guild = self.client.get_guild(self.client.config.guild_id)
        member = guild.get_member(session.user_id) if guild else None
        if member:
            member_name, avatar = member.display_name, member.display_avatar.url
        status = "is listening" if session.playing else "paused"
        embed = discord.Embed(
            title=f"{session.title} — {session.artist}"[:250],
            url=f"https://open.spotify.com/track/{session.track_id}" if session.track_id else None,
            color=COLOR if session.playing else 0x555555,
        )
        embed.set_author(name=f"{member_name} {status}", icon_url=avatar)
        if session.cover:
            embed.set_thumbnail(url=session.cover)
        position = session.position() if session.playing else (session.stopped_at or time.time()) - session.start
        lyrics = session.lyrics
        if lyrics is None:
            embed.description = "*No lyrics found for this song.*"
        elif not lyrics.synced:
            text = "\n".join(l.text for l in lyrics.lines[:14])
            embed.description = text[:1800] + ("\n…" if len(lyrics.lines) > 14 else "")
        else:
            index = session.line_index(position)
            lines = lyrics.lines
            if index is None:
                parts = ["-# ♪", f"### {lines[0].text}" if lines else ""]
                parts += [f"-# {l.text}" for l in lines[1:3]]
            else:
                parts = []
                if index > 0:
                    parts.append(f"-# {lines[index - 1].text}")
                parts.append(f"### {lines[index].text}")
                parts += [f"-# {l.text}" for l in lines[index + 1:index + 3]]
            embed.description = "\n".join(p for p in parts if p.strip())[:3500]
        source = f" · {lyrics.source}" if lyrics else ""
        embed.set_footer(text=f"{_mmss(position)} / {_mmss(session.duration)}{source}")
        return embed

    def _view(self, session: Session) -> discord.ui.View | None:
        url = self.page_url(session)
        if not url:
            return None
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Word by word", url=url, emoji="🎤"))
        return view

    async def _render(self, session: Session, force: bool = False) -> None:
        index = session.line_index(session.position())
        if not force and index == session.shown_line:
            return
        if not force and time.time() - session.last_edit < MIN_EDIT_GAP:
            return
        session.shown_line = index
        session.last_edit = time.time()
        embed, view = self._embed(session), self._view(session)
        try:
            if session.message is None:
                channel = await self.channel()
                if channel is None:
                    return
                session.message = await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
            else:
                await session.message.edit(embed=embed, view=view)
        except discord.NotFound:
            session.message = None
        except discord.HTTPException as exc:
            log.info("Couldn't update lyrics: %s", exc)

    async def _end(self, session: Session, reason: str) -> None:
        self.sessions.pop(session.user_id, None)
        self.by_token.pop(session.token, None)
        if session.task and session.task is not asyncio.current_task():
            session.task.cancel()
        if session.message:
            embed = self._embed(session)
            embed.description = f"*{reason}.* Last played: **{session.title}** — {session.artist}"
            try:
                await session.message.edit(embed=embed, view=None)
            except discord.HTTPException:
                pass

    # ---- for the word-by-word web page ----------------------------------------

    def state(self, token: str) -> dict[str, Any] | None:
        session = self.by_token.get(token)
        if session is None:
            return None
        lyrics = session.lyrics
        return {
            "now_ms": int(time.time() * 1000),
            "playing": session.playing,
            "stopped_ms": int(session.stopped_at * 1000) if session.stopped_at else None,
            "start_ms": int(session.start * 1000),
            "track": {
                "id": session.track_id, "title": session.title, "artist": session.artist,
                "album": session.album, "cover": session.cover, "duration_ms": int(session.duration * 1000),
            },
            "synced": bool(lyrics and lyrics.synced),
            "source": lyrics.source if lyrics else None,
            "lines": [
                {"s": l.start, "e": l.end, "t": l.text, "w": [[w[0], w[1], w[2]] for w in l.words]}
                for l in (lyrics.lines if lyrics else [])
            ],
        }


class LyricsGroup(app_commands.Group):
    def __init__(self, service: LyricsService) -> None:
        super().__init__(name="lyrics", description="Live Spotify lyrics in the music channel")
        self.service = service

    @app_commands.command(name="setup", description="Pick the music channel and (optionally) add a Spicy Lyrics key")
    @app_commands.describe(
        channel="Where live lyrics are posted",
        spicy_key="Your Spicy Lyrics client key (sl_pk_...) for word-synced lyrics. Leave empty to use LRCLIB only.",
    )
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel, spicy_key: str | None = None) -> None:
        perms = getattr(interaction.user, "guild_permissions", None)
        if interaction.user.id != self.service.client.config.owner_id and not (perms and perms.manage_guild):
            await interaction.response.send_message("You need Manage Server to do that.", ephemeral=True)
            return
        await self.service.store.kv_set(CHANNEL_KEY, str(channel.id))
        note = ""
        if spicy_key:
            key = spicy_key.strip()
            if key.startswith("sl_sk_"):
                await interaction.response.send_message(
                    "That's a *secret* key (sl_sk_). Use the client key that starts with sl_pk_.", ephemeral=True
                )
                return
            if not re.fullmatch(r"sl_pk_[A-Za-z0-9_-]{8,}", key):
                await interaction.response.send_message("That doesn't look like a Spicy Lyrics client key (sl_pk_...).", ephemeral=True)
                return
            await self.service.store.kv_set(SPICY_KEY, key)
            self.service.fetcher._cache.clear()
            note = " Word-synced lyrics from Spicy Lyrics are on."
        await interaction.response.send_message(
            f"Live lyrics will go to {channel.mention}.{note} Everyone who wants theirs shown runs `/lyrics on`.",
            ephemeral=True,
        )

    @app_commands.command(name="on", description="Show live lyrics for what you play on Spotify")
    async def on(self, interaction: discord.Interaction) -> None:
        await self.service.set_enabled(interaction.user.id, True)
        channel = await self.service.channel()
        where = channel.mention if isinstance(channel, discord.TextChannel) else "the music channel (not set yet: `/lyrics setup`)"
        await interaction.response.send_message(
            f"On. When you play something on Spotify, lyrics show up live in {where}.\n"
            "Make sure Spotify is connected in Discord (Settings → Connections) with *Display on profile* and "
            "*Display Spotify as your status* turned on.",
            ephemeral=True,
        )

    @app_commands.command(name="off", description="Stop showing your lyrics")
    async def off(self, interaction: discord.Interaction) -> None:
        await self.service.set_enabled(interaction.user.id, False)
        await interaction.response.send_message("Off. Your music won't be shown anymore.", ephemeral=True)

    @app_commands.command(name="now", description="Get the full lyrics of what you're playing, just for you")
    async def now(self, interaction: discord.Interaction) -> None:
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        spotify = next((a for a in (member.activities if member else []) if isinstance(a, discord.Spotify)), None)
        if spotify is None:
            await interaction.response.send_message(
                "I can't see you playing anything on Spotify. Check that Spotify shows as your Discord status.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lyrics = await self.service.fetcher.get(
            spotify.track_id, spotify.title, spotify.artist, spotify.album,
            spotify.duration.total_seconds() if spotify.duration else 0, await self.service.store.kv_get(SPICY_KEY),
        )
        embed = discord.Embed(title=f"{spotify.title} — {spotify.artist}"[:250], color=COLOR,
                              url=f"https://open.spotify.com/track/{spotify.track_id}")
        embed.set_thumbnail(url=spotify.album_cover_url)
        embed.description = "\n".join(l.text for l in lyrics.lines)[:4000] if lyrics else "*No lyrics found.*"
        if lyrics:
            embed.set_footer(text=lyrics.source)
        await interaction.followup.send(embed=embed, ephemeral=True)
