"""Writes every saved message to one text file in Google Drive, one section per channel."""
from __future__ import annotations

import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
import discord

from .config import Config
from .google_photos import GooglePause, TokenProvider
from .pins import message_text
from .store import Store

log = logging.getLogger(__name__)

UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3/files"
_FILE_KEY = "drive_log_file_id"
_TIMEOUT = aiohttp.ClientTimeout(total=4 * 3600, sock_read=600)
RECONNECT_HELP = (
    "The message log couldn't be saved to Drive. Enable the Google Drive API, then run "
    "`/archive google` in the server to sign in again."
)


class DriveProblem(Exception):
    pass


def channel_label(channel: Any) -> str:
    parent = getattr(channel, "parent", None)
    name = getattr(channel, "name", str(channel.id))
    return f"#{parent.name} > {name}" if isinstance(channel, discord.Thread) and parent else f"#{name}"


def log_row(message: discord.Message) -> dict[str, Any] | None:
    lines = [message_text(message)] if message_text(message) else []
    lines.extend(f"[Attached: {item.filename}]" for item in message.attachments)
    if not lines:
        return None
    return {
        "message_id": message.id,
        "channel_id": message.channel.id,
        "author": message.author.display_name,
        "text": "\n".join(lines),
    }


class DriveLog:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        tokens: TokenProvider,
        store: Store,
        config: Config,
        upload_base: str = UPLOAD_BASE,
    ) -> None:
        self._session = session
        self._tokens = tokens
        self._store = store
        self._config = config
        self._upload_base = upload_base

    async def _write(self, path: Path) -> int:
        count = 0
        current = None
        with path.open("w", encoding="utf-8") as handle:
            async for channel_id, label, message_id, author, text in self._store.log_rows():
                if channel_id != current:
                    if current is not None:
                        handle.write("\n\n")
                    handle.write(f"{'=' * 70}\n{label}\n{'=' * 70}\n\n")
                    current = channel_id
                when = discord.utils.snowflake_time(message_id).astimezone(self._config.timezone)
                handle.write(f"[{when:%Y-%m-%d %H:%M}] {author}: {text}\n")
                count += 1
        return count

    async def _send(self, method: str, url: str, metadata: dict[str, str], path: Path) -> str | None:
        """Upload the file. Returns its ID, or None if the file no longer exists."""
        for attempt in range(2):
            token = await self._tokens.get()
            headers = {
                "Authorization": f"Bearer {token}",
                "X-Upload-Content-Type": "text/plain",
                "X-Upload-Content-Length": str(path.stat().st_size),
            }
            async with self._session.request(method, url, json=metadata, headers=headers, timeout=_TIMEOUT) as response:
                status = response.status
                location = response.headers.get("Location")
                detail = (await response.read()).decode("utf-8", "replace")[:200]
            if status == 401 and attempt == 0:
                self._tokens.forget()
                continue
            if status == 404:
                return None
            if status != 200 or not location:
                raise DriveProblem(f"Drive returned HTTP {status}: {detail}")
            with path.open("rb") as handle:
                async with self._session.put(
                    location, data=handle, headers={"Content-Type": "text/plain"}, timeout=_TIMEOUT
                ) as response:
                    if response.status not in (200, 201):
                        raise DriveProblem(f"Drive upload failed with HTTP {response.status}")
                    return str((await response.json(content_type=None))["id"])
        raise DriveProblem("Drive refused the login")

    async def export(self) -> bool:
        """Rebuild and upload the log. Returns False if there's nothing to save."""
        self._config.tmp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self._config.tmp_dir) as work:
            path = Path(work) / "log.txt"
            if not await self._write(path):
                return False
            file_id = await self._store.kv_get(_FILE_KEY)
            saved = None
            if file_id:
                saved = await self._send("PATCH", f"{self._upload_base}/{file_id}?uploadType=resumable", {}, path)
            if saved is None:
                metadata = {"name": self._config.log_file_name, "mimeType": "text/plain"}
                saved = await self._send("POST", f"{self._upload_base}?uploadType=resumable", metadata, path)
            if saved is None:
                raise DriveProblem("Drive couldn't create the file")
            await self._store.kv_set(_FILE_KEY, saved)
            log.info("Saved the message log to Drive at %s", datetime.now(self._config.timezone))
            return True

    async def safe_export(self, alert: Any) -> None:
        try:
            await self.export()
        except (DriveProblem, GooglePause) as exc:
            log.warning("Couldn't save the message log: %s", exc)
            await alert(RECONNECT_HELP)
        except (aiohttp.ClientError, TimeoutError, OSError):
            log.exception("Couldn't save the message log")
