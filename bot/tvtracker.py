"""Runs TV Tracker (the movie and TV bot in tvtracker/) next to the archive bot.

TV Tracker is a separate Node.js Discord bot with its own token. The archive bot
starts it, restarts it if it stops, and reports how it's doing in /archive status.
Its settings are set from Discord with /archive tvtracker and kept encrypted.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import collections
import json
import logging
import os
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SETTINGS_KEY = "tvtracker_settings"
WEB_PORT = 3001
# Picture routes TV Tracker serves (banners, leaderboards, recaps). The archive
# bot passes these through at <PUBLIC_URL>/tv/... so Discord can fetch them.
IMAGE_PREFIXES = ("banner", "header", "bar", "collage", "ring", "art")
MEMORY_LIMIT_MB = 320  # the free server has 1 GB shared with the archive bot
READY_LINE = "Logged in as"


class TvTrackerError(ValueError):
    """The settings someone entered can't work."""


def client_id_from_token(token: str) -> str:
    """A Discord bot token starts with the bot's user ID, base64-encoded. That ID is the app's Client ID."""
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise TvTrackerError("That doesn't look like a Discord bot token (it should have two dots in it).")
    first = parts[0] + "=" * (-len(parts[0]) % 4)
    try:
        user_id = base64.urlsafe_b64decode(first).decode("ascii")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise TvTrackerError("That doesn't look like a Discord bot token.") from exc
    if not user_id.isdigit():
        raise TvTrackerError("That doesn't look like a Discord bot token.")
    return user_id


def new_settings(token: str, simkl_client_id: str, previous: dict[str, str] | None = None) -> dict[str, str]:
    """Settings for a token and Simkl ID. Keeps an existing dashboard password and secret."""
    token = "".join(token.split()).strip("\"'")
    simkl_client_id = simkl_client_id.strip()
    client_id = client_id_from_token(token)
    previous = previous or {}
    return {
        "token": token,
        "client_id": client_id,
        "simkl_client_id": simkl_client_id,
        "dashboard_password": previous.get("dashboard_password") or secrets.token_urlsafe(12),
        "session_secret": previous.get("session_secret") or secrets.token_hex(32),
    }


class TvTracker:
    def __init__(self, data_dir: Path, app_dir: Path, binary: str = "node", public_url: str = "") -> None:
        self._data_dir = data_dir
        self._public_url = public_url.rstrip("/")
        self._app_dir = app_dir
        self._binary = binary
        self._settings: dict[str, str] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stopping = False
        self.running = False
        self.connected = False
        self.last_lines: collections.deque[str] = collections.deque(maxlen=5)
        self.restarts = 0
        self.started_at = 0.0
        self._reconfigured = False

    @property
    def enabled(self) -> bool:
        return bool(self._settings)

    @property
    def settings(self) -> dict[str, str] | None:
        return dict(self._settings) if self._settings else None

    def installed(self) -> bool:
        return shutil.which(self._binary) is not None and (self._app_dir / "index.js").is_file()

    def status(self) -> str:
        if not self.enabled:
            return "off (set it up with `/archive tvtracker`)"
        if not self.installed():
            return "TV Tracker isn't installed in this image"
        if self.connected:
            simkl = "" if self._settings and self._settings.get("simkl_client_id") else " (no Simkl Client ID yet)"
            return f"online{simkl}"
        detail = " / ".join(list(self.last_lines)[-2:]) if self.last_lines else "starting"
        tries = f" (restarted {self.restarts} times)" if self.restarts else ""
        return f"not online{tries}: {detail[:300]}"

    def configure(self, settings: dict[str, str] | None) -> None:
        """Use new settings (or none, to switch it off) and restart with them."""
        self._settings = dict(settings) if settings else None
        self.last_lines.clear()
        self.restarts = 0
        self.connected = False
        if self._process is not None and self._process.returncode is None:
            self._reconfigured = True
            self._process.terminate()

    def environment(self) -> dict[str, str]:
        """Only what TV Tracker needs. The archive bot's own secrets are never passed on."""
        assert self._settings is not None
        data_file = self._data_dir / "tvtracker" / "storage.json"
        return {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "NODE_ENV": "production",
            "DISCORD_TOKEN": self._settings["token"],
            "CLIENT_ID": self._settings["client_id"],
            "SIMKL_CLIENT_ID": self._settings.get("simkl_client_id", ""),
            "DASHBOARD_PASSWORD": self._settings["dashboard_password"],
            "DASHBOARD_SESSION_SECRET": self._settings["session_secret"],
            "PORT": str(WEB_PORT),
            "TV_DATA_FILE": str(data_file),
            "NODE_OPTIONS": f"--max-old-space-size={MEMORY_LIMIT_MB}",
            # Where Discord fetches TV Tracker's pictures (through the archive bot).
            "PUBLIC_URL": f"{self._public_url}/tv" if self._public_url else "",
        }

    async def run(self) -> None:
        if not self.enabled or not self.installed():
            return
        self.running = True
        try:
            await self._loop()
        finally:
            self.running = False

    async def _loop(self) -> None:
        delay = 5.0
        while not self._stopping and self.enabled:
            self.started_at = time.time()
            self.connected = False
            try:
                (self._data_dir / "tvtracker").mkdir(parents=True, exist_ok=True)
                self._process = await asyncio.create_subprocess_exec(
                    self._binary, str(self._app_dir / "index.js"),
                    cwd=str(self._app_dir),
                    env=self.environment(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                assert self._process.stdout is not None
                async for raw in self._process.stdout:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    if READY_LINE in line:
                        self.connected = True
                        delay = 5.0
                    self.last_lines.append(line)
                    log.info("tvtracker: %s", line)
                code = await self._process.wait()
                if not self._stopping:
                    self.last_lines.append(f"TV Tracker exited with code {code}")
            except asyncio.CancelledError:
                self.stop()
                raise
            except Exception as exc:
                log.exception("Couldn't start TV Tracker")
                self.last_lines.append(f"couldn't start TV Tracker: {exc}")
            self.connected = False
            if self._stopping or not self.enabled:
                break
            if self._reconfigured:  # New settings: start again straight away.
                self._reconfigured = False
                continue
            self.restarts += 1
            log.warning("TV Tracker stopped; restarting in %.0fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300.0)

    def stop(self) -> None:
        self._stopping = True
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()


def seal_settings(vault: Any, settings: dict[str, str]) -> str:
    return base64.b64encode(vault.seal(json.dumps(settings).encode())).decode()


def unseal_settings(vault: Any, stored: str | None) -> dict[str, str] | None:
    if not stored:
        return None
    try:
        return json.loads(vault.unseal(base64.b64decode(stored)))
    except Exception:  # A changed encryption key or damaged value: treat as not set up.
        log.warning("Couldn't read the saved TV Tracker settings; set them again with /archive tvtracker")
        return None
