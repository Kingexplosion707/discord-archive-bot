from __future__ import annotations

import asyncio
import inspect
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import discord
import pytest
import yarl
from cryptography.fernet import Fernet
from discord.gateway import DiscordWebSocket

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot.config import Config  # noqa: E402
from bot.discord_app import ArchiveClient  # noqa: E402
from bot.google_photos import save_login  # noqa: E402
from bot.pipeline import Pipeline  # noqa: E402
from bot.vault import Vault  # noqa: E402
from fakes import GUILD_ID, OWNER_ID, FakeServices  # noqa: E402

_running_client: asyncio.Task[None] | None = None


async def until(predicate: Callable[[], Any], timeout: float = 60, what: str = "condition") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _running_client is not None and _running_client.done():
            _running_client.result()  # Surface why the bot stopped.
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {what}")


class Harness:
    def __init__(self, tmp_path: Path, services: FakeServices) -> None:
        self.tmp = tmp_path
        self.fake = services
        self.key = Fernet.generate_key().decode()
        self.client: ArchiveClient | None = None
        self._task: asyncio.Task[None] | None = None
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.web_port = sock.getsockname()[1]

    def config(self, **overrides: Any) -> Config:
        env = {
            "DISCORD_TOKEN": "fake-token",
            "DISCORD_GUILD_ID": str(GUILD_ID),
            "DISCORD_OWNER_ID": str(OWNER_ID),
            "ENCRYPTION_KEY": self.key,
            "DATA_DIR": str(self.tmp / "data"),
            "GOOGLE_CLIENT_SECRET_FILE": str(self.tmp / "client_secret.json"),
            "WEB_HOST": "127.0.0.1",
            "WEB_PORT": str(self.web_port),
            "PUBLIC_URL": f"http://127.0.0.1:{self.web_port}",
            **{k: str(v) for k, v in overrides.items()},
        }
        return Config.from_env(env)

    def save_google_login(self, config: Config) -> None:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        save_login(
            Vault(self.key),
            config.token_file,
            {
                "client_id": "client",
                "client_secret": "secret",
                "refresh_token": "refresh-1",
                "token_uri": f"{self.fake.base}/google/token",
            },
        )

    async def start(self, **overrides: Any) -> ArchiveClient:
        global _running_client
        config = self.config(**overrides)
        if not config.token_file.exists():
            self.save_google_login(config)
        self.fake.ready.clear()
        self.client = ArchiveClient(
            config,
            google_api_base=f"{self.fake.base}/google/v1",
            google_rate_limit_wait=0.05,
            drive_upload_base=f"{self.fake.base}/drive/upload",
            cinemeta=f"{self.fake.base}/cinemeta",
            kitsu=f"{self.fake.base}/kitsu",
            spicy_api=f"{self.fake.base}/spicy",
            lrclib_api=f"{self.fake.base}/lrclib",
            stremio_api=f"{self.fake.base}/stremio-api",
            game_catalog=f"{self.fake.base}/games/detectable",
            steam_api=f"{self.fake.base}/steamapi",
            steam_store=f"{self.fake.base}/steamstore",
        )
        self.client.tracker_job_seconds = 0.2
        self.client.stremio_poll_seconds = 0.2
        self._task = asyncio.create_task(self.client.start("fake-token"))
        _running_client = self._task
        await until(self.client.is_ready, 30, "the bot to connect")
        await until(lambda: self.client.pipeline is not None and self.client._pipeline_task is not None, 30, "the worker")
        return self.client

    async def stop(self) -> None:
        global _running_client
        _running_client = None
        if self.client is not None:
            await self.client.close()
            self.client = None
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def drained(self) -> bool:
        assert self.client is not None
        counts = await self.client.store.status_counts()
        return not self.client.crawler.running and counts.get("queued", 0) == 0 and sum(counts.values()) > 0

    async def statuses(self) -> dict[str, str]:
        assert self.client is not None
        async with self.client.store.db.execute("SELECT filename, status FROM attachment") as cursor:
            return {row["filename"]: row["status"] for row in await cursor.fetchall()}

    def uploaded(self) -> set[str]:
        return {item["filename"] for item in self.fake.items}

    def reply_text(self, interaction_id: str) -> str:
        return "\n".join(
            str(reply["payload"].get("data", reply["payload"]).get("content") or "")
            for reply in self.fake.replies[interaction_id]
        )


@pytest.fixture
async def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    services = FakeServices()
    await services.start()
    monkeypatch.setattr(discord.http.Route, "BASE", f"{services.base}/api/v10")
    monkeypatch.setattr(DiscordWebSocket, "DEFAULT_GATEWAY", yarl.URL(f"ws://127.0.0.1:{services.port}/gateway"))
    monkeypatch.setattr(Pipeline, "idle_seconds", 0.05)
    monkeypatch.setattr(Pipeline, "retry_base_seconds", 0.02)
    instance = Harness(tmp_path, services)
    yield instance
    await instance.stop()
    await services.stop()
