"""The bot keeps the Cloudflare tunnel running and reports how it's doing."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from bot.tunnel import Tunnel
from conftest import until


async def test_tunnel_connects_and_restarts(tmp_path: Path) -> None:
    starts = tmp_path / "starts"
    fake = tmp_path / "cloudflared"
    fake.write_text(
        "#!/bin/sh\n"
        f"echo x >> {starts}\n"
        'test "$6 $7" = "--token secret-token" || { echo "ERR wrong arguments: $*"; exit 1; }\n'
        "echo 'INF Registered tunnel connection connIndex=0'\n"
        "sleep 0.3\n"
        "echo 'ERR lost connection'\n"
    )
    fake.chmod(0o755)
    tunnel = Tunnel("secret-token", binary=str(fake))
    tunnel_restart = asyncio.create_task(_run_with_fast_retry(tunnel))
    await until(lambda: tunnel.connected, 10, "the tunnel to connect")
    assert tunnel.status() == "connected"
    await until(lambda: starts.exists() and len(starts.read_text().split()) >= 2, 20, "a restart")
    tunnel.stop()
    tunnel_restart.cancel()
    await asyncio.gather(tunnel_restart, return_exceptions=True)

    assert Tunnel("").status().startswith("off")
    assert "isn't installed" in Tunnel("t", binary=str(tmp_path / "missing")).status()


async def _run_with_fast_retry(tunnel: Tunnel) -> None:
    real_sleep = asyncio.sleep

    async def quick(seconds: float) -> None:
        await real_sleep(0.05)

    asyncio.sleep = quick  # type: ignore[assignment]
    try:
        await tunnel.run()
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]


async def test_bot_starts_the_tunnel_from_its_settings(harness, tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "cloudflared"
    fake.write_text("#!/bin/sh\necho 'INF Registered tunnel connection connIndex=0'\nsleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    client = await harness.start(TUNNEL_TOKEN="from-env-file")
    await until(lambda: client.tunnel.connected, 10, "the bot's tunnel")
    status = await client.status_text(client.config.owner_id)
    assert "Web address" in status and "connected" in status
    await harness.stop()
    assert client.tunnel._process.returncode is not None  # Stopped with the bot.


async def test_owner_fixes_a_bad_token_from_discord(harness, tmp_path: Path, monkeypatch) -> None:
    from fakes import OWNER_ID

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "cloudflared"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$7" != "good-token" ]; then echo "Provided Tunnel token is not valid."; exit 1; fi\n'
        "echo 'INF Registered tunnel connection connIndex=0'\nsleep 30\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    general = harness.fake.add_channel("general")
    client = await harness.start(TUNNEL_TOKEN="mangled-token")
    await until(lambda: "not valid" in client.tunnel.status(), 10, "the bad token message")

    denied = await harness.fake.slash(2001, "tunnel", general, token="good-token")
    await until(lambda: harness.fake.replies[denied], 10, "the refusal")
    assert not client.tunnel.connected

    fixed = await harness.fake.slash(OWNER_ID, "tunnel", general, token=' "good-\ntoken" ')
    await until(lambda: harness.fake.replies[fixed], 10, "the reply")
    await until(lambda: client.tunnel.connected, 60, "the tunnel with the new token")

    # The new token is remembered across restarts.
    await harness.stop()
    client = await harness.start(TUNNEL_TOKEN="mangled-token")
    await until(lambda: client.tunnel.connected, 10, "the remembered token")
