"""The worker: download, upload to Google Photos, delete the temp copy."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tempfile
import time
from pathlib import Path
from typing import Protocol

import aiohttp
import discord

from . import media, pins, store as st
from .config import Config
from .google_photos import GooglePause, GooglePhotos, GoogleRejected, GoogleTransient
from .store import HashEntry, Job, Store

log = logging.getLogger(__name__)


def build_caption(
    author_name: str | None, author_handle: str | None, channel: str | None, is_pin: bool
) -> str:
    """One line for the Google Photos caption: who posted it, and where."""
    who = (author_name or author_handle or "unknown").strip()
    if author_handle and author_handle != author_name:
        who = f"{who} (@{author_handle})"
    verb = "Pinned message from" if is_pin else "Sent by"
    where = f" in {channel}" if channel else ""
    return f"{verb} {who}{where}"


MAX_ATTEMPTS = 8
_ALBUM_KEY = "album_id"
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=3600, sock_read=300)
_EXPIRED_LINK_STATUSES = {401, 403, 404, 410}
_PIN_IMAGE_CAP = 30 * 1024 * 1024


class AttachmentGone(Exception):
    """The message or file no longer exists."""


class TemporaryProblem(Exception):
    """Retry later."""


class DiscordSide(Protocol):
    async def fresh_url(self, job: Job) -> str: ...

    async def message_media_urls(self, job: Job) -> dict[int, str]: ...

    async def alert_owner(self, text: str) -> None: ...


class Pipeline:
    idle_seconds = 15.0
    retry_base_seconds = 60.0

    def __init__(
        self,
        config: Config,
        store: Store,
        http: aiohttp.ClientSession,
        google: GooglePhotos,
        discord_side: DiscordSide,
    ) -> None:
        self._config = config
        self._store = store
        self._http = http
        self._google = google
        self._discord = discord_side
        self._wake = asyncio.Event()
        self._paused_until = 0.0
        self._pause_reason: str | None = None
        self._alerted: set[str] = set()
        self._stopping = False
        self._looks: list[tuple[tuple[int, float], HashEntry]] = []

    @property
    def pause_status(self) -> tuple[str, float] | None:
        if time.time() < self._paused_until and self._pause_reason:
            return self._pause_reason, self._paused_until
        return None

    def wake(self) -> None:
        self._wake.set()

    def resume(self) -> None:
        """Lift a pause right away, e.g. after the owner signs in to Google again."""
        self._paused_until = 0.0
        self._pause_reason = None
        self._alerted.clear()
        self.wake()

    def stop(self) -> None:
        self._stopping = True
        self.wake()

    async def run(self) -> None:
        self._looks = await self._store.all_looks()
        while not self._stopping:
            try:
                job = None
                if time.time() >= self._paused_until:
                    job = await self._store.next_job()
                if job is None:
                    await self._idle()
                    continue
                await self._run_job(job)
            except Exception:  # The worker must outlive any single problem.
                log.exception("Worker error")
                await self._idle()

    async def _idle(self) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self.idle_seconds)
        except asyncio.TimeoutError:
            pass

    async def _run_job(self, job: Job) -> None:
        try:
            await self._process(job)
        except (AttachmentGone, media.MediaError) as exc:
            await self._store.finish(job.id, st.SKIPPED, note=str(exc))
        except GooglePause as exc:
            await self._pause_uploads(exc)
        except GoogleRejected as exc:
            if "album" in str(exc).lower():
                await self._store.kv_delete(_ALBUM_KEY)
                await self._retry(job, exc)
            else:
                await self._store.finish(job.id, st.FAILED, note=str(exc))
        except (TemporaryProblem, GoogleTransient, aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            await self._retry(job, exc)
        except Exception as exc:  # A bad file must never stop the whole queue.
            log.exception("Unexpected error on attachment %s", job.id)
            await self._retry(job, exc)

    async def _retry(self, job: Job, exc: Exception) -> None:
        note = f"{type(exc).__name__}: {exc}"
        if job.attempts + 1 >= MAX_ATTEMPTS:
            await self._store.finish(job.id, st.FAILED, note=note)
            return
        delay = min(6 * 3600, self.retry_base_seconds * 2**job.attempts)
        await self._store.retry_later(job.id, delay, note)

    async def _pause_uploads(self, pause: GooglePause) -> None:
        self._paused_until = time.time() + pause.seconds
        self._pause_reason = pause.reason
        log.warning("Pausing uploads for %ss: %s", pause.seconds, pause.reason)
        if pause.alert and pause.reason not in self._alerted:
            self._alerted.add(pause.reason)
            try:
                await self._discord.alert_owner(pause.alert)
            except Exception:
                log.exception("Couldn't tell the owner uploads are paused")

    async def _process(self, job: Job) -> None:
        is_pin = job.content_type == pins.CONTENT_TYPE
        kind = media.IMAGE if is_pin else media.classify(job.filename, job.content_type)
        if kind is None:
            await self._store.finish(job.id, st.SKIPPED, note="not a photo or video")
            return
        self._config.tmp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self._config.tmp_dir) as work:
            work_dir = Path(work)
            if is_pin:
                source = work_dir / "pin.jpg"
                pin_images = await self._download_pin_images(job, work_dir)
                await asyncio.to_thread(pins.render, job.url, source, pin_images)
                sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            else:
                source = work_dir / f"input{media.safe_extension(job.filename, job.content_type)}"
                sha256 = await self._download(job, source)
            cached = await self._store.hash_get(sha256)
            if cached and cached.decision == st.UPLOADED:
                await self._store.mark_duplicate(job.id, sha256, cached)
                return
            look = None
            if kind == media.IMAGE and not is_pin:
                look = await asyncio.to_thread(media.look_hash, source)
            if look is not None:
                match = next((entry for known, entry in self._looks if media.looks_alike(look, known)), None)
                if match is not None:
                    await self._store.mark_duplicate(job.id, sha256, match)
                    return
            posted_at = discord.utils.snowflake_time(job.message_id)
            author = job.author_name or job.author_handle
            channel = await self._store.channel_label(job.channel_id)
            caption = build_caption(job.author_name, job.author_handle, channel, is_pin)
            ready = await asyncio.to_thread(
                media.prepare_upload,
                source,
                job.filename,
                kind,
                work_dir,
                posted_at,
                self._config.timezone,
                author,
            )
            album_id = await self._album_id()
            upload_token = await self._google.upload_bytes(ready.path, ready.mime_type)
            item = await self._google.create_media_item(
                upload_token, ready.filename, album_id, caption
            )
            await self._store.mark_uploaded(job.id, sha256, item.id, item.product_url)
            if look is not None:
                await self._store.add_look(job.id, look[0], look[1], item.id, item.product_url)
                self._looks.append((look, HashEntry(st.UPLOADED, item.id, item.product_url)))
            self._alerted.clear()
            self._pause_reason = None

    async def _download(self, job: Job, target: Path) -> str:
        limit = self._config.max_download_mb * 1024 * 1024
        too_big = f"over the {self._config.max_download_mb} MB limit"
        if job.size > limit:
            raise AttachmentGone(too_big)
        url = job.url
        for attempt in range(2):
            async with self._http.get(url, timeout=_DOWNLOAD_TIMEOUT) as response:
                if response.status in _EXPIRED_LINK_STATUSES and attempt == 0:
                    url = await self._discord.fresh_url(job)
                    await self._store.update_url(job.id, url)
                    continue
                if response.status != 200:
                    raise TemporaryProblem(f"download failed with HTTP {response.status}")
                digest = hashlib.sha256()
                written = 0
                with target.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        written += len(chunk)
                        if written > limit:
                            raise AttachmentGone(too_big)
                        digest.update(chunk)
                        handle.write(chunk)
                return digest.hexdigest()
        raise TemporaryProblem("download link kept expiring")

    async def _download_pin_images(self, job: Job, work_dir: Path) -> list[Path]:
        """Download the images attached to a pinned message, for the card to embed."""
        try:
            images = json.loads(job.url).get("images", [])
        except (ValueError, TypeError, AttributeError):
            return []
        if not images:
            return []
        fresh = await self._discord.message_media_urls(job)
        paths: list[Path] = []
        for index, image in enumerate(images):
            url = fresh.get(image.get("id"), image.get("url"))
            if not url:
                continue
            suffix = media.safe_extension(image.get("filename", ""), image.get("content_type"))
            target = work_dir / f"pin-img-{index}{suffix}"
            try:
                await self._fetch(url, target)
                paths.append(target)
            except (TemporaryProblem, AttachmentGone, aiohttp.ClientError, asyncio.TimeoutError, OSError):
                continue
        return paths

    async def _fetch(self, url: str, target: Path) -> None:
        async with self._http.get(url, timeout=_DOWNLOAD_TIMEOUT) as response:
            if response.status != 200:
                raise TemporaryProblem(f"pin image download failed with HTTP {response.status}")
            written = 0
            with target.open("wb") as handle:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    written += len(chunk)
                    if written > _PIN_IMAGE_CAP:
                        raise AttachmentGone("pin image too big to embed")
                    handle.write(chunk)

    async def _album_id(self) -> str:
        album_id = await self._store.kv_get(_ALBUM_KEY)
        if album_id is None:
            album_id = await self._google.create_album(self._config.album_name)
            await self._store.kv_set(_ALBUM_KEY, album_id)
        return album_id
