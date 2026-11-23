"""TV Tracker runs beside the archive bot, set up from Discord with /archive tvtracker."""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

import pytest

from bot.tvtracker import TvTracker, TvTrackerError, client_id_from_token, new_settings
from conftest import Harness, until
from fakes import OWNER_ID

APP_ID = "123456789012345678"
TOKEN = base64.urlsafe_b64encode(APP_ID.encode()).decode().rstrip("=") + ".GxYz12.abcdefghijklmnop"


def fake_node(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in for node that records its environment and says it logged in."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env_dump = tmp_path / "env.txt"
    script = bin_dir / "node"
    script.write_text(f"#!/bin/sh\nenv > {env_dump}\necho 'Logged in as TV Tracker#0001'\nexec sleep 30\n")
    script.chmod(0o755)
    app = tmp_path / "tvtracker"
    app.mkdir()
    (app / "index.js").write_text("// stand-in\n")
    return bin_dir, env_dump


def test_client_id_comes_from_the_token() -> None:
    assert client_id_from_token(TOKEN) == APP_ID
    for bad in ["", "no-dots", "a.b", "!!!.b.c", base64.b64encode(b"letters").decode() + ".b.c"]:
        with pytest.raises(TvTrackerError):
            client_id_from_token(bad)
    first = new_settings(f"  {TOKEN} ", " simkl-123 ")
    assert first["token"] == TOKEN and first["simkl_client_id"] == "simkl-123"
    again = new_settings(TOKEN, "simkl-456", first)
    assert again["dashboard_password"] == first["dashboard_password"]
    assert again["session_secret"] == first["session_secret"]


async def test_runs_with_only_its_own_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir, env_dump = fake_node(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("ENCRYPTION_KEY", "archive-secret")
    monkeypatch.setenv("DISCORD_TOKEN", "archive-bot-token")

    off = TvTracker(tmp_path / "data", tmp_path / "tvtracker")
    assert off.status().startswith("off")
    missing = TvTracker(tmp_path / "data", tmp_path / "nowhere")
    missing.configure(new_settings(TOKEN, "simkl-1"))
    assert "isn't installed" in missing.status()

    tracker = TvTracker(tmp_path / "data", tmp_path / "tvtracker")
    tracker.configure(new_settings(TOKEN, "simkl-1"))
    task = asyncio.create_task(tracker.run())
    try:
        await until(lambda: tracker.connected, 10, "TV Tracker to log in")
        assert tracker.status() == "online"
        await until(lambda: env_dump.exists() and "TV_DATA_FILE" in env_dump.read_text(), 10, "the environment")
        env = dict(line.split("=", 1) for line in env_dump.read_text().splitlines() if "=" in line)
        assert env["DISCORD_TOKEN"] == TOKEN
        assert env["CLIENT_ID"] == APP_ID
        assert env["SIMKL_CLIENT_ID"] == "simkl-1"
        assert env["TV_DATA_FILE"] == str(tmp_path / "data" / "tvtracker" / "storage.json")
        assert "ENCRYPTION_KEY" not in env  # The archive bot's secrets stay with it.
        assert "archive-bot-token" not in env_dump.read_text()
    finally:
        tracker.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_owner_sets_it_up_from_discord(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir, env_dump = fake_node(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    fake = harness.fake
    general = fake.add_channel("general")
    client = await harness.start(TVTRACKER_DIR=str(tmp_path / "tvtracker"))
    assert "TV Tracker:** off" in await client.status_text(OWNER_ID)

    denied = await fake.slash(2001, "tvtracker", general)
    await until(lambda: fake.replies[denied], 20, "the refusal")
    assert "Only the bot owner" in harness.reply_text(denied)

    opened = await fake.slash(OWNER_ID, "tvtracker", general)
    await until(lambda: fake.replies[opened], 20, "the setup form")
    modal = fake.replies[opened][0]["payload"]["data"]
    bad = await fake.submit_modal(OWNER_ID, general, modal, ["not a token", "simkl-1"])
    await until(lambda: fake.replies[bad], 20, "the bad token message")
    assert "doesn't look like a Discord bot token" in harness.reply_text(bad)

    opened = await fake.slash(OWNER_ID, "tvtracker", general)  # A submitted form closes; open it again.
    await until(lambda: fake.replies[opened], 20, "the setup form again")
    modal = fake.replies[opened][0]["payload"]["data"]
    good = await fake.submit_modal(OWNER_ID, general, modal, [TOKEN, "simkl-1"])
    await until(lambda: fake.replies[good], 20, "the confirmation")
    assert "TV Tracker is starting" in harness.reply_text(good)
    reply = fake.replies[good][0]["payload"]["data"]
    invite = next(c["url"] for row in reply["components"] for c in row["components"] if c.get("url"))
    assert f"client_id={APP_ID}" in invite
    await until(lambda: client.tvtracker.connected, 10, "TV Tracker to start")
    assert "TV Tracker:** online" in await client.status_text(OWNER_ID)

    stored = await client.store.kv_get("tvtracker_settings")
    assert stored and TOKEN not in stored and "simkl-1" not in stored  # Kept encrypted.

    # A restart picks the saved settings back up.
    await harness.stop()
    env_dump.unlink()
    client = await harness.start(TVTRACKER_DIR=str(tmp_path / "tvtracker"))
    await until(lambda: client.tvtracker.connected, 10, "TV Tracker after a restart")

    # Typing "off" turns it off.
    opened = await fake.slash(OWNER_ID, "tvtracker", general)
    await until(lambda: fake.replies[opened], 20, "the setup form again")
    modal = fake.replies[opened][0]["payload"]["data"]
    off = await fake.submit_modal(OWNER_ID, general, modal, ["off", ""])
    await until(lambda: fake.replies[off], 20, "the off message")
    assert "TV Tracker is off" in harness.reply_text(off)
    await until(lambda: not client.tvtracker.running, 10, "TV Tracker to stop")
    assert "TV Tracker:** off" in await client.status_text(OWNER_ID)


async def test_pictures_pass_through_the_public_address(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discord fetches TV Tracker's banners and leaderboards from <PUBLIC_URL>/tv/..."""
    from aiohttp import ClientSession, web

    from bot.tvtracker import WEB_PORT

    seen: list[str] = []

    async def picture(request: web.Request) -> web.Response:
        seen.append(request.path_qs)
        return web.Response(body=b"\x89PNG fake", content_type="image/png", headers={"Cache-Control": "max-age=60"})

    async def dashboard(_: web.Request) -> web.Response:
        return web.Response(text="secret dashboard")

    fake_tv = web.Application()
    fake_tv.router.add_get("/art/v1/{tail:.*}", picture)
    fake_tv.router.add_get("/dashboard/{tail:.*}", dashboard)
    runner = web.AppRunner(fake_tv)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", WEB_PORT)
    await site.start()
    try:
        client = await harness.start()
        base = client.config.public_url
        async with ClientSession() as session:
            async with session.get(f"{base}/tv/art/v1/lb/1000/streak.png?v=2") as response:
                assert response.status == 200
                assert response.headers["Content-Type"] == "image/png"
                assert response.headers["Cache-Control"] == "max-age=60"
                assert await response.read() == b"\x89PNG fake"
            for blocked in ("/tv/dashboard/x", "/tv/art/../dashboard/x", "/tv/art/v1/a%20b"):
                async with session.get(f"{base}{blocked}") as response:
                    assert response.status == 404, blocked
        assert seen == ["/art/v1/lb/1000/streak.png?v=2"]

        bin_dir, _ = fake_node(tmp_path)
        tracker = TvTracker(tmp_path / "data", tmp_path / "tvtracker", public_url=base + "/")
        tracker.configure(new_settings(TOKEN, "simkl-1"))
        env = tracker.environment()
        assert env["PUBLIC_URL"] == f"{base}/tv"
        assert env["NODE_OPTIONS"].startswith("--max-old-space-size=")
    finally:
        await runner.cleanup()
