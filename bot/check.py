"""`python -m bot check`: test every connection and say exactly what's wrong."""
from __future__ import annotations

import shutil

import aiohttp
import discord

from .config import Config, ConfigError, fill_in_ids
from .google_photos import API_BASE, GooglePause, GooglePhotos, GoogleRejected, GoogleTransient, TokenProvider
from .store import Store
from .vault import Vault

_ALBUM_KEY = "album_id"


async def run_check(
    config: Config,
    google_api_base: str = API_BASE,
    drive_api: str = "https://www.googleapis.com/drive/v3/about",
) -> int:
    failures = 0

    def report(passed: bool, label: str, hint: str = "") -> None:
        nonlocal failures
        print(f"[{'ok' if passed else 'FAIL'}] {label}")
        if not passed:
            failures += 1
            if hint:
                print(f"       {hint}")

    report(True, "Settings loaded")
    report(bool(shutil.which("ffmpeg") and shutil.which("exiftool")), "ffmpeg and exiftool are installed")

    config.data_dir.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession() as session:
        store = Store(config.db_file)
        await store.open()
        try:
            tokens = TokenProvider(session, Vault(config.encryption_key), config.token_file)
            google = GooglePhotos(session, tokens, google_api_base, rate_limit_wait=1.0)
            album_id = await store.kv_get(_ALBUM_KEY)
            if album_id is None:
                album_id = await google.create_album(config.album_name)
                await store.kv_set(_ALBUM_KEY, album_id)
            report(True, f'Google Photos connected (album "{config.album_name}")')
            token = await tokens.get()
            async with session.get(
                drive_api, params={"fields": "user"}, headers={"Authorization": f"Bearer {token}"}
            ) as response:
                report(
                    response.status == 200,
                    "Google Drive connected",
                    "Enable the Google Drive API in your project, then run: docker compose run --rm bot auth",
                )
        except GooglePause as exc:
            report(False, "Google Photos connected", exc.alert or exc.reason)
        except (GoogleTransient, GoogleRejected, aiohttp.ClientError, TimeoutError) as exc:
            report(False, "Google Photos connected", str(exc))
        finally:
            await store.close()

    client = discord.Client(intents=discord.Intents.none())
    async with client:
        try:
            await client.login(config.discord_token)
            report(True, f"Discord token works ({client.user})")
        except discord.LoginFailure:
            report(False, "Discord token works", "Reset the token in the Developer Portal and update .env.")
            return _finish(failures)
        flags = client.application.flags if client.application else None
        report(
            bool(flags and (flags.gateway_message_content or flags.gateway_message_content_limited)),
            "Message Content Intent is on",
            "Developer Portal > your app > Bot > turn on Message Content Intent.",
        )
        report(
            bool(flags and (flags.gateway_guild_members or flags.gateway_guild_members_limited)),
            "Server Members Intent is on",
            "Developer Portal > your app > Bot > turn on Server Members Intent.",
        )
        report(
            bool(flags and (flags.gateway_presence or flags.gateway_presence_limited)),
            "Presence Intent is on (for live lyrics)",
            "Developer Portal > your app > Bot > turn on Presence Intent.",
        )
        if config.public_url:
            print(f"[--] Addon and lyrics pages will use {config.public_url}")
        else:
            print("[--] PUBLIC_URL isn't set, so Stremio addons and the lyrics page are off")
        try:
            config = await fill_in_ids(client, config)
        except ConfigError as exc:
            report(False, "Bot is in the server", str(exc))
            return _finish(failures)
        try:
            guild = await client.fetch_guild(config.guild_id)
        except discord.HTTPException:
            report(False, "Bot is in the server", "Invite the bot, and check DISCORD_GUILD_ID.")
            return _finish(failures)
        report(True, f"Bot is in the server ({guild.name})")
        try:
            await guild.fetch_member(config.owner_id)
            report(True, "The owner is a member of that server")
        except discord.HTTPException:
            report(False, "The owner is a member of that server", "Check DISCORD_OWNER_ID.")
    return _finish(failures)


def _finish(failures: int) -> int:
    print("All good." if not failures else f"{failures} thing(s) to fix.")
    return 1 if failures else 0
