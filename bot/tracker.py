"""TV and movie tracking: a personal Stremio addon per person, activity posts, and friend features."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import secrets
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from .meta import MetaClient, Title, split_video_id
from .store import Store

if TYPE_CHECKING:
    from .discord_app import ArchiveClient

log = logging.getLogger(__name__)

CHANNEL_KEY = "tracker_channel"
RECAP_KEY = "tracker_last_recap"
REPEAT_WINDOW = 6 * 3600  # The same episode again within this window is the same watch.
NOW_WINDOW = 3 * 3600
COLOR = 0x8B5CF6
NO_PINGS = discord.AllowedMentions.none()


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split()) if "\n" not in text else text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _stars(score: float) -> str:
    return f"{score:.1f}/10" if score % 1 else f"{int(score)}/10"


def episode_label(season: int | None, episode: int | None) -> str:
    return f"S{season}E{episode}" if season is not None and episode is not None else ""


def title_value(title: Title) -> str:
    return f"{title.type}:{title.id}"


def parse_title_value(value: str) -> tuple[str, str] | None:
    kind, _, title_id = value.partition(":")
    if kind in ("movie", "series") and title_id:
        return kind, title_id
    return None


class Tracker:
    def __init__(self, client: "ArchiveClient", store: Store, meta: MetaClient) -> None:
        self.client = client
        self.store = store
        self.meta = meta

    @property
    def public_url(self) -> str:
        return self.client.config.public_url

    # ---- accounts ----------------------------------------------------------

    async def token_for(self, user_id: int, *, rotate: bool = False) -> str:
        row = await self.store.fetch_one("SELECT token FROM tracker_user WHERE user_id = ?", (user_id,))
        if row and not rotate:
            return row["token"]
        token = secrets.token_urlsafe(18)
        await self.store.execute(
            "INSERT INTO tracker_user (user_id, token, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET token = excluded.token",
            (user_id, token, time.time()),
        )
        return token

    async def user_for_token(self, token: str) -> int | None:
        row = await self.store.fetch_one("SELECT user_id FROM tracker_user WHERE token = ?", (token,))
        return None if row is None else int(row["user_id"])

    async def setting(self, user_id: int, name: str) -> int:
        row = await self.store.fetch_one(f"SELECT {name} FROM tracker_user WHERE user_id = ?", (user_id,))
        return int(row[name]) if row else (1 if name == "pings" else 0)

    def addon_url(self, token: str) -> str:
        return f"{self.public_url}/stremio/{token}/manifest.json"

    def install_page(self, token: str) -> str:
        return f"{self.public_url}/stremio/{token}/install"

    async def channel(self) -> discord.abc.Messageable | None:
        raw = await self.store.kv_get(CHANNEL_KEY)
        if not raw:
            return None
        channel = self.client.get_channel(int(raw))
        return channel if isinstance(channel, discord.abc.Messageable) else None

    def member_name(self, user_id: int) -> str:
        guild = self.client.get_guild(self.client.config.guild_id)
        member = guild.get_member(user_id) if guild else None
        return member.display_name if member else "Someone"

    def member_avatar(self, user_id: int) -> str | None:
        guild = self.client.get_guild(self.client.config.guild_id)
        member = guild.get_member(user_id) if guild else None
        return member.display_avatar.url if member else None

    # ---- plays from the addon -----------------------------------------------

    async def handle_play(self, token: str, kind: str, video_id: str) -> bool:
        """Called when someone's Stremio starts a video. Returns False for unknown links."""
        user_id = await self.user_for_token(token)
        if user_id is None:
            return False
        title_id, season, number = split_video_id(video_id)
        if not title_id or not re.fullmatch(r"(tt\d+|kitsu:\d+)", title_id):
            return True
        recent = await self.store.fetch_one(
            "SELECT 1 FROM watch_event WHERE user_id = ? AND video_id = ? AND watched_at > ?",
            (user_id, video_id, time.time() - REPEAT_WINDOW),
        )
        if recent:
            return True
        kind = "series" if kind == "series" or season is not None else "movie"
        await self.store.execute(
            "INSERT INTO watch_event (user_id, title_id, video_id, type, season, episode, watched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, title_id, video_id, kind, season, number, time.time()),
        )
        if kind == "series":
            await self.store.execute(
                "INSERT INTO followed_show (user_id, title_id, last_seen_episode) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, title_id) DO UPDATE SET last_seen_episode = excluded.last_seen_episode",
                (user_id, title_id, video_id),
            )
        self.client._spawn(self._post_play(user_id, kind, title_id, season, number))
        return True

    async def _post_play(self, user_id: int, kind: str, title_id: str, season: int | None, number: int | None) -> None:
        if await self.setting(user_id, "private"):
            return
        channel = await self.channel()
        if channel is None:
            return
        title = await self.meta.title(kind, title_id)
        if title is None:
            return
        embed = await self.watch_embed(user_id, title, season, number)
        await channel.send(embed=embed, view=self.title_buttons(title), allowed_mentions=NO_PINGS)

    async def watch_embed(self, user_id: int, title: Title, season: int | None, number: int | None) -> discord.Embed:
        episode = title.episode(season, number)
        label = episode_label(season, number)
        heading = title.name + (f" · {label}" if label else "")
        if episode:
            heading += f" · {episode.name}"
        embed = discord.Embed(title=_clip(heading, 250), url=title.url, color=COLOR)
        embed.set_author(name=f"{self.member_name(user_id)} is watching", icon_url=self.member_avatar(user_id))
        about = episode.overview if episode and episode.overview else title.description
        if about:
            embed.description = _clip(about, 350)
        if title.poster:
            embed.set_thumbnail(url=title.poster)
        picture = (episode.thumbnail if episode else None) or title.background
        if picture:
            embed.set_image(url=picture)
        await self._add_facts(embed, title, exclude=user_id)
        embed.set_footer(text="Stremio · /watch rate to review")
        return embed

    async def _add_facts(self, embed: discord.Embed, title: Title, exclude: int | None = None) -> None:
        facts = [f for f in (title.year, " · ".join(title.genres[:3]), title.runtime) if f]
        if facts:
            embed.add_field(name="Info", value=" | ".join(facts), inline=False)
        if title.imdb_rating:
            embed.add_field(name="IMDb", value=f"⭐ {title.imdb_rating}", inline=True)
        friend_avg = await self.store.fetch_one(
            "SELECT AVG(score) AS avg, COUNT(*) AS n FROM rating WHERE title_id = ?", (title.id,)
        )
        if friend_avg and friend_avg["n"]:
            embed.add_field(name="Friends rate it", value=f"{_stars(round(friend_avg['avg'], 1))} ({friend_avg['n']})", inline=True)
        watchers = await self.store.fetch_all(
            "SELECT user_id, MAX(watched_at) AS last FROM watch_event WHERE title_id = ? "
            "GROUP BY user_id ORDER BY last DESC LIMIT 8",
            (title.id,),
        )
        others = [f"<@{row['user_id']}>" for row in watchers if row["user_id"] != exclude]
        if others:
            embed.add_field(name="Also watched by", value=", ".join(others), inline=False)
        if title.cast:
            embed.add_field(name="Starring", value=", ".join(title.cast[:4]), inline=False)

    def title_buttons(self, title: Title) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        for action in ("watchlist", "rate", "rec"):
            view.add_item(TitleButton(action, title.type, title.id))
        if title.trailer:
            view.add_item(discord.ui.Button(label="Trailer", url=title.trailer))
        return view

    def card(self, title: Title, note: str | None = None) -> discord.Embed:
        """A compact details card for lists: poster, year, genres, rating and a short summary."""
        icon = "🎬" if title.type == "movie" else "📺"
        embed = discord.Embed(title=_clip(f"{icon} {title.name}", 250), url=title.url, color=COLOR)
        facts = [f for f in (title.year, " · ".join(title.genres[:3]), title.runtime) if f]
        if title.imdb_rating:
            facts.append(f"⭐ {title.imdb_rating}")
        parts = []
        if facts:
            parts.append("-# " + " | ".join(facts))
        if note:
            parts.append(note)
        if title.description:
            parts.append(_clip(title.description, 160))
        embed.description = "\n".join(parts) or None
        if title.poster:
            embed.set_thumbnail(url=title.poster)
        return embed

    async def title_embed(self, title: Title) -> discord.Embed:
        embed = discord.Embed(title=_clip(title.name, 250), url=title.url, color=COLOR)
        if title.description:
            embed.description = _clip(title.description, 600)
        if title.poster:
            embed.set_thumbnail(url=title.poster)
        if title.background:
            embed.set_image(url=title.background)
        await self._add_facts(embed, title)
        reviews = await self.store.fetch_all(
            "SELECT user_id, score, review FROM rating WHERE title_id = ? AND review IS NOT NULL AND review != '' "
            "ORDER BY updated_at DESC LIMIT 3",
            (title.id,),
        )
        for row in reviews:
            embed.add_field(
                name=f"{self.member_name(row['user_id'])} · {_stars(row['score'])}",
                value=f"||{_clip(row['review'], 300)}||",
                inline=False,
            )
        wanted = await self.store.fetch_one("SELECT COUNT(*) AS n FROM watchlist WHERE title_id = ?", (title.id,))
        if wanted and wanted["n"]:
            embed.set_footer(text=f"On {wanted['n']} watchlist(s) here")
        return embed

    # ---- shared actions ------------------------------------------------------

    async def add_to_watchlist(self, user_id: int, kind: str, title_id: str) -> bool:
        changed = await self.store.execute(
            "INSERT OR IGNORE INTO watchlist (user_id, title_id, type, added_at) VALUES (?, ?, ?, ?)",
            (user_id, title_id, kind, time.time()),
        )
        return bool(changed)

    async def rate(self, user_id: int, kind: str, title_id: str, score: int, review: str | None) -> None:
        await self.store.execute(
            "INSERT INTO rating (user_id, title_id, type, score, review, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, title_id) DO UPDATE SET score = excluded.score, review = excluded.review, "
            "updated_at = excluded.updated_at",
            (user_id, title_id, kind, score, review or None, time.time()),
        )
        await self.store.execute("DELETE FROM watchlist WHERE user_id = ? AND title_id = ?", (user_id, title_id))

    async def recommend(
        self, from_user: int, to_user: int, kind: str, title_id: str, note: str | None, fallback: Any = None
    ) -> str:
        if from_user == to_user:
            return "You can't recommend something to yourself."
        title = await self.meta.title(kind, title_id)
        if title is None:
            return "I couldn't find that title."
        await self.store.execute(
            "INSERT INTO recommendation (from_user, to_user, title_id, type, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (from_user, to_user, title.id, title.type, note or None, time.time()),
        )
        channel = await self.channel() or fallback
        if channel is None:
            return "Saved, but no tracker channel is set up, so nobody was pinged. Ask the owner to run /tracker setup."
        embed = await self.title_embed(title)
        embed.set_author(name=f"{self.member_name(from_user)} recommends", icon_url=self.member_avatar(from_user))
        if note:
            embed.insert_field_at(0, name="Why", value=_clip(note, 500), inline=False)
        ping = bool(await self.setting(to_user, "pings"))
        await channel.send(
            content=f"<@{to_user}>, <@{from_user}> thinks you should watch **{title.name}**",
            embed=embed,
            view=self.title_buttons(title),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(to_user)] if ping else []),
        )
        return f"Sent **{title.name}** to <@{to_user}>. It also shows up in their Stremio under *From friends*."

    async def resolve(self, value: str) -> Title | None:
        parsed = parse_title_value(value)
        if parsed:
            return await self.meta.title(*parsed)
        return await self.meta.best_match(value)

    async def autocomplete(self, current: str) -> list[app_commands.Choice[str]]:
        results = await self.meta.search(current, limit=12)
        choices = []
        for title in results:
            icon = "🎬" if title.type == "movie" else "📺"
            name = _clip(f"{icon} {title.name} ({title.year})" if title.year else f"{icon} {title.name}", 100)
            choices.append(app_commands.Choice(name=name, value=title_value(title)))
        return choices[:25]

    # ---- Stremio addon ---------------------------------------------------------

    def manifest(self, token: str) -> dict[str, Any]:
        catalogs = []
        for kind, word in (("movie", "Movies"), ("series", "Shows")):
            catalogs += [
                {"type": kind, "id": "friends-recs", "name": f"From friends · {word}"},
                {"type": kind, "id": "watchlist", "name": f"My watchlist · {word}"},
                {"type": kind, "id": "friends-watching", "name": f"Friends are watching · {word}"},
            ]
        return {
            "id": f"community.discord.tracker.{token[:8].lower()}",
            "version": "1.0.0",
            "name": "Discord Watch Tracker",
            "description": "Shares what you watch with your Discord server, and shows your friends' recommendations.",
            "resources": ["subtitles", "catalog"],
            "types": ["movie", "series"],
            "idPrefixes": ["tt", "kitsu"],
            "catalogs": catalogs,
            "behaviorHints": {"configurable": False, "configurationRequired": False},
        }

    async def catalog(self, token: str, kind: str, catalog_id: str) -> list[dict[str, Any]] | None:
        user_id = await self.user_for_token(token)
        if user_id is None:
            return None
        if catalog_id == "watchlist":
            rows = await self.store.fetch_all(
                "SELECT title_id FROM watchlist WHERE user_id = ? AND type = ? ORDER BY added_at DESC LIMIT 60",
                (user_id, kind),
            )
        elif catalog_id == "friends-recs":
            rows = await self.store.fetch_all(
                "SELECT title_id, MAX(created_at) AS t FROM recommendation WHERE to_user = ? AND type = ? "
                "GROUP BY title_id ORDER BY t DESC LIMIT 60",
                (user_id, kind),
            )
        elif catalog_id == "friends-watching":
            rows = await self.store.fetch_all(
                "SELECT w.title_id, MAX(w.watched_at) AS t FROM watch_event w "
                "JOIN tracker_user u ON u.user_id = w.user_id "
                "WHERE w.user_id != ? AND w.type = ? AND u.private = 0 GROUP BY w.title_id ORDER BY t DESC LIMIT 60",
                (user_id, kind),
            )
        else:
            return []
        metas = []
        for row in rows:
            title = await self.meta.title(kind, row["title_id"])
            if title:
                metas.append({
                    "id": title.id, "type": kind, "name": title.name,
                    "poster": title.poster, "background": title.background,
                    "description": _clip(title.description, 300), "releaseInfo": title.year,
                })
        return metas

    # ---- background jobs -------------------------------------------------------

    async def check_new_episodes(self) -> None:
        shows = await self.store.fetch_all("SELECT DISTINCT title_id FROM followed_show")
        now = datetime.now(timezone.utc)
        for row in shows:
            title_id = row["title_id"]
            title = await self.meta.title("series", title_id)
            if title is None:
                continue
            released = [e for e in title.episodes if e.released and e.released <= now]
            known = {r["video_id"] for r in await self.store.fetch_all(
                "SELECT video_id FROM episode_alert WHERE title_id = ?", (title_id,))}
            fresh = [e for e in released if e.id not in known]
            if not fresh:
                continue
            async with self.store.transaction() as db:
                await db.executemany(
                    "INSERT OR IGNORE INTO episode_alert (title_id, video_id) VALUES (?, ?)",
                    [(title_id, e.id) for e in fresh],
                )
            if not known:
                continue  # First look at this show: remember what's out, don't announce old episodes.
            recent = [e for e in fresh if now - e.released <= timedelta(days=3)]
            if recent:
                await self._announce_episodes(title, recent)

    async def _announce_episodes(self, title: Title, episodes: list[Any]) -> None:
        channel = await self.channel()
        if channel is None:
            return
        rows = await self.store.fetch_all(
            "SELECT f.user_id FROM followed_show f LEFT JOIN tracker_user u ON u.user_id = f.user_id "
            "WHERE f.title_id = ? AND COALESCE(u.pings, 1) = 1",
            (title.id,),
        )
        fans = [row["user_id"] for row in rows]
        latest = episodes[-1]
        embed = discord.Embed(
            title=_clip(f"{title.name} · {episode_label(latest.season, latest.number)} · {latest.name}", 250),
            url=title.url, color=COLOR,
            description=_clip(latest.overview or title.description, 350) or None,
        )
        embed.set_author(name="New episode out" if len(episodes) == 1 else f"{len(episodes)} new episodes out")
        if title.poster:
            embed.set_thumbnail(url=title.poster)
        if latest.thumbnail or title.background:
            embed.set_image(url=latest.thumbnail or title.background)
        mentions = " ".join(f"<@{uid}>" for uid in fans)
        await channel.send(
            content=f"{mentions} new episode of **{title.name}**" if mentions else None,
            embed=embed,
            view=self.title_buttons(title),
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(uid) for uid in fans]),
        )

    async def close_movie_nights(self) -> None:
        rows = await self.store.fetch_all(
            "SELECT message_id, channel_id, options FROM movie_night WHERE closed = 0 AND ends_at <= ?", (time.time(),)
        )
        for row in rows:
            await self.store.execute("UPDATE movie_night SET closed = 1 WHERE message_id = ?", (row["message_id"],))
            options = json.loads(row["options"])
            votes = await self.store.fetch_all(
                "SELECT choice, COUNT(*) AS n FROM movie_night_vote WHERE message_id = ? GROUP BY choice", (row["message_id"],)
            )
            voters = [r["user_id"] for r in await self.store.fetch_all(
                "SELECT user_id FROM movie_night_vote WHERE message_id = ?", (row["message_id"],))]
            channel = self.client.get_channel(row["channel_id"])
            if channel is None:
                continue
            if not votes:
                await channel.send("Movie night poll closed with no votes.")
                continue
            top = max(r["n"] for r in votes)
            winners = [r["choice"] for r in votes if r["n"] == top]
            choice = random.choice(winners)
            pick = options[choice]
            title = await self.meta.title(pick["type"], pick["id"])
            embed = await self.title_embed(title) if title else None
            if embed:
                embed.set_author(name=f"Movie night pick · {top} vote(s)")
            await channel.send(
                content="🍿 " + " ".join(f"<@{uid}>" for uid in voters) + f" movie night is **{pick['name']}**",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(uid) for uid in voters]),
            )
            try:
                message = await channel.fetch_message(row["message_id"])
                await message.edit(view=None)
            except discord.HTTPException:
                pass

    async def weekly_recap(self, force: bool = False) -> None:
        zone = self.client.config.timezone
        now = datetime.now(zone)
        week = now.strftime("%G-%V")
        if not force and (now.weekday() != 6 or now.hour < 18 or await self.store.kv_get(RECAP_KEY) == week):
            return
        channel = await self.channel()
        if channel is None:
            return
        await self.store.kv_set(RECAP_KEY, week)
        since = time.time() - 7 * 86400
        rows = await self.store.fetch_all(
            "SELECT w.user_id, w.title_id, w.type FROM watch_event w LEFT JOIN tracker_user u ON u.user_id = w.user_id "
            "WHERE w.watched_at > ? AND COALESCE(u.private, 0) = 0", (since,)
        )
        if not rows:
            return
        by_title = Counter((r["type"], r["title_id"]) for r in rows)
        by_viewer = Counter(r["user_id"] for r in rows)
        episodes = sum(1 for r in rows if r["type"] == "series")
        movies = sum(1 for r in rows if r["type"] == "movie")
        embed = discord.Embed(title="📊 This week in watching", color=COLOR)
        embed.description = f"**{movies}** movie(s) and **{episodes}** episode(s) across the server."
        lines = []
        top_poster = None
        for (kind, title_id), count in by_title.most_common(5):
            title = await self.meta.title(kind, title_id)
            if title:
                top_poster = top_poster or title.poster
                lines.append(f"**{title.name}** · {count} play(s)")
        if lines:
            embed.add_field(name="Most watched", value="\n".join(lines), inline=False)
        embed.add_field(
            name="Biggest watchers",
            value="\n".join(f"<@{uid}> · {n}" for uid, n in by_viewer.most_common(3)),
            inline=False,
        )
        if top_poster:
            embed.set_thumbnail(url=top_poster)
        await channel.send(embed=embed, allowed_mentions=NO_PINGS)

    async def run_jobs(self) -> None:
        await self.client.wait_until_ready()
        last_episode_check = 0.0
        while not self.client.is_closed():
            try:
                await self.close_movie_nights()
                await self.weekly_recap()
                if time.time() - last_episode_check > 3 * 3600:
                    last_episode_check = time.time()
                    await self.check_new_episodes()
            except Exception:
                log.exception("Tracker job failed")
            await asyncio.sleep(self.client.tracker_job_seconds)


