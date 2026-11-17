"""Runs the Cloudflare tunnel next to the bot, so the public address works with no extra setup."""
from __future__ import annotations

import asyncio
import collections
import logging
import shutil
import time

log = logging.getLogger(__name__)


class Tunnel:
    def __init__(self, token: str, binary: str = "cloudflared") -> None:
        self._token = token
        self._binary = binary
        self._process: asyncio.subprocess.Process | None = None
        self._stopping = False
        self.connected = False
        self.last_lines: collections.deque[str] = collections.deque(maxlen=5)
        self.started_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    def status(self) -> str:
        if not self.enabled:
            return "off (no TUNNEL_TOKEN)"
        if shutil.which(self._binary) is None:
            return "cloudflared isn't installed in this image"
        if self.connected:
            return "connected"
        detail = self.last_lines[-1] if self.last_lines else "starting"
        return f"not connected: {detail[:200]}"

    async def run(self) -> None:
        if not self.enabled:
            return
        if shutil.which(self._binary) is None:
            log.error("TUNNEL_TOKEN is set but cloudflared isn't installed")
            return
        delay = 5.0
        while not self._stopping:
            self.started_at = time.time()
            self.connected = False
            try:
                self._process = await asyncio.create_subprocess_exec(
                    self._binary, "tunnel", "--no-autoupdate", "run", "--token", self._token,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                assert self._process.stdout is not None
                async for raw in self._process.stdout:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    if "Registered tunnel connection" in line:
                        self.connected = True
                        delay = 5.0
                    if " ERR " in line or "error" in line.lower() or "Registered" in line:
                        self.last_lines.append(line)
                        log.info("cloudflared: %s", line)
                await self._process.wait()
            except asyncio.CancelledError:
                self.stop()
                raise
            except Exception:
                log.exception("Couldn't start cloudflared")
            self.connected = False
            if self._stopping:
                break
            log.warning("cloudflared stopped; restarting in %.0fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300.0)

    def stop(self) -> None:
        self._stopping = True
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
