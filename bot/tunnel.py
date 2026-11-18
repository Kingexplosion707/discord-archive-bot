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
        self.running = False
        self.connected = False
        self.last_lines: collections.deque[str] = collections.deque(maxlen=5)
        self.started_at = 0.0
        self.restarts = 0

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
        detail = " / ".join(list(self.last_lines)[-2:]) if self.last_lines else "starting"
        tries = f" (restarted {self.restarts} times)" if self.restarts else ""
        return f"not connected{tries}: {detail[:300]}"

    def set_token(self, token: str) -> None:
        """Switch to a new token and restart the tunnel with it."""
        self._token = token.strip()
        self.last_lines.clear()
        self.restarts = 0
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()

    async def run(self) -> None:
        if not self.enabled:
            return
        if shutil.which(self._binary) is None:
            log.error("TUNNEL_TOKEN is set but cloudflared isn't installed")
            return
        self.running = True
        try:
            await self._loop()
        finally:
            self.running = False

    async def _loop(self) -> None:
        delay = 5.0
        while not self._stopping:
            self.started_at = time.time()
            self.connected = False
            try:
                self._process = await asyncio.create_subprocess_exec(
                    self._binary, "tunnel", "--no-autoupdate", "--protocol", "http2", "run", "--token", self._token,
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
                    # Keep every line: some failures (like a bad token) aren't marked as errors.
                    self.last_lines.append(line)
                    log.info("cloudflared: %s", line)
                code = await self._process.wait()
                if not self._stopping:
                    self.last_lines.append(f"cloudflared exited with code {code}")
            except asyncio.CancelledError:
                self.stop()
                raise
            except Exception as exc:
                log.exception("Couldn't start cloudflared")
                self.last_lines.append(f"couldn't start cloudflared: {exc}")
            self.connected = False
            if self._stopping:
                break
            self.restarts += 1
            log.warning("cloudflared stopped; restarting in %.0fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300.0)

    def stop(self) -> None:
        self._stopping = True
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
