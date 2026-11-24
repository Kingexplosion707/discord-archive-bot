"""Game tracking: what people play, for how long, and who plays the most.

Sources:
- Discord's "Playing …" status. Only real games count: the game has to be on
  Discord's list of detectable games (the same list behind "Playing"), or have
  an exact match on the Steam store. Launchers and apps (Medal, OBS, Steam,
  Overwolf, Spotify, browsers…) never count, even if they show as "Playing".
- Steam, for people who link a public profile: what they're playing right now
  (when Discord doesn't show it) and lifetime hours per game.
- Epic Games has no public API for playtime; Epic games are tracked through
  Discord's status like any other game.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import aiohttp
import discord
from discord import app_commands

if TYPE_CHECKING:
    from .discord_app import ArchiveClient

log = logging.getLogger(__name__)

DISCORD_CATALOG = "https://discord.com/api/v10/applications/detectable"
STEAM_API = "https://api.steampowered.com"
STEAM_STORE = "https://store.steampowered.com/api"
STEAM_IMAGES = "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps"
DISCORD_CDN = "https://cdn.discordapp.com/app-icons"

CHANNEL_KEY = "games_channel"
START_POSTS_KEY = "games_start_posts"
STEAM_KEY = "games_steam_key"
CATALOG_AT_KEY = "games_catalog_at"

CATALOG_EVERY = 24 * 3600
HEARTBEAT_EVERY = 60
STEAM_EVERY = 180
STEAM_LIBRARY_EVERY = 12 * 3600
MIN_SESSION = 60  # shorter than this isn't kept
POST_AFTER = 10 * 60  # sessions this long get an end-of-session post
GAME_COLOR = 0x8B5CF6
_TIMEOUT = aiohttp.ClientTimeout(total=60)

# Apps that show up as "Playing" but aren't games. Checked before anything else.
NOT_GAMES = {
    "medal", "medal.tv", "obs", "obs studio", "streamlabs", "streamlabs desktop", "xsplit", "twitch studio",
    "spotify", "youtube", "youtube music", "netflix", "twitch", "crunchyroll", "plex", "vlc", "vlc media player",
    "steam", "steam big picture", "epic games launcher", "epic games store", "ea app", "origin", "ubisoft connect",
    "battle.net", "gog galaxy", "xbox", "xbox app", "playnite", "launchbox", "heroic games launcher",
    "overwolf", "outplayed", "insights capture", "nvidia share", "geforce experience", "nvidia app", "amd software",
    "wallpaper engine", "discord", "visual studio code", "code", "visual studio", "intellij idea", "pycharm",
    "google chrome", "chrome", "firefox", "microsoft edge", "opera", "opera gx", "brave", "safari",
    "photoshop", "adobe photoshop", "premiere pro", "after effects", "blender", "figma", "notion", "obsidian",
    "premid", "rpc", "custom status", "rich presence", "vencord", "betterdiscord", "lunar client", "badlion client",
}


def _norm(name: str) -> str:
    """Lowercase, accents and symbols dropped: "ELDEN RING™" -> "elden ring"."""
    name = re.sub(r"[™®©]", "", name or "").lower()
    name = re.sub(r"[^a-z0-9]+", " ", name)
    return name.strip()


def clock(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{max(minutes, 1)}m"


def hours(seconds: float) -> str:
    h = seconds / 3600
    return f"{h:,.1f}h" if h < 100 else f"{h:,.0f}h"


@dataclass
class Game:
    key: str
    name: str
    discord_id: str | None = None
    steam_appid: int | None = None
    cover: str | None = None
    icon: str | None = None
    themes: str = ""


def _steam_cover(appid: int) -> str:
    return f"{STEAM_IMAGES}/{appid}/library_600x900.jpg"


def _steam_header(appid: int) -> str:
    return f"{STEAM_IMAGES}/{appid}/header.jpg"


class GameTracker:
    def __init__(
        self,
        client: "ArchiveClient",
        catalog_url: str = DISCORD_CATALOG,
        steam_api: str = STEAM_API,
        steam_store: str = STEAM_STORE,
    ) -> None:
        self.client = client
        self.store = client.store
        self.catalog_url = catalog_url
        self.steam_api = steam_api.rstrip("/")
        self.steam_store = steam_store.rstrip("/")
        self._name_checks: dict[str, Game | None] = {}
        self._lock = asyncio.Lock()
        self.catalog_size = 0
        self.last_error: str | None = None

    # ---- setup ----------------------------------------------------------------

    async def start(self) -> None:
        """Close sessions left open when the bot stopped, then run the loops."""
        async with self._lock:
            for row in await self.store.fetch_all("SELECT * FROM game_open"):
                await self._close(row, ended=row["last_seen"], post=False)
        row = await self.store.fetch_one("SELECT COUNT(*) AS n FROM game_catalog")
        self.catalog_size = row["n"] if row else 0
        self.client._spawn(self._loop(self.refresh_catalog_if_due, 600))
        self.client._spawn(self._loop(self.heartbeat, HEARTBEAT_EVERY))
        self.client._spawn(self._loop(self.poll_steam, STEAM_EVERY))

    async def _loop(self, job: Any, every: float) -> None:
        while True:
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)[:200]
                log.exception("Game tracker job %s failed", getattr(job, "__name__", job))
            await asyncio.sleep(every)

    # ---- which "Playing" statuses are games ------------------------------------

    async def refresh_catalog_if_due(self, force: bool = False) -> None:
        last = float(await self.store.kv_get(CATALOG_AT_KEY) or 0)
        if not force and self.catalog_size and time.time() - last < CATALOG_EVERY:
            return
        async with self.client.web.get(self.catalog_url, timeout=_TIMEOUT) as response:
            response.raise_for_status()
            apps = await response.json(content_type=None)
        rows, aliases = [], []
        for app in apps if isinstance(apps, list) else []:
            if not isinstance(app, dict) or not app.get("id") or not app.get("name"):
                continue
            steam = next((s.get("id") for s in app.get("third_party_skus") or [] if s.get("distributor") == "steam"), None)
            rows.append((
                str(app["id"]), app["name"], _norm(app["name"]), app.get("icon_hash"), app.get("cover_image_hash"),
                int(steam) if str(steam or "").isdigit() else None, ", ".join((app.get("themes") or [])[:3]),
            ))
            for alias in app.get("aliases") or []:
                if isinstance(alias, str) and _norm(alias):
                    aliases.append((_norm(alias), str(app["id"])))
        del apps
        async with self.store.transaction() as db:
            await db.execute("DELETE FROM game_catalog")
            await db.execute("DELETE FROM game_alias")
            await db.executemany("INSERT OR REPLACE INTO game_catalog VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            await db.executemany("INSERT OR IGNORE INTO game_alias VALUES (?, ?)", aliases)
        await self.store.kv_set(CATALOG_AT_KEY, str(time.time()))
        self.catalog_size = len(rows)
        self._name_checks.clear()
        log.info("Game list refreshed: %d games", len(rows))

    def _from_catalog(self, row: Any) -> Game:
        app_id = row["app_id"]
        steam = row["steam_appid"]
        cover = _steam_cover(steam) if steam else (f"{DISCORD_CDN}/{app_id}/{row['cover_hash']}.png?size=512" if row["cover_hash"] else None)
        icon = f"{DISCORD_CDN}/{app_id}/{row['icon_hash']}.png?size=128" if row["icon_hash"] else None
        return Game(key=f"d:{app_id}", name=row["name"], discord_id=app_id, steam_appid=steam, cover=cover, icon=icon, themes=row["themes"] or "")

    async def identify(self, activity: Any) -> Game | None:
        """The game behind a Discord activity, or None if it isn't a game."""
        if getattr(activity, "type", None) != discord.ActivityType.playing:
            return None
        name = (getattr(activity, "name", "") or "").strip()
        norm = _norm(name)
        if not norm or norm in NOT_GAMES or name.lower() in NOT_GAMES:
            return None
        app_id = getattr(activity, "application_id", None)
        if app_id:
            row = await self.store.fetch_one("SELECT * FROM game_catalog WHERE app_id = ?", (str(app_id),))
            if row:
                return self._from_catalog(row)
        row = await self.store.fetch_one("SELECT * FROM game_catalog WHERE name_norm = ? LIMIT 1", (norm,))
        if row is None:
            row = await self.store.fetch_one(
                "SELECT c.* FROM game_alias a JOIN game_catalog c ON c.app_id = a.app_id WHERE a.alias_norm = ? LIMIT 1", (norm,)
            )
        if row:
            return self._from_catalog(row)
        return await self._steam_lookup(name)

    async def _steam_lookup(self, name: str) -> Game | None:
        """Last resort: a game on the Steam store with exactly this name."""
        norm = _norm(name)
        if norm in self._name_checks:
            return self._name_checks[norm]
        cached = await self.store.fetch_one("SELECT steam_appid, name, checked_at FROM game_name_check WHERE name_norm = ?", (norm,))
        if cached and time.time() - cached["checked_at"] < 7 * 86400:
            game = self._steam_game(cached["steam_appid"], cached["name"]) if cached["steam_appid"] else None
            self._name_checks[norm] = game
            return game
        found: tuple[int, str] | None = None
        try:
            async with self.client.web.get(
                f"{self.steam_store}/storesearch/", params={"term": name, "cc": "us", "l": "english"}, timeout=_TIMEOUT
            ) as response:
                data = await response.json(content_type=None)
            for item in (data or {}).get("items") or []:
                if item.get("type") == "app" and _norm(item.get("name", "")) == norm:
                    found = (int(item["id"]), item["name"])
                    break
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as exc:
            log.info("Steam store search failed for %r: %s", name, exc)
            return None  # Don't cache a failure.
        await self.store.execute(
            "INSERT OR REPLACE INTO game_name_check VALUES (?, ?, ?, ?)",
            (norm, found[0] if found else None, found[1] if found else name, time.time()),
        )
        game = self._steam_game(*found) if found else None
        self._name_checks[norm] = game
        return game

    def _steam_game(self, appid: int, name: str) -> Game:
        return Game(key=f"s:{appid}", name=name, steam_appid=appid, cover=_steam_cover(appid))

    async def _remember(self, game: Game) -> None:
        await self.store.execute(
            "INSERT INTO game (key, name, discord_id, steam_appid, cover, icon, themes, first_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET name = excluded.name, cover = excluded.cover, icon = excluded.icon, themes = excluded.themes",
            (game.key, game.name, game.discord_id, game.steam_appid, game.cover, game.icon, game.themes, time.time()),
        )

    async def game(self, key: str) -> Game | None:
        row = await self.store.fetch_one("SELECT * FROM game WHERE key = ?", (key,))
        if not row:
            return None
        return Game(row["key"], row["name"], row["discord_id"], row["steam_appid"], row["cover"], row["icon"], row["themes"] or "")

    # ---- sessions ------------------------------------------------------------------

    async def opted_out(self, user_id: int) -> bool:
        return bool(await self.store.fetch_one("SELECT 1 FROM game_optout WHERE user_id = ?", (user_id,)))

    async def playing_now(self, member: discord.Member) -> Game | None:
        for activity in member.activities or ():
            game = await self.identify(activity)
            if game:
                return game
        return None

    async def on_presence(self, member: discord.Member) -> None:
        if member.bot or member.guild.id != self.client.config.guild_id:
            return
        if await self.opted_out(member.id):
            return
        game = await self.playing_now(member)
        await self._update(member.id, game, "discord")

    async def _update(self, user_id: int, game: Game | None, source: str) -> None:
        async with self._lock:
            now = time.time()
            open_row = await self.store.fetch_one("SELECT * FROM game_open WHERE user_id = ?", (user_id,))
            if open_row and game and open_row["game_key"] == game.key:
                await self.store.execute("UPDATE game_open SET last_seen = ? WHERE user_id = ?", (now, user_id))
                return
            if open_row and (game or open_row["source"] == source):
                # Switched games, or this source says they stopped.
                await self._close(open_row, ended=now)
                open_row = None
            if game and not open_row:
                await self._remember(game)
                await self.store.execute(
                    "INSERT OR REPLACE INTO game_open (user_id, game_key, started_at, last_seen, source, channel_id, message_id) "
                    "VALUES (?, ?, ?, ?, ?, NULL, NULL)",
                    (user_id, game.key, now, now, source),
                )
                await self._post_start(user_id, game, now)

    async def _close(self, row: Any, ended: float, post: bool = True) -> None:
        await self.store.execute("DELETE FROM game_open WHERE user_id = ?", (row["user_id"],))
        seconds = max(0.0, ended - row["started_at"])
        if seconds < MIN_SESSION:
            if row["message_id"]:
                await self._delete_post(row["channel_id"], row["message_id"])
            return
        await self.store.execute(
            "INSERT INTO game_session (user_id, game_key, started_at, ended_at, seconds, source) VALUES (?, ?, ?, ?, ?, ?)",
            (row["user_id"], row["game_key"], row["started_at"], ended, seconds, row["source"]),
        )
        if post:
            await self._post_end(row, seconds)

    async def heartbeat(self) -> None:
        """Keep open sessions honest: close any whose game isn't showing any more."""
        guild = self.client.get_guild(self.client.config.guild_id)
        for row in await self.store.fetch_all("SELECT * FROM game_open WHERE source = 'discord'"):
            member = guild.get_member(row["user_id"]) if guild else None
            game = await self.playing_now(member) if member else None
            if game and game.key == row["game_key"]:
                await self.store.execute("UPDATE game_open SET last_seen = ? WHERE user_id = ?", (time.time(), row["user_id"]))
            elif member is not None:
                async with self._lock:
                    current = await self.store.fetch_one("SELECT * FROM game_open WHERE user_id = ?", (row["user_id"],))
                    if current and current["game_key"] == row["game_key"]:
                        await self._close(current, ended=time.time())

    # ---- posts -----------------------------------------------------------------------

    async def _channel(self) -> Any:
        channel_id = await self.store.kv_get(CHANNEL_KEY)
        return self.client.get_channel(int(channel_id)) if channel_id else None

    def _name(self, user_id: int) -> str:
        guild = self.client.get_guild(self.client.config.guild_id)
        member = guild.get_member(user_id) if guild else None
        return member.display_name if member else f"User {user_id}"

    def _avatar(self, user_id: int) -> str | None:
        guild = self.client.get_guild(self.client.config.guild_id)
        member = guild.get_member(user_id) if guild else None
        return member.display_avatar.url if member else None

    async def _total(self, user_id: int, game_key: str) -> float:
        row = await self.store.fetch_one(
            "SELECT COALESCE(SUM(seconds), 0) AS s FROM game_session WHERE user_id = ? AND game_key = ?", (user_id, game_key)
        )
        return row["s"] if row else 0.0

    async def _embed(self, user_id: int, game: Game, verb: str, lines: list[str]) -> discord.Embed:
        embed = discord.Embed(color=GAME_COLOR, description="\n".join(lines))
        embed.set_author(name=f"{self._name(user_id)} {verb}", icon_url=self._avatar(user_id))
        embed.title = game.name
        if game.steam_appid:
            embed.url = f"https://store.steampowered.com/app/{game.steam_appid}"
        if game.cover:
            embed.set_thumbnail(url=game.cover)
        footer = " · ".join(x for x in [game.themes, "Steam" if game.key.startswith("s:") else None] if x)
        if footer:
            embed.set_footer(text=footer)
        return embed

    async def _post_start(self, user_id: int, game: Game, started: float) -> None:
        if await self.store.kv_get(START_POSTS_KEY) != "1":
            return
        channel = await self._channel()
        if channel is None:
            return
        total = await self._total(user_id, game.key)
        lines = [f"🎮 Playing now · started <t:{int(started)}:R>"]
        if total:
            lines.append(f"-# {hours(total)} played before")
        try:
            message = await channel.send(embed=await self._embed(user_id, game, "is playing", lines))
        except discord.HTTPException as exc:
            log.info("Couldn't post game start: %s", exc)
            return
        await self.store.execute(
            "UPDATE game_open SET channel_id = ?, message_id = ? WHERE user_id = ?", (channel.id, message.id, user_id)
        )

    async def _post_end(self, row: Any, seconds: float) -> None:
        game = await self.game(row["game_key"])
        if game is None:
            return
        if seconds < POST_AFTER and not row["message_id"]:
            return
        total = await self._total(row["user_id"], game.key)
        lines = [
            f"🕒 Played for **{clock(seconds)}**",
            f"-# <t:{int(row['started_at'])}:t> – <t:{int(row['started_at'] + seconds)}:t> · {hours(total)} total",
        ]
        embed = await self._embed(row["user_id"], game, "played", lines)
        try:
            if row["message_id"]:
                channel = self.client.get_channel(row["channel_id"])
                if channel is not None:
                    await channel.get_partial_message(row["message_id"]).edit(embed=embed)
                return
            channel = await self._channel()
            if channel is not None:
                await channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.info("Couldn't post game session: %s", exc)

    async def _delete_post(self, channel_id: int | None, message_id: int) -> None:
        channel = self.client.get_channel(channel_id) if channel_id else None
        if channel is not None:
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.HTTPException:
                pass

    # ---- Steam ---------------------------------------------------------------------------

    async def steam_key(self) -> str | None:
        stored = await self.store.kv_get(STEAM_KEY)
        if not stored:
            return None
        try:
            return self.client.vault.unseal(bytes.fromhex(stored)).decode()
        except Exception:
            return None

    async def set_steam_key(self, key: str) -> None:
        await self.store.kv_set(STEAM_KEY, self.client.vault.seal(key.strip().encode()).hex())

    async def _steam(self, path: str, **params: Any) -> dict[str, Any]:
        key = await self.steam_key()
        if not key:
            raise SteamError("The owner hasn't added a Steam Web API key yet (`/games steamkey`).")
        try:
            async with self.client.web.get(f"{self.steam_api}/{path}", params={"key": key, **params}, timeout=_TIMEOUT) as response:
                if response.status in (401, 403):
                    raise SteamError("Steam turned the API key down. The owner can set a new one with `/games steamkey`.")
                response.raise_for_status()
                return await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise SteamError(f"Steam didn't answer ({exc})") from exc

    async def resolve_steam(self, text: str) -> str:
        """A profile link, a 17-digit SteamID or a custom URL name -> SteamID64."""
        text = text.strip().rstrip("/")
        match = re.search(r"steamcommunity\.com/profiles/(\d{17})", text) or re.fullmatch(r"(\d{17})", text)
        if match:
            return match.group(1)
        vanity = re.search(r"steamcommunity\.com/id/([^/?#]+)", text)
        name = vanity.group(1) if vanity else text
        data = await self._steam("ISteamUser/ResolveVanityURL/v1/", vanityurl=name)
        result = (data or {}).get("response") or {}
        if result.get("success") != 1 or not result.get("steamid"):
            raise SteamError("Couldn't find that Steam profile. Paste your profile link (steamcommunity.com/…).")
        return result["steamid"]

    async def link_steam(self, user_id: int, text: str) -> dict[str, Any]:
        steam_id = await self.resolve_steam(text)
        data = await self._steam("ISteamUser/GetPlayerSummaries/v2/", steamids=steam_id)
        players = ((data or {}).get("response") or {}).get("players") or []
        if not players:
            raise SteamError("Couldn't find that Steam profile.")
        player = players[0]
        if player.get("communityvisibilitystate") != 3:
            raise SteamError("That Steam profile is private. Set your profile and game details to Public, then try again.")
        await self.store.execute(
            "INSERT OR REPLACE INTO steam_link (user_id, steam_id, persona, avatar, linked_at, library_at) VALUES (?, ?, ?, ?, ?, 0)",
            (user_id, steam_id, player.get("personaname"), player.get("avatarfull"), time.time()),
        )
        await self.sync_library(user_id, steam_id)
        return player

    async def sync_library(self, user_id: int, steam_id: str) -> int:
        data = await self._steam(
            "IPlayerService/GetOwnedGames/v1/", steamid=steam_id, include_appinfo=1, include_played_free_games=1
        )
        games = ((data or {}).get("response") or {}).get("games") or []
        async with self.store.transaction() as db:
            await db.execute("DELETE FROM steam_library WHERE user_id = ?", (user_id,))
            await db.executemany(
                "INSERT INTO steam_library (user_id, appid, name, minutes, minutes_2w) VALUES (?, ?, ?, ?, ?)",
                [(user_id, g["appid"], g.get("name") or str(g["appid"]), g.get("playtime_forever", 0), g.get("playtime_2weeks", 0)) for g in games],
            )
            await db.execute("UPDATE steam_link SET library_at = ? WHERE user_id = ?", (time.time(), user_id))
        return len(games)

    async def poll_steam(self) -> None:
        """What linked people are playing on Steam, and their libraries twice a day."""
        links = await self.store.fetch_all("SELECT * FROM steam_link")
        if not links or not await self.steam_key():
            return
        by_steam = {row["steam_id"]: row for row in links}
        ids = list(by_steam)
        for start in range(0, len(ids), 100):
            data = await self._steam("ISteamUser/GetPlayerSummaries/v2/", steamids=",".join(ids[start:start + 100]))
            for player in ((data or {}).get("response") or {}).get("players") or []:
                link = by_steam.get(player.get("steamid"))
                if link is None or await self.opted_out(link["user_id"]):
                    continue
                open_row = await self.store.fetch_one("SELECT source FROM game_open WHERE user_id = ?", (link["user_id"],))
                if open_row and open_row["source"] == "discord":
                    continue  # Discord already shows what they're playing.
                appid = player.get("gameid")
                game = None
                if appid and str(appid).isdigit():
                    row = await self.store.fetch_one("SELECT * FROM game_catalog WHERE steam_appid = ? LIMIT 1", (int(appid),))
                    game = self._from_catalog(row) if row else self._steam_game(int(appid), player.get("gameextrainfo") or f"App {appid}")
                if game and _norm(game.name) in NOT_GAMES:
                    game = None
                await self._update(link["user_id"], game, "steam")
        for link in links:
            if time.time() - (link["library_at"] or 0) > STEAM_LIBRARY_EVERY:
                try:
                    await self.sync_library(link["user_id"], link["steam_id"])
                except SteamError as exc:
                    log.info("Steam library sync failed for %s: %s", link["user_id"], exc)

    # ---- numbers -------------------------------------------------------------------------

    @staticmethod
    def since(period: str) -> float:
        now = datetime.now(timezone.utc)
        if period == "week":
            return (now - timedelta(days=7)).timestamp()
        if period == "month":
            return (now - timedelta(days=30)).timestamp()
        return 0.0

    async def leaderboard(self, period: str, limit: int = 10) -> list[Any]:
        return await self.store.fetch_all(
            "SELECT user_id, SUM(seconds) AS seconds, COUNT(DISTINCT game_key) AS games FROM game_session "
            "WHERE ended_at >= ? AND user_id NOT IN (SELECT user_id FROM game_optout) "
            "GROUP BY user_id ORDER BY seconds DESC LIMIT ?",
            (self.since(period), limit),
        )

    async def top_games(self, period: str, limit: int = 10) -> list[Any]:
        return await self.store.fetch_all(
            "SELECT s.game_key, g.name, g.cover, SUM(s.seconds) AS seconds, COUNT(DISTINCT s.user_id) AS players "
            "FROM game_session s JOIN game g ON g.key = s.game_key WHERE s.ended_at >= ? "
            "AND s.user_id NOT IN (SELECT user_id FROM game_optout) GROUP BY s.game_key ORDER BY seconds DESC LIMIT ?",
            (self.since(period), limit),
        )

    async def user_games(self, user_id: int, period: str = "all", limit: int = 5) -> list[Any]:
        return await self.store.fetch_all(
            "SELECT s.game_key, g.name, g.cover, SUM(s.seconds) AS seconds, MAX(s.ended_at) AS last FROM game_session s "
            "JOIN game g ON g.key = s.game_key WHERE s.user_id = ? AND s.ended_at >= ? GROUP BY s.game_key ORDER BY seconds DESC LIMIT ?",
            (user_id, self.since(period), limit),
        )

    async def streak(self, user_id: int) -> int:
        """Days in a row (ending today or yesterday, UTC) with at least one session."""
        rows = await self.store.fetch_all(
            "SELECT DISTINCT date(ended_at, 'unixepoch') AS d FROM game_session WHERE user_id = ? ORDER BY d DESC LIMIT 400", (user_id,)
        )
        days = [datetime.strptime(r["d"], "%Y-%m-%d").date() for r in rows]
        if not days:
            return 0
        today = datetime.now(timezone.utc).date()
        if days[0] < today - timedelta(days=1):
            return 0
        count = 1
        for prev, cur in zip(days, days[1:]):
            if prev - cur != timedelta(days=1):
                break
            count += 1
        return count

    async def find_game(self, text: str) -> Game | None:
        row = await self.store.fetch_one("SELECT key FROM game WHERE key = ?", (text,))
        if row is None:
            row = await self.store.fetch_one(
                "SELECT g.key FROM game g LEFT JOIN game_session s ON s.game_key = g.key WHERE lower(g.name) LIKE ? "
                "GROUP BY g.key ORDER BY COALESCE(SUM(s.seconds), 0) DESC LIMIT 1",
                (f"%{text.lower()}%",),
            )
        return await self.game(row["key"]) if row else None


