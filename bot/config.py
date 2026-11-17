"""Settings, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


def _text(env: Mapping[str, str], name: str, default: str | None = None) -> str:
    value = env.get(name, "").strip()
    if value:
        return value
    if default is None:
        raise ConfigError(f"{name} is not set. Add it to your .env file.")
    return default


def _int(env: Mapping[str, str], name: str, default: int | None = None) -> int:
    raw = _text(env, name, None if default is None else str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}.") from exc


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _text(env, name, "true" if default else "false").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}.")


def _id_set(env: Mapping[str, str], name: str) -> frozenset[int]:
    raw = env.get(name, "").replace(" ", "")
    try:
        return frozenset(int(part) for part in raw.split(",") if part)
    except ValueError as exc:
        raise ConfigError(f"{name} must be comma-separated channel IDs.") from exc


@dataclass(frozen=True)
class Config:
    discord_token: str
    guild_id: int
    owner_id: int
    encryption_key: str
    data_dir: Path
    client_secret_file: Path
    album_name: str
    max_download_mb: int
    skip_channel_ids: frozenset[int]
    scan_nsfw: bool
    sync_hour: int
    sync_minute: int
    timezone: ZoneInfo
    skip_role_name: str
    log_file_name: str
    public_url: str
    web_host: str
    web_port: int
    start_fresh: bool = False

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, need_discord: bool = True
    ) -> "Config":
        env = os.environ if env is None else env
        zone_name = _text(env, "TIMEZONE", "America/Toronto")
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"TIMEZONE isn't a known time zone: {zone_name!r}.") from exc
        sync_time = _text(env, "SYNC_TIME", "19:00")
        try:
            parsed = datetime.strptime(sync_time, "%H:%M")
        except ValueError as exc:
            raise ConfigError(f"SYNC_TIME must look like 19:00, got {sync_time!r}.") from exc
        sync_hour, sync_minute = parsed.hour, parsed.minute
        public_url = _text(env, "PUBLIC_URL", "").rstrip("/")
        if public_url and not public_url.startswith(("https://", "http://")):
            raise ConfigError("PUBLIC_URL must start with https://, like https://bot.example.com")
        return cls(
            discord_token=_text(env, "DISCORD_TOKEN", None if need_discord else ""),
            # 0 means "work it out": the one server the bot is in, and the app's owner.
            guild_id=_int(env, "DISCORD_GUILD_ID", 0),
            owner_id=_int(env, "DISCORD_OWNER_ID", 0),
            encryption_key=_text(env, "ENCRYPTION_KEY"),
            data_dir=Path(_text(env, "DATA_DIR", "/data")),
            client_secret_file=Path(
                _text(env, "GOOGLE_CLIENT_SECRET_FILE", "/secrets/client_secret.json")
            ),
            album_name=_text(env, "GOOGLE_ALBUM_NAME", "Discord Archive"),
            max_download_mb=_int(env, "MAX_DOWNLOAD_MB", 500),
            skip_channel_ids=_id_set(env, "SKIP_CHANNEL_IDS"),
            scan_nsfw=_bool(env, "SCAN_NSFW_CHANNELS", False),
            start_fresh=_bool(env, "START_FRESH", False),
            sync_hour=sync_hour,
            sync_minute=sync_minute,
            timezone=zone,
            skip_role_name=_text(env, "SKIP_ROLE_NAME", "clankers"),
            log_file_name=_text(env, "DRIVE_LOG_NAME", "Discord Server Log.txt"),
            public_url=public_url,
            web_host=_text(env, "WEB_HOST", "0.0.0.0"),
            web_port=_int(env, "WEB_PORT", 8080),
        )

    def seconds_until_sync(self, now: datetime | None = None) -> float:
        """Time until the next daily sync, in the configured time zone."""
        current = (now or datetime.now(self.timezone)).astimezone(self.timezone)
        target = current.replace(hour=self.sync_hour, minute=self.sync_minute, second=0, microsecond=0)
        if target <= current:
            target = (current + timedelta(days=1)).replace(
                hour=self.sync_hour, minute=self.sync_minute, second=0, microsecond=0
            )
        return target.timestamp() - current.timestamp()

    @property
    def db_file(self) -> Path:
        return self.data_dir / "archive.db"

    @property
    def token_file(self) -> Path:
        return self.data_dir / "google_token.enc"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"


async def fill_in_ids(client: Any, config: Config) -> Config:
    """Fills in a missing server ID (the only server the bot is in) and owner ID (the app's owner)."""
    guild_id, owner_id = config.guild_id, config.owner_id
    if not owner_id:
        info = await client.application_info()
        owner_id = info.team.owner_id if info.team and info.team.owner_id else info.owner.id
    if not guild_id:
        guilds = [guild async for guild in client.fetch_guilds(limit=3)]
        if len(guilds) != 1:
            raise ConfigError(
                "The bot is in "
                + ("no servers" if not guilds else "more than one server")
                + ". Set DISCORD_GUILD_ID in .env to the server to archive."
            )
        guild_id = guilds[0].id
    return replace(config, guild_id=guild_id, owner_id=owner_id)