# ---- buttons and dialogs -----------------------------------------------------------


class TitleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"trk:(?P<action>watchlist|rate|rec):(?P<kind>movie|series):(?P<title>.+)"):
    LABELS = {"watchlist": ("Add to watchlist", "📌"), "rate": ("Rate", "⭐"), "rec": ("Recommend", "📨")}

    def __init__(self, action: str, kind: str, title_id: str) -> None:
        label, emoji = self.LABELS[action]
        super().__init__(discord.ui.Button(
            label=label, emoji=emoji, style=discord.ButtonStyle.secondary,
            custom_id=f"trk:{action}:{kind}:{title_id}"[:100],
        ))
        self.action, self.kind, self.title_id = action, kind, title_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /) -> "TitleButton":
        return cls(match["action"], match["kind"], match["title"])

    async def callback(self, interaction: discord.Interaction) -> None:
        tracker: Tracker = interaction.client.tracker  # type: ignore[attr-defined]
        if self.action == "watchlist":
            added = await tracker.add_to_watchlist(interaction.user.id, self.kind, self.title_id)
            await interaction.response.send_message(
                "Added to your watchlist. It's in Stremio under *My watchlist* too." if added else "Already on your watchlist.",
                ephemeral=True,
            )
        elif self.action == "rate":
            await interaction.response.send_modal(RateModal(self.kind, self.title_id))
        else:
            await interaction.response.send_message(
                "Who should watch it?", view=RecommendPicker(self.kind, self.title_id), ephemeral=True
            )