class SteamError(Exception):
    pass


PERIODS = [
    app_commands.Choice(name="This week", value="week"),
    app_commands.Choice(name="This month (30 days)", value="month"),
    app_commands.Choice(name="All time", value="all"),
]
PERIOD_NAMES = {"week": "this week", "month": "this month", "all": "all time"}
MEDALS = ["🥇", "🥈", "🥉"]


class SteamKeyModal(discord.ui.Modal, title="Steam Web API key"):
    key: discord.ui.TextInput = discord.ui.TextInput(
        label="Steam Web API key", placeholder="steamcommunity.com/dev/apikey", min_length=20, max_length=64
    )

    def __init__(self, games: GameTracker) -> None:
        super().__init__()
        self.games = games

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.games.set_steam_key(str(self.key.value))
        await interaction.response.send_message(
            "Saved (encrypted). Everyone can now link Steam with `/games steam`.", ephemeral=True
        )


class GamesGroup(app_commands.Group):
    """Game tracking commands."""

    def __init__(self, games: GameTracker) -> None:
        super().__init__(name="games", description="Track what everyone plays, playtime and leaderboards")
        self.games = games

    async def _game_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        rows = await self.games.store.fetch_all(
            "SELECT g.key, g.name FROM game g LEFT JOIN game_session s ON s.game_key = g.key WHERE lower(g.name) LIKE ? "
            "GROUP BY g.key ORDER BY COALESCE(SUM(s.seconds), 0) DESC LIMIT 20",
            (f"%{current.lower()}%",),
        )
        return [app_commands.Choice(name=r["name"][:100], value=r["key"]) for r in rows]

    @app_commands.command(name="profile", description="Someone's games: playing now, playtime, top games, Steam")
    @app_commands.describe(user="Whose profile (you by default)")
    async def profile(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        member = user or interaction.user
        g = self.games
        if await g.opted_out(member.id):
            await interaction.response.send_message(f"{member.display_name} has game tracking turned off.", ephemeral=True)
            return
        await interaction.response.defer()
        embed = discord.Embed(color=GAME_COLOR)
        embed.set_author(name=f"{member.display_name}'s games", icon_url=member.display_avatar.url)
        open_row = await g.store.fetch_one("SELECT * FROM game_open WHERE user_id = ?", (member.id,))
        lines = []
        if open_row:
            now_game = await g.game(open_row["game_key"])
            if now_game:
                lines.append(f"🎮 **Playing {now_game.name}** · since <t:{int(open_row['started_at'])}:R>")
                if now_game.cover:
                    embed.set_thumbnail(url=now_game.cover)
        totals = {}
        for period in ("week", "month", "all"):
            row = await g.store.fetch_one(
                "SELECT COALESCE(SUM(seconds), 0) AS s FROM game_session WHERE user_id = ? AND ended_at >= ?", (member.id, g.since(period))
            )
            totals[period] = row["s"] if row else 0
        streak = await g.streak(member.id)
        lines.append(f"🕒 **{hours(totals['week'])}** this week · {hours(totals['month'])} this month · {hours(totals['all'])} total")
        if streak:
            lines.append(f"🔥 {streak}-day streak")
        embed.description = "\n".join(lines)
        top = await g.user_games(member.id, "all", 5)
        if top:
            embed.add_field(
                name="Most played (tracked here)",
                value="\n".join(f"{MEDALS[i] if i < 3 else '▫️'} **{r['name']}** · {hours(r['seconds'])}" for i, r in enumerate(top)),
                inline=False,
            )
            if not open_row and top[0]["cover"]:
                embed.set_thumbnail(url=top[0]["cover"])
        recent = await g.store.fetch_all(
            "SELECT s.*, g.name FROM game_session s JOIN game g ON g.key = s.game_key WHERE s.user_id = ? ORDER BY s.ended_at DESC LIMIT 5",
            (member.id,),
        )
        if recent:
            embed.add_field(
                name="Recent sessions",
                value="\n".join(f"• {r['name']} · {clock(r['seconds'])} · <t:{int(r['ended_at'])}:R>" for r in recent),
                inline=False,
            )
        link = await g.store.fetch_one("SELECT * FROM steam_link WHERE user_id = ?", (member.id,))
        if link:
            lib = await g.store.fetch_all(
                "SELECT name, minutes FROM steam_library WHERE user_id = ? ORDER BY minutes DESC LIMIT 5", (member.id,)
            )
            total = await g.store.fetch_one(
                "SELECT COUNT(*) AS n, COALESCE(SUM(minutes), 0) AS m FROM steam_library WHERE user_id = ?", (member.id,)
            )
            value = f"[{link['persona'] or 'Steam profile'}](https://steamcommunity.com/profiles/{link['steam_id']}) · " \
                    f"{total['n']} games · {hours(total['m'] * 60)} lifetime"
            if lib:
                value += "\n" + "\n".join(f"▫️ {r['name']} · {hours(r['minutes'] * 60)}" for r in lib if r["minutes"])
            embed.add_field(name="Steam", value=value[:1024], inline=False)
        if not top and not open_row and not link:
            embed.description += "\n\nNothing tracked yet. Play something with Discord showing your game, or link Steam with `/games steam`."
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="now", description="Who's playing what right now")
    async def now(self, interaction: discord.Interaction) -> None:
        rows = await self.games.store.fetch_all(
            "SELECT o.*, g.name FROM game_open o JOIN game g ON g.key = o.game_key ORDER BY o.started_at"
        )
        if not rows:
            await interaction.response.send_message("Nobody's playing anything right now.")
            return
        lines = [f"🎮 **{self.games._name(r['user_id'])}** · {r['name']} · <t:{int(r['started_at'])}:R>" for r in rows]
        await interaction.response.send_message(embed=discord.Embed(title="Playing now", description="\n".join(lines[:25]), color=GAME_COLOR))

    @app_commands.command(name="leaderboard", description="Who played the most")
    @app_commands.describe(period="Which stretch of time (this week by default)")
    @app_commands.choices(period=PERIODS)
    async def leaderboard(self, interaction: discord.Interaction, period: app_commands.Choice[str] | None = None) -> None:
        p = period.value if period else "week"
        rows = await self.games.leaderboard(p)
        if not rows:
            await interaction.response.send_message(f"No playtime tracked {PERIOD_NAMES[p]} yet.")
            return
        lines = [
            f"{MEDALS[i] if i < 3 else f'`{i + 1}.`'} **{self.games._name(r['user_id'])}** · {hours(r['seconds'])} · {r['games']} game{'s' if r['games'] != 1 else ''}"
            for i, r in enumerate(rows)
        ]
        embed = discord.Embed(title=f"🏆 Most played · {PERIOD_NAMES[p]}", description="\n".join(lines), color=GAME_COLOR)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="top", description="The server's most played games")
    @app_commands.describe(period="Which stretch of time (this month by default)")
    @app_commands.choices(period=PERIODS)
    async def top(self, interaction: discord.Interaction, period: app_commands.Choice[str] | None = None) -> None:
        p = period.value if period else "month"
        rows = await self.games.top_games(p)
        if not rows:
            await interaction.response.send_message(f"No games tracked {PERIOD_NAMES[p]} yet.")
            return
        lines = [
            f"{MEDALS[i] if i < 3 else f'`{i + 1}.`'} **{r['name']}** · {hours(r['seconds'])} · {r['players']} player{'s' if r['players'] != 1 else ''}"
            for i, r in enumerate(rows)
        ]
        embed = discord.Embed(title=f"🎮 Top games · {PERIOD_NAMES[p]}", description="\n".join(lines), color=GAME_COLOR)
        if rows[0]["cover"]:
            embed.set_thumbnail(url=rows[0]["cover"])
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="game", description="One game: who here plays it and for how long")
    @app_commands.describe(name="Start typing a game's name")
    async def game(self, interaction: discord.Interaction, name: str) -> None:
        g = self.games
        game = await g.find_game(name)
        if game is None:
            await interaction.response.send_message("Nobody here has played that yet.", ephemeral=True)
            return
        rows = await g.store.fetch_all(
            "SELECT user_id, SUM(seconds) AS seconds, MAX(ended_at) AS last FROM game_session WHERE game_key = ? "
            "AND user_id NOT IN (SELECT user_id FROM game_optout) GROUP BY user_id ORDER BY seconds DESC LIMIT 15",
            (game.key,),
        )
        playing = await g.store.fetch_all("SELECT user_id FROM game_open WHERE game_key = ?", (game.key,))
        embed = discord.Embed(title=game.name, color=GAME_COLOR)
        if game.steam_appid:
            embed.url = f"https://store.steampowered.com/app/{game.steam_appid}"
            embed.set_image(url=_steam_header(game.steam_appid))
        elif game.cover:
            embed.set_thumbnail(url=game.cover)
        total = sum(r["seconds"] for r in rows)
        lines = [f"🕒 **{hours(total)}** played here by {len(rows)} {'person' if len(rows) == 1 else 'people'}"]
        if playing:
            lines.append("🎮 Playing now: " + ", ".join(g._name(r["user_id"]) for r in playing))
        if game.themes:
            lines.append(f"🏷️ {game.themes}")
        embed.description = "\n".join(lines)
        if rows:
            embed.add_field(
                name="Players",
                value="\n".join(f"{MEDALS[i] if i < 3 else '▫️'} **{g._name(r['user_id'])}** · {hours(r['seconds'])} · last <t:{int(r['last'])}:R>" for i, r in enumerate(rows)),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    @game.autocomplete("name")
    async def _game_name(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._game_autocomplete(interaction, current)

    @app_commands.command(name="steam", description="Link your Steam profile (lifetime hours, and what you play)")
    @app_commands.describe(profile="Your Steam profile link, SteamID or custom URL name. Leave empty to unlink.")
    async def steam(self, interaction: discord.Interaction, profile: str | None = None) -> None:
        g = self.games
        if not profile:
            removed = await g.store.execute("DELETE FROM steam_link WHERE user_id = ?", (interaction.user.id,))
            await g.store.execute("DELETE FROM steam_library WHERE user_id = ?", (interaction.user.id,))
            await interaction.response.send_message(
                "Steam unlinked." if removed else "Paste your Steam profile link to link it: `/games steam profile:https://steamcommunity.com/id/…`",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            player = await g.link_steam(interaction.user.id, profile)
        except SteamError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        count = await g.store.fetch_one("SELECT COUNT(*) AS n, COALESCE(SUM(minutes), 0) AS m FROM steam_library WHERE user_id = ?", (interaction.user.id,))
        note = "" if count["n"] else "\nYour game list looks empty: set **Game details** to Public in Steam's privacy settings."
        await interaction.followup.send(
            f"Linked **{player.get('personaname')}**: {count['n']} games, {hours(count['m'] * 60)} lifetime.{note}", ephemeral=True
        )

    @app_commands.command(name="epic", description="About Epic Games tracking")
    async def epic(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Epic Games doesn't offer a public way for apps to read your library or playtime, so there's nothing to link. "
            "Epic games are still tracked: when Discord shows you playing one, it counts like any other game. "
            "Make sure **Settings → Activity Privacy → Share your detected activities** is on in Discord.",
            ephemeral=True,
        )

    @app_commands.command(name="privacy", description="Turn game tracking for yourself on or off")
    @app_commands.choices(tracking=[app_commands.Choice(name="On", value="on"), app_commands.Choice(name="Off (and hide my playtime)", value="off")])
    async def privacy(self, interaction: discord.Interaction, tracking: app_commands.Choice[str]) -> None:
        g = self.games
        if tracking.value == "off":
            await g.store.execute("INSERT OR IGNORE INTO game_optout VALUES (?, ?)", (interaction.user.id, time.time()))
            row = await g.store.fetch_one("SELECT * FROM game_open WHERE user_id = ?", (interaction.user.id,))
            if row:
                await g.store.execute("DELETE FROM game_open WHERE user_id = ?", (interaction.user.id,))
            await interaction.response.send_message("Game tracking is off for you. Your playtime is hidden from leaderboards.", ephemeral=True)
        else:
            await g.store.execute("DELETE FROM game_optout WHERE user_id = ?", (interaction.user.id,))
            await interaction.response.send_message("Game tracking is on for you.", ephemeral=True)

    @app_commands.command(name="setup", description="Pick the games channel (Manage Server)")
    @app_commands.describe(channel="Where game posts go", start_posts="Also post when someone starts playing (updated live)")
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel, start_posts: bool = False) -> None:
        perms = getattr(interaction.user, "guild_permissions", None)
        if not (interaction.user.id == self.games.client.config.owner_id or (perms and perms.manage_guild)):
            await interaction.response.send_message("You need Manage Server to do that.", ephemeral=True)
            return
        await self.games.store.kv_set(CHANNEL_KEY, str(channel.id))
        await self.games.store.kv_set(START_POSTS_KEY, "1" if start_posts else "0")
        await interaction.response.send_message(
            f"Game posts will go to {channel.mention}"
            + (" (live \"is playing\" posts, updated when they stop)." if start_posts else " (when someone finishes a session of 10+ minutes)."),
            ephemeral=True,
        )

    @app_commands.command(name="steamkey", description="Add the Steam Web API key (bot owner)")
    async def steamkey(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.games.client.config.owner_id:
            await interaction.response.send_message("Only the bot owner can do that.", ephemeral=True)
            return
        await interaction.response.send_modal(SteamKeyModal(self.games))
