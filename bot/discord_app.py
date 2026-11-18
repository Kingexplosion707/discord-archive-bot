"""The Discord side: events and slash commands."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any

import aiohttp
import discord
from aiohttp import web as aioweb
from discord import app_commands

from . import store as st
from .config import Config, fill_in_ids
from .crawler import UNREADABLE_KEY, Crawler, attachment_rows
from .drive_log import UPLOAD_BASE, DriveLog, channel_label, log_row
from .google_login import LoginProblem, finish_login, start_login
from .google_photos import API_BASE, GooglePhotos, TokenProvider
from .lyrics import LRCLIB_API, SPICY_API, LyricsFetcher, LyricsGroup, LyricsService
from .meta import CINEMETA, KITSU, MetaClient
from .tracker import TitleButton, Tracker, TrackerGroup, VoteButton, WatchGroup
from .web import build_app
from .tunnel import Tunnel
from .pipeline import AttachmentGone, Pipeline, TemporaryProblem
from .store import Job, Store
from .vault import Vault

log = logging.getLogger(__name__)
FRESH_START_KEY = "fresh_start_done"
TUNNEL_TOKEN_KEY = "tunnel_token"


class ArchiveGroup(app_commands.Group):
    def __init__(self, bot: "ArchiveClient") -> None:
        super().__init__(name="archive", description="Copy this server's media to Google Photos")
        self.bot = bot

    @app_commands.command(name="scan", description="Check the server for new media and retry failures")
    async def scan(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.bot.config.owner_id:
            await interaction.response.send_message("Only the bot owner can do that.", ephemeral=True)
            return
        retried = await self.bot.store.requeue_failed()
        if retried:
            self.bot.pipeline.wake()
        text = "Scanning the server." if self.bot.trigger_crawl() else "A scan is already running."
        if retried:
            text += f" Retrying {retried} failed item(s)."
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.command(name="google", description="Sign the bot in to Google Photos and Drive")
    async def google(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.bot.config.owner_id:
            await interaction.response.send_message("Only the bot owner can do that.", ephemeral=True)
            return
        try:
            flow, url, state = start_login(self.bot.config)
        except LoginProblem as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        embed = discord.Embed(
            title="Connect Google",
            color=0x4285F4,
            description=(
                "1. Tap **Sign in** and choose the Google account for the archive.\n"
                "2. Allow access. If Google says it hasn't verified the app, tap Advanced, then continue.\n"
                "3. You'll land on a page that won't load (localhost). That's expected: "
                "copy the whole address from the address bar.\n"
                "4. Tap **Paste address** here and paste it."
            ),
        )
        await interaction.response.send_message(
            embed=embed, view=GoogleLoginView(self.bot, flow, state, url), ephemeral=True
        )

    @app_commands.command(name="tunnel", description="Set the Cloudflare tunnel token for the web address")
    @app_commands.describe(token="From Cloudflare Zero Trust > Networks > Tunnels > your tunnel")
    async def tunnel(self, interaction: discord.Interaction, token: str) -> None:
        if interaction.user.id != self.bot.config.owner_id:
            await interaction.response.send_message("Only the bot owner can do that.", ephemeral=True)
            return
        token = "".join(token.split()).strip("\"'")
        await self.bot.store.kv_set(TUNNEL_TOKEN_KEY, token)
        tunnel = self.bot.tunnel
        tunnel.set_token(token)
        if not tunnel.running:
            self.bot._spawn(tunnel.run())
        await interaction.response.send_message(
            "Saved. The tunnel is restarting with the new token; check `/archive status` in a minute.",
            ephemeral=True,
        )

    @app_commands.command(name="status", description="Show upload progress")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            await self.bot.status_text(interaction.user.id), ephemeral=True
        )


class GoogleLoginView(discord.ui.View):
    def __init__(self, bot: "ArchiveClient", flow: Any, state: str, url: str) -> None:
        super().__init__(timeout=900)
        self.bot, self.flow, self.state = bot, flow, state
        self.add_item(discord.ui.Button(label="Sign in", url=url, style=discord.ButtonStyle.link))

    @discord.ui.button(label="Paste address", style=discord.ButtonStyle.primary)
    async def paste(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(GoogleCodeModal(self))


class GoogleCodeModal(discord.ui.Modal, title="Paste the address"):
    address: discord.ui.TextInput = discord.ui.TextInput(
        label="Address from the page that didn't load",
        placeholder="http://localhost:8765/?state=...&code=...",
        style=discord.TextStyle.paragraph,
        max_length=2000,
    )

    def __init__(self, login: GoogleLoginView) -> None:
        super().__init__()
        self.login = login

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = self.login.bot
        try:
            await asyncio.to_thread(
                finish_login, bot.config, self.login.flow, self.login.state, str(self.address.value)
            )
        except LoginProblem as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        bot.tokens.forget()
        bot.pipeline.resume()
        self.login.stop()
        await interaction.followup.send("Google is connected. Uploads start now.", ephemeral=True)


class ArchiveClient(discord.Client):
    def __init__(
        self,
        config: Config,
        *,
        google_api_base: str = API_BASE,
        google_rate_limit_wait: float = 30.0,
        drive_upload_base: str = UPLOAD_BASE,
        cinemeta: str = CINEMETA,
        kitsu: str = KITSU,
        spicy_api: str = SPICY_API,
        lrclib_api: str = LRCLIB_API,
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        intents.members = True  # Names and avatars on posts, and the members who listen.
        intents.presences = True  # Spotify status, for live lyrics.
        super().__init__(
            intents=intents,
            max_messages=None,
            member_cache_flags=discord.MemberCacheFlags.from_intents(intents),
            chunk_guilds_at_startup=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self._meta_urls = (cinemeta, kitsu)
        self._lyrics_urls = (spicy_api, lrclib_api)
        self.tracker: Tracker = None  # type: ignore[assignment]
        self.lyrics: LyricsService = None  # type: ignore[assignment]
        self.tracker_job_seconds = 60.0
        self._web_runner: aioweb.AppRunner | None = None
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.vault = Vault(config.encryption_key)
        self.store = Store(config.db_file)
        self.web: aiohttp.ClientSession | None = None
        self.pipeline: Pipeline = None  # type: ignore[assignment]
        self.crawler: Crawler = None  # type: ignore[assignment]
        self.drive_log: DriveLog = None  # type: ignore[assignment]
        self._google_api_base = google_api_base
        self._google_rate_limit_wait = google_rate_limit_wait
        self._drive_upload_base = drive_upload_base
        self._owner_user: discord.User | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._pipeline_task: asyncio.Task[None] | None = None
        self.tunnel: Tunnel | None = None

    async def setup_hook(self) -> None:
        self.config = config = await fill_in_ids(self, self.config)
        config.data_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(config.tmp_dir, ignore_errors=True)
        config.tmp_dir.mkdir(parents=True, exist_ok=True)
        await self.store.open()
        self.web = aiohttp.ClientSession()
        tokens = self.tokens = TokenProvider(self.web, self.vault, config.token_file)
        self.drive_log = DriveLog(self.web, tokens, self.store, config, self._drive_upload_base)
        google = GooglePhotos(
            self.web,
            tokens,
            self._google_api_base,
            self._google_rate_limit_wait,
        )
        self.pipeline = Pipeline(config, self.store, self.web, google, self)
        self.crawler = Crawler(self, config, self.store, self.pipeline.wake)
        self.tracker = Tracker(self, self.store, MetaClient(self.web, self.store, *self._meta_urls))
        self.lyrics = LyricsService(self, self.store, LyricsFetcher(self.web, *self._lyrics_urls))
        self.add_dynamic_items(TitleButton, VoteButton)

        self._web_runner = aioweb.AppRunner(build_app(self), access_log=None)
        await self._web_runner.setup()
        await aioweb.TCPSite(self._web_runner, config.web_host, config.web_port).start()
        log.info("Web server listening on port %s", config.web_port)
        # A token set with /archive tunnel wins over the one in .env.
        self.tunnel = Tunnel(await self.store.kv_get(TUNNEL_TOKEN_KEY) or config.tunnel_token)
        if self.tunnel.enabled:
            self._spawn(self.tunnel.run())

        guild = discord.Object(id=config.guild_id)
        self.tree.add_command(ArchiveGroup(self), guild=guild)
        self.tree.add_command(TrackerGroup(self.tracker), guild=guild)
        self.tree.add_command(WatchGroup(self.tracker), guild=guild)
        self.tree.add_command(LyricsGroup(self.lyrics), guild=guild)
        self.tree.on_error = self._on_command_error
        await self.tree.sync(guild=guild)
        self._spawn(self._after_ready())

    def _spawn(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("Background task failed", exc_info=task.exception())

    async def _after_ready(self) -> None:
        await self.wait_until_ready()
        self._pipeline_task = self._spawn(self.pipeline.run())
        self._spawn(self.tracker.run_jobs())
        if self.config.start_fresh and await self.store.kv_get(FRESH_START_KEY) is None:
            # Everything already in the server went to the album from the old machine.
            # Record it (and log its messages) without uploading it again.
            log.info("Fresh start: recording existing media without uploading it")
            self.crawler.baseline = True
            try:
                await self.crawler.crawl()
                await self.store.kv_set(FRESH_START_KEY, "done")
            finally:
                self.crawler.baseline = False
        while not self.is_closed():
            try:
                await self.crawler.crawl()
                await self.drive_log.safe_export(self.alert_owner)
            except Exception:  # The next round can still succeed.
                log.exception("Sync failed")
            await asyncio.sleep(self.config.seconds_until_sync())
            try:
                if await self.store.requeue_failed():
                    self.pipeline.wake()
            except Exception:
                log.exception("Couldn't retry failed items")

    async def close(self) -> None:
        if self.tunnel is not None:
            self.tunnel.stop()
        if self.pipeline is not None:
            self.pipeline.stop()
        tasks = list(self._background)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await super().close()
        if self._web_runner is not None:
            await self._web_runner.cleanup()
        if self.web is not None:
            await self.web.close()
        await self.store.close()

    async def _on_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.error("Command failed", exc_info=error)
        message = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    def trigger_crawl(self) -> bool:
        if self.crawler.running:
            return False
        self._spawn(self.crawler.crawl())
        return True

    # ---- events ----------------------------------------------------------

    async def on_ready(self) -> None:
        log.info("Connected as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.guild.id != self.config.guild_id:
            return
        if self.user is not None and message.author.id == self.user.id:
            return
        if not self.crawler.allowed(message.channel) or await self.crawler.excluded(message):
            return
        await self.store.set_channel_label(message.channel.id, channel_label(message.channel))
        entry = log_row(message)
        if entry is not None:
            await self.store.add_logs([entry])
        rows = attachment_rows(message)
        if rows:
            await self.store.add_attachments(rows)
            self.pipeline.wake()

    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        try:
            await self.lyrics.on_presence(after)
        except Exception:
            log.exception("Lyrics update failed")

    async def on_guild_channel_pins_update(self, channel: Any, last_pin: Any) -> None:
        if channel.guild.id == self.config.guild_id:
            await self.crawler.collect_pins(channel)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self.store.cancel_for_messages([payload.message_id])

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        await self.store.cancel_for_messages(payload.message_ids)

    # ---- used by the pipeline --------------------------------------------

    async def fresh_url(self, job: Job) -> str:
        channel = self.get_partial_messageable(job.channel_id, guild_id=self.config.guild_id)
        try:
            message = await channel.fetch_message(job.message_id)
        except (discord.NotFound, discord.Forbidden) as exc:
            raise AttachmentGone("message was deleted or can't be read") from exc
        except discord.HTTPException as exc:
            raise TemporaryProblem(f"couldn't refresh the link: {exc}") from exc
        for attachment in message.attachments:
            if attachment.id == job.id:
                return attachment.url
        raise AttachmentGone("attachment was removed from the message")

    async def message_media_urls(self, job: Job) -> dict[int, str]:
        """Current URLs for every attachment on a message, for embedding pin media."""
        channel = self.get_partial_messageable(job.channel_id, guild_id=self.config.guild_id)
        try:
            message = await channel.fetch_message(job.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return {}
        return {attachment.id: attachment.url for attachment in message.attachments}

    async def alert_owner(self, text: str) -> None:
        if self._owner_user is None:
            self._owner_user = await self.fetch_user(self.config.owner_id)
        await self._owner_user.send(text)

    async def status_text(self, viewer_id: int) -> str:
        counts = await self.store.status_counts()
        lines = [
            f"**Uploaded:** {counts.get(st.UPLOADED, 0):,}",
            f"**Waiting to upload:** {counts.get(st.QUEUED, 0):,}",
            f"**Duplicates skipped:** {counts.get(st.DUPLICATE, 0):,}",
            f"**Skipped or failed:** {counts.get(st.SKIPPED, 0) + counts.get(st.FAILED, 0):,}",
        ]
        if self.crawler.running:
            lines.append("Reading through the server now.")
        pause = self.pipeline.pause_status
        if pause:
            lines.append(f"**Uploads paused:** {pause[0]}, retrying <t:{int(pause[1])}:R>")
        if viewer_id == self.config.owner_id:
            unreadable = json.loads(await self.store.kv_get(UNREADABLE_KEY) or "[]")
            if unreadable:
                lines.append(f"**Channels I can't read:** {', '.join(unreadable[:20])}")
            if self.config.public_url:
                lines.append(f"**Web address ({self.config.public_url}):** {self.tunnel.status()}")
        return "\n".join(lines)