class RateModal(discord.ui.Modal, title="Rate it"):
    score = discord.ui.TextInput(label="Score from 1 to 10", max_length=2, placeholder="8")
    review = discord.ui.TextInput(
        label="Review (optional, shown as a spoiler)", style=discord.TextStyle.paragraph, required=False, max_length=1000
    )

    def __init__(self, kind: str, title_id: str) -> None:
        super().__init__()
        self.kind, self.title_id = kind, title_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tracker: Tracker = interaction.client.tracker  # type: ignore[attr-defined]
        try:
            score = int(str(self.score.value).strip())
        except ValueError:
            score = 0
        if not 1 <= score <= 10:
            await interaction.response.send_message("The score has to be a number from 1 to 10.", ephemeral=True)
            return
        await tracker.rate(interaction.user.id, self.kind, self.title_id, score, str(self.review.value).strip())
        title = await tracker.meta.title(self.kind, self.title_id)
        name = title.name if title else "it"
        await interaction.response.send_message(
            f"<@{interaction.user.id}> rated **{name}** {_stars(score)}"
            + (f"\n> ||{_clip(str(self.review.value), 300)}||" if str(self.review.value).strip() else ""),
            allowed_mentions=NO_PINGS,
        )


class RecommendPicker(discord.ui.View):
    def __init__(self, kind: str, title_id: str) -> None:
        super().__init__(timeout=300)
        self.kind, self.title_id = kind, title_id

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Pick a friend", min_values=1, max_values=1)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        tracker: Tracker = interaction.client.tracker  # type: ignore[attr-defined]
        target = select.values[0]
        result = await tracker.recommend(interaction.user.id, target.id, self.kind, self.title_id, None, interaction.channel)
        await interaction.response.edit_message(content=result, view=None)


class VoteButton(discord.ui.DynamicItem[discord.ui.Button], template=r"trk:vote:(?P<choice>[0-9])"):
    def __init__(self, choice: int, label: str = "") -> None:
        super().__init__(discord.ui.Button(
            label=_clip(label, 70) or f"Option {choice + 1}", emoji=f"{choice + 1}\N{COMBINING ENCLOSING KEYCAP}",
            style=discord.ButtonStyle.primary, custom_id=f"trk:vote:{choice}",
        ))
        self.choice = choice

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item[Any], match: re.Match[str], /) -> "VoteButton":
        return cls(int(match["choice"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        tracker: Tracker = interaction.client.tracker  # type: ignore[attr-defined]
        poll = await tracker.store.fetch_one(
            "SELECT closed FROM movie_night WHERE message_id = ?", (interaction.message.id,)
        )
        if poll is None or poll["closed"]:
            await interaction.response.send_message("This poll has closed.", ephemeral=True)
            return
        await tracker.store.execute(
            "INSERT INTO movie_night_vote (message_id, user_id, choice) VALUES (?, ?, ?) "
            "ON CONFLICT(message_id, user_id) DO UPDATE SET choice = excluded.choice",
            (interaction.message.id, interaction.user.id, self.choice),
        )
        await interaction.response.send_message(f"Voted for option {self.choice + 1}.", ephemeral=True)


# ---- slash commands --------------------------------------------------------------------


def _is_admin(interaction: discord.Interaction, owner_id: int) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return interaction.user.id == owner_id or bool(perms and perms.manage_guild)


class TrackerGroup(app_commands.Group):
    """Setup and your own account."""

    def __init__(self, tracker: Tracker) -> None:
        super().__init__(name="tracker", description="Set up Stremio watch tracking")
        self.tracker = tracker

    @app_commands.command(name="setup", description="Pick the channel where watching activity is posted")
    @app_commands.describe(channel="Where activity, recommendations and new-episode pings go")
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        if not _is_admin(interaction, self.tracker.client.config.owner_id):
            await interaction.response.send_message("You need Manage Server to do that.", ephemeral=True)
            return
        await self.tracker.store.kv_set(CHANNEL_KEY, str(channel.id))
        await interaction.response.send_message(
            f"Watch activity will go to {channel.mention}. Everyone can now run `/tracker link` to connect their Stremio.",
            ephemeral=True,
        )

    @app_commands.command(name="link", description="Get your personal Stremio addon")
    async def link(self, interaction: discord.Interaction) -> None:
        if not self.tracker.public_url:
            await interaction.response.send_message(
                "The bot's public web address isn't set up yet, so Stremio can't reach it. Ask the owner to set PUBLIC_URL.",
                ephemeral=True,
            )
            return
        token = await self.tracker.token_for(interaction.user.id)
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Install in Stremio", url=self.tracker.install_page(token), emoji="🎬"))
        note = "" if await self.tracker.channel() else "\n\nHeads up: no activity channel is set yet (`/tracker setup`)."
        await interaction.response.send_message(
            "**Your personal addon** (keep it to yourself, it's how the bot knows it's you):\n"
            f"`{self.tracker.addon_url(token)}`\n\n"
            "Tap **Install in Stremio**, or in Stremio go to Addons, paste that link in the search bar and install. "
            "From then on, whatever you press play on gets posted, and your watchlist and friends' picks show up on "
            "Stremio's home screen." + note,
            view=view, ephemeral=True,
        )

    @app_commands.command(name="unlink", description="Turn off your addon link (makes a new one next time)")
    async def unlink(self, interaction: discord.Interaction) -> None:
        await self.tracker.token_for(interaction.user.id, rotate=True)
        await interaction.response.send_message(
            "Your old addon link no longer works. Remove it from Stremio, and run `/tracker link` for a new one.", ephemeral=True
        )

    @app_commands.command(name="privacy", description="Hide or show your watching in the activity channel")
    @app_commands.choices(mode=[app_commands.Choice(name="Show my activity", value="show"), app_commands.Choice(name="Hide my activity", value="hide")])
    async def privacy(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        await self.tracker.token_for(interaction.user.id)
        await self.tracker.store.execute(
            "UPDATE tracker_user SET private = ? WHERE user_id = ?", (1 if mode.value == "hide" else 0, interaction.user.id)
        )
        text = "Your watching is hidden. It's still counted in your own stats." if mode.value == "hide" else "Your watching will be posted."
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="pings", description="Turn pings for recommendations and new episodes on or off")
    @app_commands.choices(mode=[app_commands.Choice(name="On", value="on"), app_commands.Choice(name="Off", value="off")])
    async def pings(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        await self.tracker.token_for(interaction.user.id)
        await self.tracker.store.execute(
            "UPDATE tracker_user SET pings = ? WHERE user_id = ?", (1 if mode.value == "on" else 0, interaction.user.id)
        )
        await interaction.response.send_message(f"Pings are {mode.value}.", ephemeral=True)


class WatchGroup(app_commands.Group):
    """Friend features."""

    def __init__(self, tracker: Tracker) -> None:
        super().__init__(name="watch", description="Movies and shows with friends")
        self.tracker = tracker

    async def _title(self, interaction: discord.Interaction, value: str) -> Title | None:
        title = await self.tracker.resolve(value)
        if title is None:
            await interaction.followup.send("I couldn't find that. Pick one from the suggestions.", ephemeral=True)
        return title

    async def _title_complete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self.tracker.autocomplete(current)

    @app_commands.command(name="info", description="Details, ratings and which friends watched it")
    async def info(self, interaction: discord.Interaction, title: str) -> None:
        await interaction.response.defer(thinking=True)
        found = await self._title(interaction, title)
        if found:
            await interaction.followup.send(embed=await self.tracker.title_embed(found), view=self.tracker.title_buttons(found))

    @app_commands.command(name="rate", description="Rate and review something you watched")
    @app_commands.describe(score="1 to 10", review="Optional, shown as a spoiler")
    async def rate(self, interaction: discord.Interaction, title: str, score: app_commands.Range[int, 1, 10], review: str | None = None) -> None:
        await interaction.response.defer(thinking=True)
        found = await self._title(interaction, title)
        if not found:
            return
        await self.tracker.rate(interaction.user.id, found.type, found.id, score, review)
        embed = await self.tracker.title_embed(found)
        embed.set_author(name=f"{interaction.user.display_name} rated it {_stars(score)}", icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed, view=self.tracker.title_buttons(found))

    @app_commands.command(name="recommend", description="Recommend something to a friend (pings them)")
    async def recommend(self, interaction: discord.Interaction, title: str, friend: discord.Member, note: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        found = await self._title(interaction, title)
        if found:
            result = await self.tracker.recommend(interaction.user.id, friend.id, found.type, found.id, note, interaction.channel)
            await interaction.followup.send(result, ephemeral=True)

    @app_commands.command(name="add", description="Add something to your watchlist")
    async def add(self, interaction: discord.Interaction, title: str) -> None:
        await interaction.response.defer(thinking=True)
        found = await self._title(interaction, title)
        if found:
            added = await self.tracker.add_to_watchlist(interaction.user.id, found.type, found.id)
            embed = await self.tracker.title_embed(found)
            embed.set_author(
                name=f"{interaction.user.display_name} added this to their watchlist" if added
                else "Already on your watchlist",
                icon_url=interaction.user.display_avatar.url,
            )
            await interaction.followup.send(embed=embed, view=self.tracker.title_buttons(found))

    @app_commands.command(name="remove", description="Take something off your watchlist")
    async def remove(self, interaction: discord.Interaction, title: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        found = await self._title(interaction, title)
        if found:
            removed = await self.tracker.store.execute(
                "DELETE FROM watchlist WHERE user_id = ? AND title_id = ?", (interaction.user.id, found.id)
            )
            await interaction.followup.send(
                f"Took **{found.name}** off your watchlist." if removed else f"**{found.name}** wasn't on your watchlist.",
                ephemeral=True,
            )

    @app_commands.command(name="watchlist", description="See your or a friend's watchlist")
    async def watchlist(self, interaction: discord.Interaction, friend: discord.Member | None = None) -> None:
        await interaction.response.defer(thinking=True)
        who = friend or interaction.user
        rows = await self.tracker.store.fetch_all(
            "SELECT title_id, type FROM watchlist WHERE user_id = ? ORDER BY added_at DESC LIMIT 20", (who.id,)
        )
        if not rows:
            await interaction.followup.send(f"{who.display_name}'s watchlist is empty.")
            return
        titles = [t for t in [await self.tracker.meta.title(r["type"], r["title_id"]) for r in rows] if t]
        embeds = [self.tracker.card(t) for t in titles[:9]]
        if len(titles) > 9:
            more = ", ".join(t.name for t in titles[9:])
            embeds.append(discord.Embed(description=_clip(f"Also: {more}", 1000), color=COLOR))
        await interaction.followup.send(content=f"**{who.display_name}'s watchlist** ({len(titles)})", embeds=embeds)

    @app_commands.command(name="recs", description="Things friends recommended to you")
    async def recs(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await self.tracker.store.fetch_all(
            "SELECT from_user, title_id, type, note FROM recommendation WHERE to_user = ? ORDER BY created_at DESC LIMIT 15",
            (interaction.user.id,),
        )
        if not rows:
            await interaction.followup.send("Nobody has recommended you anything yet.", ephemeral=True)
            return
        embeds = []
        for row in rows[:10]:
            title = await self.tracker.meta.title(row["type"], row["title_id"])
            if title:
                note = f"From <@{row['from_user']}>" + (f": “{_clip(row['note'], 120)}”" if row["note"] else "")
                embeds.append(self.tracker.card(title, note))
        await interaction.followup.send(content="**Recommended to you**", embeds=embeds, ephemeral=True)

    @app_commands.command(name="now", description="What everyone is watching")
    async def now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        rows = await self.tracker.store.fetch_all(
            "SELECT w.user_id, w.title_id, w.type, w.season, w.episode, MAX(w.watched_at) AS t FROM watch_event w "
            "LEFT JOIN tracker_user u ON u.user_id = w.user_id WHERE w.watched_at > ? AND COALESCE(u.private, 0) = 0 "
            "GROUP BY w.user_id ORDER BY t DESC LIMIT 15",
            (time.time() - 86400,),
        )
        if not rows:
            await interaction.followup.send("Nobody has watched anything in the last day.")
            return
        live, earlier = [], []
        for row in rows:
            title = await self.tracker.meta.title(row["type"], row["title_id"])
            name = title.name if title else row["title_id"]
            label = episode_label(row["season"], row["episode"])
            line = f"<@{row['user_id']}> · **{name}**{' ' + label if label else ''} · <t:{int(row['t'])}:R>"
            (live if time.time() - row["t"] < NOW_WINDOW else earlier).append(line)
        embed = discord.Embed(title="What everyone's watching", color=COLOR)
        if live:
            embed.add_field(name="🔴 Now", value="\n".join(live), inline=False)
        if earlier:
            embed.add_field(name="Earlier today", value="\n".join(earlier), inline=False)
        await interaction.followup.send(embed=embed, allowed_mentions=NO_PINGS)

    @app_commands.command(name="history", description="Recently watched by you or a friend")
    async def history(self, interaction: discord.Interaction, friend: discord.Member | None = None) -> None:
        await interaction.response.defer(thinking=True)
        who = friend or interaction.user
        if who.id != interaction.user.id and await self.tracker.setting(who.id, "private"):
            await interaction.followup.send(f"{who.display_name} keeps their watching private.")
            return
        rows = await self.tracker.store.fetch_all(
            "SELECT title_id, type, season, episode, watched_at FROM watch_event WHERE user_id = ? ORDER BY watched_at DESC LIMIT 15",
            (who.id,),
        )
        if not rows:
            await interaction.followup.send(f"Nothing tracked for {who.display_name} yet.")
            return
        lines, poster = [], None
        for row in rows:
            title = await self.tracker.meta.title(row["type"], row["title_id"])
            poster = poster or (title.poster if title else None)
            label = episode_label(row["season"], row["episode"])
            lines.append(f"**{title.name if title else row['title_id']}**{' ' + label if label else ''} · <t:{int(row['watched_at'])}:R>")
        embed = discord.Embed(title=f"{who.display_name} recently watched", description="\n".join(lines), color=COLOR)
        if poster:
            embed.set_thumbnail(url=poster)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="stats", description="Watching stats for you or a friend")
    async def stats(self, interaction: discord.Interaction, friend: discord.Member | None = None) -> None:
        await interaction.response.defer(thinking=True)
        who = friend or interaction.user
        if who.id != interaction.user.id and await self.tracker.setting(who.id, "private"):
            await interaction.followup.send(f"{who.display_name} keeps their watching private.")
            return
        rows = await self.tracker.store.fetch_all(
            "SELECT title_id, type, COUNT(DISTINCT video_id) AS plays FROM watch_event WHERE user_id = ? GROUP BY title_id, type "
            "ORDER BY plays DESC LIMIT 60",
            (who.id,),
        )
        if not rows:
            await interaction.followup.send(f"Nothing tracked for {who.display_name} yet.")
            return
        movies = sum(1 for r in rows if r["type"] == "movie")
        episodes = sum(r["plays"] for r in rows if r["type"] == "series")
        minutes, genres, top_shows = 0, Counter(), []
        for row in rows:
            title = await self.tracker.meta.title(row["type"], row["title_id"])
            if not title:
                continue
            minutes += (title.runtime_minutes or (45 if title.type == "series" else 110)) * row["plays"]
            genres.update(title.genres)
            if title.type == "series" and len(top_shows) < 3:
                top_shows.append(f"{title.name} ({row['plays']} ep)")
        rated = await self.tracker.store.fetch_one("SELECT COUNT(*) AS n, AVG(score) AS avg FROM rating WHERE user_id = ?", (who.id,))
        embed = discord.Embed(title=f"{who.display_name}'s watching", color=COLOR)
        embed.set_thumbnail(url=who.display_avatar.url)
        embed.add_field(name="Movies", value=str(movies))
        embed.add_field(name="Episodes", value=str(episodes))
        embed.add_field(name="Time", value=f"~{minutes // 60}h")
        if genres:
            embed.add_field(name="Top genres", value=", ".join(g for g, _ in genres.most_common(3)), inline=False)
        if top_shows:
            embed.add_field(name="Top shows", value="\n".join(top_shows), inline=False)
        if rated and rated["n"]:
            embed.add_field(name="Ratings", value=f"{rated['n']} rated, average {_stars(round(rated['avg'], 1))}", inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="match", description="How similar your taste is to a friend's")
    async def match(self, interaction: discord.Interaction, friend: discord.Member) -> None:
        await interaction.response.defer(thinking=True)
        me, them = interaction.user.id, friend.id

        async def watched(uid: int) -> set[str]:
            return {r["title_id"] for r in await self.tracker.store.fetch_all(
                "SELECT DISTINCT title_id FROM watch_event WHERE user_id = ? UNION SELECT title_id FROM rating WHERE user_id = ?", (uid, uid))}

        mine, theirs = await watched(me), await watched(them)
        if not mine or not theirs:
            await interaction.followup.send("You both need some watch history or ratings first.")
            return
        shared = mine & theirs
        overlap = len(shared) / len(mine | theirs)
        pairs = await self.tracker.store.fetch_all(
            "SELECT a.score AS a, b.score AS b FROM rating a JOIN rating b ON a.title_id = b.title_id "
            "WHERE a.user_id = ? AND b.user_id = ?", (me, them))
        agreement = 1 - sum(abs(p["a"] - p["b"]) for p in pairs) / (9 * len(pairs)) if pairs else None
        score = round(100 * (0.5 * min(1.0, overlap * 3) + 0.5 * agreement)) if agreement is not None else round(100 * min(1.0, overlap * 3))
        names = []
        for title_id in list(shared)[:5]:
            title = await self.tracker.meta.title("series", title_id)
            if title:
                names.append(title.name)
        embed = discord.Embed(
            title=f"{interaction.user.display_name} × {friend.display_name}: {score}% match", color=COLOR,
            description=f"{len(shared)} title(s) in common" + (f", rated {len(pairs)} the same things" if pairs else ""),
        )
        if names:
            embed.add_field(name="Both watched", value="\n".join(names), inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="movienight", description="Start a vote on what to watch, from everyone's watchlists")
    @app_commands.describe(hours="How long voting stays open")
    async def movienight(self, interaction: discord.Interaction, hours: app_commands.Range[float, 0.1, 48.0] = 2.0) -> None:
        await interaction.response.defer(thinking=True)
        rows = await self.tracker.store.fetch_all(
            "SELECT title_id, type, COUNT(*) AS wants FROM watchlist GROUP BY title_id, type ORDER BY wants DESC, RANDOM() LIMIT 5"
        )
        if len(rows) < 2:
            await interaction.followup.send("I need at least two titles across everyone's watchlists. Use `/watch add` first.")
            return
        options, lines = [], []
        for index, row in enumerate(rows):
            title = await self.tracker.meta.title(row["type"], row["title_id"])
            name = title.name if title else row["title_id"]
            options.append({"id": row["title_id"], "type": row["type"], "name": name})
            lines.append(f"**{index + 1}.** {name} · wanted by {row['wants']}")
        ends = time.time() + hours * 3600
        embed = discord.Embed(title="🍿 Movie night vote", description="\n".join(lines) + f"\n\nVoting closes <t:{int(ends)}:R>.", color=COLOR)
        view = discord.ui.View(timeout=None)
        for index, option in enumerate(options):
            view.add_item(VoteButton(index, option["name"]))
        message = await interaction.followup.send(embed=embed, view=view, wait=True)
        await self.tracker.store.execute(
            "INSERT INTO movie_night (message_id, channel_id, options, ends_at) VALUES (?, ?, ?, ?)",
            (message.id, message.channel.id, json.dumps(options), ends),
        )

    @app_commands.command(name="recap", description="Post this week's watching recap now")
    async def recap(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.tracker.weekly_recap(force=True)
        await interaction.followup.send("Posted the recap in the tracker channel (if anyone watched anything).", ephemeral=True)

    for _cmd in (info, rate, recommend, add, remove):
        _cmd.autocomplete("title")(_title_complete)
    del _cmd
